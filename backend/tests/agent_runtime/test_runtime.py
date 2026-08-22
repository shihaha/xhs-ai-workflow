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


class EchoInput(BaseModel):
    text: str


class IncrementInput(BaseModel):
    amount: int = 1


class SequenceModel:
    def __init__(self, *turns: ModelTurn) -> None:
        self.turns = list(turns)
        self.contexts = []

    def next_action(self, context):
        self.contexts.append(context)
        if not self.turns:
            raise AssertionError("model called more times than expected")
        return self.turns.pop(0)


def _turn(action: NextAction, *, input_tokens: int = 10, output_tokens: int = 2) -> ModelTurn:
    return ModelTurn(
        action=action,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model_name="fake-model",
    )


def _store(tmp_path: Path) -> AgentRunStore:
    return AgentRunStore(Database(tmp_path / "workbench.sqlite3"))


def test_read_tool_then_finish_persists_trace_and_usage(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="echo.read",
            description="Echo bounded test input.",
            input_model=EchoInput,
            handler=lambda value: ToolExecutionResult(
                output={"echo": value.text},
                evidence_refs=["evidence:test:1"],
            ),
        )
    )
    model = SequenceModel(
        _turn(NextAction(action="tool", tool_name="echo.read", arguments={"text": "hello"})),
        _turn(NextAction(action="finish", final_output={"answer": "hello"})),
    )
    store = _store(tmp_path)
    runtime = AgentRuntime(
        store=store,
        model=model,
        tools=registry,
        permissions=RuleBasedPermissionPolicy(),
    )

    outcome = runtime.start("echo one trusted value")

    assert outcome.state is AgentRunState.succeeded
    assert outcome.final_output == {"answer": "hello"}
    assert outcome.usage.model_calls == 2
    assert outcome.usage.input_tokens == 20
    steps = store.list_steps(outcome.run_id)
    tool_steps = [step for step in steps if step.kind == "tool"]
    assert len(tool_steps) == 1
    assert tool_steps[0].status == "succeeded"
    assert tool_steps[0].evidence_refs_json == ["evidence:test:1"]
    assert all("thinking" not in str(step.output_json).lower() for step in steps)


def test_approval_pauses_without_side_effect_then_resumes_explicitly(tmp_path: Path) -> None:
    counter = {"value": 0}

    def increment(value: IncrementInput) -> ToolExecutionResult:
        counter["value"] += value.amount
        return ToolExecutionResult(output={"value": counter["value"]})

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="counter.increment",
            description="Mutate a local test counter.",
            input_model=IncrementInput,
            handler=increment,
            read_only=False,
            requires_approval=True,
            idempotent=False,
        )
    )
    model = SequenceModel(
        _turn(
            NextAction(
                action="tool",
                tool_name="counter.increment",
                arguments={"amount": 3},
                tool_call_id="increment-1",
            )
        ),
        _turn(NextAction(action="finish", final_output={"counter": 3})),
    )
    store = _store(tmp_path)
    runtime = AgentRuntime(
        store=store,
        model=model,
        tools=registry,
        permissions=RuleBasedPermissionPolicy(),
    )

    paused = runtime.start("increment after human approval")

    assert paused.state is AgentRunState.needs_human
    assert paused.needs_human_action_id is not None
    assert counter["value"] == 0

    finished = runtime.resume(paused.run_id, approved=True, note="test approval")

    assert finished.state is AgentRunState.succeeded
    assert finished.final_output == {"counter": 3}
    assert counter["value"] == 3


def test_permission_deny_never_executes_tool(tmp_path: Path) -> None:
    calls = {"count": 0}

    def mutate(value: IncrementInput) -> ToolExecutionResult:
        calls["count"] += 1
        return ToolExecutionResult(output={"amount": value.amount})

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="danger.write",
            description="Denied test mutation.",
            input_model=IncrementInput,
            handler=mutate,
            read_only=False,
        )
    )
    runtime = AgentRuntime(
        store=_store(tmp_path),
        model=SequenceModel(
            _turn(NextAction(action="tool", tool_name="danger.write", arguments={"amount": 1}))
        ),
        tools=registry,
        permissions=RuleBasedPermissionPolicy(
            overrides={"danger.write": PermissionDecision.deny}
        ),
    )

    outcome = runtime.start("try denied mutation")

    assert outcome.state is AgentRunState.failed
    assert outcome.error_category == "permission_denied"
    assert calls["count"] == 0


def test_unknown_tool_fails_closed(tmp_path: Path) -> None:
    runtime = AgentRuntime(
        store=_store(tmp_path),
        model=SequenceModel(
            _turn(NextAction(action="tool", tool_name="invented.tool", arguments={}))
        ),
        tools=ToolRegistry(),
        permissions=RuleBasedPermissionPolicy(),
    )

    outcome = runtime.start("never invent tools")

    assert outcome.state is AgentRunState.failed
    assert outcome.error_category == "tool_unavailable"


def test_model_call_budget_allows_last_call_but_prevents_another(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="echo.read",
            description="Echo.",
            input_model=EchoInput,
            handler=lambda value: ToolExecutionResult(output={"echo": value.text}),
        )
    )
    runtime = AgentRuntime(
        store=_store(tmp_path),
        model=SequenceModel(
            _turn(NextAction(action="tool", tool_name="echo.read", arguments={"text": "one"}))
        ),
        tools=registry,
        permissions=RuleBasedPermissionPolicy(),
        budget=RunBudget(max_model_calls=1),
    )

    outcome = runtime.start("one model call only")

    assert outcome.state is AgentRunState.failed
    assert outcome.error_category == "budget_exhausted"
    assert outcome.usage.model_calls == 1
    assert any(step.kind == "tool" and step.status == "succeeded" for step in runtime.store.list_steps(outcome.run_id))
