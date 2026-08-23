from pathlib import Path

from backend.app.agent_runtime import (
    AgentRunState,
    AgentRunStore,
    AgentRuntime as PackageAgentRuntime,
    ModelTurn,
    NextAction,
    RuleBasedPermissionPolicy,
    RunBudget,
    ToolRegistry,
)
from backend.app.agent_runtime._runtime_core import AgentRuntime as PrivateCoreRuntime
from backend.app.agent_runtime.runtime import AgentRuntime as DirectAgentRuntime
from backend.app.agent_runtime.stage2_runtime import Stage2AgentRuntime
from backend.app.db import Database


class FinishModel:
    def __init__(self) -> None:
        self.calls = 0

    def next_action(self, _context):
        self.calls += 1
        return ModelTurn(
            action=NextAction(action="finish", final_output={"ok": True}),
            input_tokens=1,
            output_tokens=1,
            model_name="canonical-runtime-test",
        )


def _store(tmp_path: Path) -> AgentRunStore:
    return AgentRunStore(Database(tmp_path / "workbench.sqlite3"))


def test_all_public_runtime_imports_are_the_same_hardened_class() -> None:
    assert PackageAgentRuntime is DirectAgentRuntime
    assert Stage2AgentRuntime is DirectAgentRuntime
    assert DirectAgentRuntime is not PrivateCoreRuntime
    assert DirectAgentRuntime.__module__ == "backend.app.agent_runtime.runtime"


def test_resumed_persisted_budget_never_replaces_constructor_default(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = store.create_run(
        goal="resume with original one-call budget",
        budget=RunBudget(max_model_calls=1),
    )
    store.add_model_usage(
        run.id,
        ModelTurn(
            action=NextAction(action="finish", final_output={"old": True}),
            input_tokens=1,
            output_tokens=1,
            model_name="prior-process",
        ),
    )
    recovered = store.recover_interrupted(run.id)
    assert recovered.state == AgentRunState.needs_human.value

    model = FinishModel()
    runtime = DirectAgentRuntime(
        store=store,
        model=model,
        tools=ToolRegistry(),
        permissions=RuleBasedPermissionPolicy(),
        budget=RunBudget(max_model_calls=99),
    )

    resumed = runtime.resume_interrupted(run.id)

    assert resumed.state is AgentRunState.failed
    assert resumed.error_category == "budget_exhausted"
    assert model.calls == 0
    assert runtime.budget.max_model_calls == 99

    fresh = runtime.start("new run keeps constructor budget")

    assert fresh.state is AgentRunState.succeeded
    assert model.calls == 1
    fresh_record = store.get_run(fresh.run_id)
    assert fresh_record.budget_json["max_model_calls"] == 99
    assert runtime.budget.max_model_calls == 99
