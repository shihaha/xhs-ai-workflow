import time
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from backend.app.agent_runtime import (
    AgentRunState,
    AgentRunStore,
    AgentRuntime,
    ModelTurn,
    NextAction,
    RecordingEventSink,
    RuleBasedPermissionPolicy,
    RuntimeEventType,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolRegistry,
    ToolSpec,
)
from backend.app.agent_runtime.tools import ToolInputError
from backend.app.db import Database


class EchoInput(BaseModel):
    text: str


class EmptyInput(BaseModel):
    pass


class SequenceModel:
    def __init__(self, *turns: ModelTurn) -> None:
        self.turns = list(turns)

    def next_action(self, _context):
        if not self.turns:
            raise AssertionError("unexpected extra model call")
        return self.turns.pop(0)


def _turn(action: NextAction) -> ModelTurn:
    return ModelTurn(action=action, input_tokens=7, output_tokens=2, model_name="fake-model")


def _store(tmp_path: Path) -> AgentRunStore:
    return AgentRunStore(Database(tmp_path / "workbench.sqlite3"))


def test_lifecycle_events_cover_model_tool_permission_checkpoint_and_finish(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="echo.read",
            description="Read deterministic test data.",
            input_model=EchoInput,
            handler=lambda value: ToolExecutionResult(
                output={"echo": value.text}, evidence_refs=["evidence:test:event"]
            ),
        )
    )
    sink = RecordingEventSink()
    runtime = AgentRuntime(
        store=_store(tmp_path),
        model=SequenceModel(
            _turn(NextAction(action="tool", tool_name="echo.read", arguments={"text": "hello"})),
            _turn(NextAction(action="finish", final_output={"answer": "hello"})),
        ),
        tools=registry,
        permissions=RuleBasedPermissionPolicy(),
        event_sink=sink,
    )

    outcome = runtime.start("exercise lifecycle events")

    assert outcome.state is AgentRunState.succeeded
    kinds = [event.event_type for event in sink.events]
    required = {
        RuntimeEventType.run_started,
        RuntimeEventType.before_model,
        RuntimeEventType.after_model,
        RuntimeEventType.before_tool_validate,
        RuntimeEventType.after_tool_validate,
        RuntimeEventType.permission_decision,
        RuntimeEventType.before_tool,
        RuntimeEventType.after_tool,
        RuntimeEventType.checkpoint_committed,
        RuntimeEventType.run_finished,
    }
    assert required.issubset(set(kinds))
    assert kinds[0] is RuntimeEventType.run_started
    assert kinds[-1] is RuntimeEventType.run_finished
    assert not any("arguments" in event.payload for event in sink.events)


def test_event_sink_failure_cannot_change_successful_runtime_outcome(tmp_path: Path) -> None:
    class BrokenSink:
        def emit(self, _event) -> None:
            raise RuntimeError("telemetry unavailable")

    runtime = AgentRuntime(
        store=_store(tmp_path),
        model=SequenceModel(_turn(NextAction(action="finish", final_output={"ok": True}))),
        tools=ToolRegistry(),
        permissions=RuleBasedPermissionPolicy(),
        event_sink=BrokenSink(),
    )

    outcome = runtime.start("telemetry must not own truth")

    assert outcome.state is AgentRunState.succeeded
    assert outcome.final_output == {"ok": True}


def test_cooperative_tool_deadline_is_categorized_and_persisted(tmp_path: Path) -> None:
    def slow_read(_value: EmptyInput, context: ToolExecutionContext) -> ToolExecutionResult:
        time.sleep(0.01)
        context.check_deadline()
        return ToolExecutionResult(output={"unexpected": True})

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="slow.read",
            description="Read-only timeout test.",
            input_model=EmptyInput,
            handler=slow_read,
            timeout_seconds=0.001,
        )
    )
    store = _store(tmp_path)
    runtime = AgentRuntime(
        store=store,
        model=SequenceModel(_turn(NextAction(action="tool", tool_name="slow.read", arguments={}))),
        tools=registry,
        permissions=RuleBasedPermissionPolicy(),
    )

    outcome = runtime.start("timeout safely")

    assert outcome.state is AgentRunState.failed
    assert outcome.error_category == "tool_timeout"
    tool_steps = [step for step in store.list_steps(outcome.run_id) if step.kind == "tool"]
    assert len(tool_steps) == 1
    assert tool_steps[0].status == "failed"
    assert tool_steps[0].error_category == "tool_timeout"


def test_model_action_and_tool_arguments_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        NextAction.model_validate(
            {
                "action": "finish",
                "final_output": {"ok": True},
                "unexpected_authority": "approve_everything",
            }
        )

    registry = ToolRegistry()
    tool = ToolSpec(
        name="echo.read",
        description="Strict argument test.",
        input_model=EchoInput,
        handler=lambda value: ToolExecutionResult(output={"echo": value.text}),
    )
    registry.register(tool)

    with pytest.raises(ToolInputError, match="unknown arguments"):
        registry.validate(tool, {"text": "hello", "unexpected": True})
