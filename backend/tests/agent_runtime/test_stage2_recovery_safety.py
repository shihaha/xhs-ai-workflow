from pathlib import Path

from pydantic import BaseModel

from backend.app.agent_runtime import (
    AgentRunState,
    AgentRunStore,
    AgentRuntime,
    ModelTurn,
    NextAction,
    PermissionDecision,
    RuleBasedPermissionPolicy,
    RunBudget,
    ToolExecutionResult,
    ToolRegistry,
    ToolSpec,
)
from backend.app.db import Database


class EmptyInput(BaseModel):
    pass


class IncrementInput(BaseModel):
    amount: int = 1


class SequenceModel:
    def __init__(self, *turns: ModelTurn) -> None:
        self.turns = list(turns)
        self.calls = 0

    def next_action(self, _context):
        self.calls += 1
        if not self.turns:
            raise AssertionError("unexpected extra model call")
        return self.turns.pop(0)


def _turn(action: NextAction) -> ModelTurn:
    return ModelTurn(
        action=action,
        input_tokens=3,
        output_tokens=1,
        model_name="stage2-sequence",
    )


def _store(tmp_path: Path) -> AgentRunStore:
    return AgentRunStore(Database(tmp_path / "workbench.sqlite3"))


def test_repeated_idempotent_tool_call_id_reuses_committed_result(tmp_path: Path) -> None:
    calls = {"count": 0}

    def read(_value: EmptyInput) -> ToolExecutionResult:
        calls["count"] += 1
        return ToolExecutionResult(
            output={"value": "stable"}, evidence_refs=["evidence:stable:1"]
        )

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="stable.read",
            description="Deterministic read used to prove replay suppression.",
            input_model=EmptyInput,
            handler=read,
            idempotent=True,
        )
    )
    store = _store(tmp_path)
    runtime = AgentRuntime(
        store=store,
        model=SequenceModel(
            _turn(
                NextAction(
                    action="tool",
                    tool_name="stable.read",
                    tool_call_id="same-call",
                    arguments={},
                )
            ),
            _turn(
                NextAction(
                    action="tool",
                    tool_name="stable.read",
                    tool_call_id="same-call",
                    arguments={},
                )
            ),
            _turn(NextAction(action="finish", final_output={"ok": True})),
        ),
        tools=registry,
        permissions=RuleBasedPermissionPolicy(),
    )

    outcome = runtime.start("never execute the same idempotent call twice")

    assert outcome.state is AgentRunState.succeeded
    assert calls["count"] == 1
    tool_steps = [step for step in store.list_steps(outcome.run_id) if step.kind == "tool"]
    assert [step.status for step in tool_steps] == ["succeeded", "reused"]
    assert tool_steps[1].output_json == tool_steps[0].output_json
    assert tool_steps[1].evidence_refs_json == ["evidence:stable:1"]


def test_repeated_non_idempotent_tool_call_id_never_executes_twice(tmp_path: Path) -> None:
    calls = {"count": 0}

    def mutate(value: IncrementInput) -> ToolExecutionResult:
        calls["count"] += 1
        return ToolExecutionResult(output={"amount": value.amount})

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="counter.write",
            description="State-changing test tool.",
            input_model=IncrementInput,
            handler=mutate,
            read_only=False,
            idempotent=False,
        )
    )
    runtime = AgentRuntime(
        store=_store(tmp_path),
        model=SequenceModel(
            _turn(
                NextAction(
                    action="tool",
                    tool_name="counter.write",
                    tool_call_id="write-once",
                    arguments={"amount": 1},
                )
            ),
            _turn(
                NextAction(
                    action="tool",
                    tool_name="counter.write",
                    tool_call_id="write-once",
                    arguments={"amount": 1},
                )
            ),
        ),
        tools=registry,
        permissions=RuleBasedPermissionPolicy(
            overrides={"counter.write": PermissionDecision.allow}
        ),
    )

    outcome = runtime.start("block non-idempotent replay")

    assert outcome.state is AgentRunState.failed
    assert outcome.error_category == "duplicate_non_idempotent_call"
    assert calls["count"] == 1


def test_reconstructed_runtime_uses_persisted_run_budget(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = store.create_run(
        goal="persist the original budget",
        budget=RunBudget(max_model_calls=1),
    )
    store.add_model_usage(
        run.id,
        _turn(NextAction(action="finish", final_output={"old": True})),
    )
    recovered = store.recover_interrupted(run.id)
    assert recovered.state == AgentRunState.needs_human.value
    assert recovered.error_category == "interrupted"

    model = SequenceModel(
        _turn(NextAction(action="finish", final_output={"should_not_run": True}))
    )
    runtime = AgentRuntime(
        store=store,
        model=model,
        tools=ToolRegistry(),
        permissions=RuleBasedPermissionPolicy(),
        budget=RunBudget(max_model_calls=99),
    )

    outcome = runtime.resume_interrupted(run.id)

    assert outcome.state is AgentRunState.failed
    assert outcome.error_category == "budget_exhausted"
    assert outcome.usage.model_calls == 1
    assert model.calls == 0


def test_uncertain_started_side_effect_is_not_replayed_after_explicit_resume(tmp_path: Path) -> None:
    calls = {"count": 0}

    def mutate(value: IncrementInput) -> ToolExecutionResult:
        calls["count"] += 1
        return ToolExecutionResult(output={"amount": value.amount})

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="external.write",
            description="Simulated non-idempotent external side effect.",
            input_model=IncrementInput,
            handler=mutate,
            read_only=False,
            external_side_effect=True,
            idempotent=False,
        )
    )
    store = _store(tmp_path)
    run = store.create_run(goal="recover uncertain write", budget=RunBudget())
    store.append_step(
        run.id,
        kind="tool",
        status="started",
        tool_name="external.write",
        tool_call_id="uncertain-write",
        input_json={
            "arguments": {"amount": 1},
            "idempotent": False,
            "timeout_seconds": 30.0,
        },
    )
    runtime = AgentRuntime(
        store=store,
        model=SequenceModel(
            _turn(
                NextAction(
                    action="tool",
                    tool_name="external.write",
                    tool_call_id="uncertain-write",
                    arguments={"amount": 1},
                )
            )
        ),
        tools=registry,
        permissions=RuleBasedPermissionPolicy(
            overrides={"external.write": PermissionDecision.allow}
        ),
    )

    recovered = runtime.recover_interrupted(run.id)
    assert recovered.state is AgentRunState.needs_human
    assert recovered.error_category == "uncertain_tool_side_effect"

    outcome = runtime.resume_interrupted(run.id, acknowledge_uncertain=True)

    assert outcome.state is AgentRunState.needs_human
    assert outcome.error_category == "uncertain_tool_side_effect"
    assert calls["count"] == 0


def test_side_effect_exception_becomes_needs_human_not_false_failure(tmp_path: Path) -> None:
    calls = {"count": 0}

    def mutate_then_lose_ack(value: IncrementInput) -> ToolExecutionResult:
        calls["count"] += value.amount
        raise RuntimeError("simulated acknowledgement loss")

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="external.uncertain",
            description="Simulated external write with acknowledgement loss.",
            input_model=IncrementInput,
            handler=mutate_then_lose_ack,
            read_only=False,
            external_side_effect=True,
            idempotent=False,
        )
    )
    store = _store(tmp_path)
    runtime = AgentRuntime(
        store=store,
        model=SequenceModel(
            _turn(
                NextAction(
                    action="tool",
                    tool_name="external.uncertain",
                    tool_call_id="lost-ack",
                    arguments={"amount": 1},
                )
            )
        ),
        tools=registry,
        permissions=RuleBasedPermissionPolicy(
            overrides={"external.uncertain": PermissionDecision.allow}
        ),
    )

    outcome = runtime.start("do not invent failure after uncertain external write")

    assert outcome.state is AgentRunState.needs_human
    assert outcome.error_category == "uncertain_tool_side_effect"
    assert calls["count"] == 1
    tool_steps = [step for step in store.list_steps(outcome.run_id) if step.kind == "tool"]
    assert len(tool_steps) == 1
    assert tool_steps[0].status == "uncertain"
    assert tool_steps[0].error_category == "uncertain_tool_side_effect"
