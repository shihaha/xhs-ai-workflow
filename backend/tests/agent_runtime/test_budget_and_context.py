from pathlib import Path

from pydantic import BaseModel

from backend.app.agent_runtime import (
    AgentRunState,
    AgentRunStore,
    AgentRuntime,
    DefaultContextBuilder,
    ModelTurn,
    NextAction,
    RuleBasedPermissionPolicy,
    RunBudget,
    ToolExecutionResult,
    ToolRegistry,
    ToolSpec,
)
from backend.app.db import Database


class EmptyInput(BaseModel):
    pass


class OneTurnModel:
    def __init__(self, turn: ModelTurn) -> None:
        self.turn = turn
        self.calls = 0

    def next_action(self, _context):
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("unexpected second model call")
        return self.turn


def test_input_token_overage_fails_durably_before_side_effect(tmp_path: Path) -> None:
    calls = {"count": 0}

    def read(_value: EmptyInput) -> ToolExecutionResult:
        calls["count"] += 1
        return ToolExecutionResult(output={"ok": True})

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="bounded.read",
            description="Read-only test tool.",
            input_model=EmptyInput,
            handler=read,
        )
    )
    store = AgentRunStore(Database(tmp_path / "workbench.sqlite3"))
    runtime = AgentRuntime(
        store=store,
        model=OneTurnModel(
            ModelTurn(
                action=NextAction(action="tool", tool_name="bounded.read", arguments={}),
                input_tokens=101,
                output_tokens=1,
                model_name="fake-model",
            )
        ),
        tools=registry,
        permissions=RuleBasedPermissionPolicy(),
        budget=RunBudget(max_input_tokens=100),
    )

    outcome = runtime.start("respect token budget")

    assert outcome.state is AgentRunState.failed
    assert outcome.error_category == "budget_exhausted"
    assert outcome.usage.input_tokens == 101
    assert calls["count"] == 0


def test_context_builder_bounds_large_old_tool_output(tmp_path: Path) -> None:
    store = AgentRunStore(Database(tmp_path / "workbench.sqlite3"))
    run = store.create_run(goal="bound context", budget=RunBudget())
    store.append_step(
        run.id,
        kind="tool",
        status="succeeded",
        tool_name="large_result.read",
        output_json={"payload": "x" * 20_000},
        evidence_refs=["evidence:large:1"],
    )
    builder = DefaultContextBuilder(max_recent_steps=5, max_step_chars=256)

    context = builder.build(
        goal=run.goal,
        run_id=run.id,
        steps=store.list_steps(run.id),
        tools=[],
        usage={"model_calls": 0, "input_tokens": 0, "output_tokens": 0},
        remaining_budget={"steps": 5, "model_calls": 2, "input_tokens": 1000, "output_tokens": 1000, "wall_time_seconds": 5.0},
    )

    assert len(context.recent_steps) == 1
    summary = context.recent_steps[0].output_summary
    assert summary is not None
    assert len(summary) <= 256
    assert summary.endswith("…[truncated]")
    assert context.recent_steps[0].evidence_refs == ["evidence:large:1"]
