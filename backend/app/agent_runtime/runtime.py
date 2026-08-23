"""Canonical bounded, durable, fail-closed Agent Runtime.

Stage 2 recovery, replay, and uncertain-side-effect semantics are the public
runtime behavior.  The original Stage 1 loop lives in the private
``_runtime_core`` module only as an implementation scaffold.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from backend.app.agent_runtime._runtime_core import AgentRuntime as _BaseAgentRuntime
from backend.app.agent_runtime.events import RuntimeEventType
from backend.app.agent_runtime.tools import (
    ToolDomainFailureError,
    ToolNeedsHumanError,
    ToolTimeoutError,
)
from backend.app.agent_runtime.types import (
    AgentRunState,
    AgentStepKind,
    NextAction,
    RunBudget,
    RuntimeOutcome,
)


class AgentRuntime(_BaseAgentRuntime):
    """Fail-closed runtime proven by the Stage 2 acceptance suite.

    Persisted run budgets are scoped with a ContextVar rather than by mutating a
    shared ``self.budget`` value.  This keeps reconstructed/resumed runs bound to
    their original budget without leaking that budget into another concurrent
    run using the same runtime instance.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._budget_scope: ContextVar[RunBudget | None] = ContextVar(
            f"agent_runtime_budget_{id(self)}",
            default=None,
        )
        self._default_budget = RunBudget()
        super().__init__(*args, **kwargs)

    @property
    def budget(self) -> RunBudget:
        scoped = self._budget_scope.get()
        return scoped if scoped is not None else self._default_budget

    @budget.setter
    def budget(self, value: RunBudget) -> None:
        self._default_budget = value

    def _drive(self, run_id: str) -> RuntimeOutcome:
        # A reconstructed/resumed process must honor the budget persisted with
        # the run, not constructor defaults that may have changed after restart.
        run = self.store.get_run(run_id)
        persisted_budget = RunBudget.model_validate(run.budget_json)
        token = self._budget_scope.set(persisted_budget)
        try:
            return super()._drive(run_id)
        finally:
            self._budget_scope.reset(token)

    def resume_interrupted(
        self,
        run_id: str,
        *,
        acknowledge_uncertain: bool = False,
    ) -> RuntimeOutcome:
        """Explicitly resume a recovered run without replaying an old tool call.

        Approval-gated runs must continue through ``resume``.  An uncertain
        in-flight side effect additionally requires an explicit acknowledgement.
        The old tool call is never executed by this method.
        """

        run = self.store.get_run(run_id)
        if run.state != AgentRunState.needs_human.value:
            raise ValueError("only needs_human runs can resume after interruption")
        if self.store.pending_human_action(run_id) is not None:
            raise ValueError("approval-gated run must use resume()")
        if run.error_category not in {"interrupted", "uncertain_tool_side_effect"}:
            raise ValueError("run is not in an interruption recovery state")
        if run.error_category == "uncertain_tool_side_effect" and not acknowledge_uncertain:
            raise ValueError("uncertain side effect requires explicit acknowledgement")

        self.store.set_state(run_id, AgentRunState.running)
        checkpoint = self.store.checkpoint(
            run_id,
            state_json={
                "state": AgentRunState.running.value,
                "recovery": "explicit_resume_without_replay",
                "uncertain_acknowledged": acknowledge_uncertain,
            },
        )
        self._emit(
            RuntimeEventType.checkpoint_committed,
            run_id,
            step_index=checkpoint.step_index,
            payload={"state": AgentRunState.running.value, "recovery": True},
        )
        return self._drive(run_id)

    def _execute_tool(
        self,
        run_id: str,
        action: NextAction,
        tool: Any,
        validated_json: dict[str, Any],
    ) -> RuntimeOutcome | None:
        validated = self.tools.validate(tool, validated_json)

        duplicate = self._duplicate_tool_call(
            run_id,
            action=action,
            tool=tool,
            validated_json=validated_json,
        )
        if duplicate is not False:
            return duplicate

        step = self.store.append_step(
            run_id,
            kind=AgentStepKind.tool.value,
            status="started",
            tool_name=tool.name,
            tool_call_id=action.tool_call_id,
            input_json={
                "arguments": validated_json,
                "idempotent": tool.idempotent,
                "timeout_seconds": tool.timeout_seconds,
            },
        )
        self._emit(
            RuntimeEventType.before_tool,
            run_id,
            step_index=step.step_index,
            tool_name=tool.name,
            tool_call_id=action.tool_call_id,
            payload={"timeout_seconds": tool.timeout_seconds},
        )

        try:
            result = self.tools.execute_validated(
                tool,
                validated,
                run_id=run_id,
                tool_call_id=action.tool_call_id,
            )
            persisted = result.model_dump(mode="json")
        except ToolNeedsHumanError as exc:
            return self._domain_needs_human(run_id, step, action, tool, exc)
        except ToolDomainFailureError as exc:
            persisted = exc.result.model_dump(mode="json") if exc.result is not None else None
            evidence_refs = exc.result.evidence_refs if exc.result is not None else None
            self.store.update_step(
                step.id,
                status="failed",
                output_json=persisted,
                evidence_refs=evidence_refs,
                error_category=exc.category,
                error_detail=exc.detail,
            )
            self._emit(
                RuntimeEventType.after_tool,
                run_id,
                step_index=step.step_index,
                tool_name=tool.name,
                tool_call_id=action.tool_call_id,
                payload={"status": "failed", "error_category": exc.category},
            )
            return self._fail(run_id, exc.category, exc.detail)
        except ToolTimeoutError as exc:
            detail = str(exc)
            if self._tool_may_have_side_effect(tool):
                return self._uncertain_tool_side_effect(
                    run_id,
                    step=step,
                    action=action,
                    tool=tool,
                    detail=detail,
                )
            self.store.update_step(
                step.id,
                status="failed",
                error_category="tool_timeout",
                error_detail=detail,
            )
            self._emit(
                RuntimeEventType.after_tool,
                run_id,
                step_index=step.step_index,
                tool_name=tool.name,
                tool_call_id=action.tool_call_id,
                payload={"status": "failed", "error_category": "tool_timeout"},
            )
            return self._fail(run_id, "tool_timeout", detail)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            if self._tool_may_have_side_effect(tool):
                return self._uncertain_tool_side_effect(
                    run_id,
                    step=step,
                    action=action,
                    tool=tool,
                    detail=detail,
                )
            self.store.update_step(
                step.id,
                status="failed",
                error_category="tool_execution_failed",
                error_detail=detail,
            )
            self._emit(
                RuntimeEventType.after_tool,
                run_id,
                step_index=step.step_index,
                tool_name=tool.name,
                tool_call_id=action.tool_call_id,
                payload={"status": "failed", "error_category": "tool_execution_failed"},
            )
            return self._fail(run_id, "tool_execution_failed", detail)

        self.store.update_step(
            step.id,
            status="succeeded",
            output_json=persisted,
            evidence_refs=result.evidence_refs,
        )
        self._emit(
            RuntimeEventType.after_tool,
            run_id,
            step_index=step.step_index,
            tool_name=tool.name,
            tool_call_id=action.tool_call_id,
            payload={"status": "succeeded", "evidence_refs": result.evidence_refs},
        )
        checkpoint = self.store.checkpoint(
            run_id,
            state_json={
                "state": AgentRunState.running.value,
                "last_committed_tool_call_id": action.tool_call_id,
                "last_committed_tool": tool.name,
            },
        )
        self._emit(
            RuntimeEventType.checkpoint_committed,
            run_id,
            step_index=checkpoint.step_index,
            tool_name=tool.name,
            tool_call_id=action.tool_call_id,
            payload={"state": AgentRunState.running.value},
        )
        return None

    def _duplicate_tool_call(
        self,
        run_id: str,
        *,
        action: NextAction,
        tool: Any,
        validated_json: dict[str, Any],
    ) -> RuntimeOutcome | None | bool:
        matches = [
            step
            for step in self.store.list_steps(run_id)
            if step.kind == AgentStepKind.tool.value
            and step.tool_call_id == action.tool_call_id
        ]
        if not matches:
            return False

        previous = matches[-1]
        previous_arguments = (previous.input_json or {}).get("arguments")
        if previous.tool_name != tool.name or previous_arguments != validated_json:
            return self._fail(
                run_id,
                "tool_call_id_conflict",
                "A tool_call_id was reused with different tool identity or arguments.",
            )
        if previous.status in {"started", "uncertain", "needs_human"}:
            self.store.set_state(
                run_id,
                AgentRunState.needs_human,
                error_category="uncertain_tool_side_effect",
                error_detail="A prior execution of this tool_call_id has no safe committed outcome.",
            )
            return self._outcome(run_id)
        if previous.status in {"succeeded", "reused"}:
            if not tool.idempotent:
                return self._fail(
                    run_id,
                    "duplicate_non_idempotent_call",
                    "Automatic replay of a committed non-idempotent tool call is forbidden.",
                )
            reused = self.store.append_step(
                run_id,
                kind=AgentStepKind.tool.value,
                status="reused",
                tool_name=tool.name,
                tool_call_id=action.tool_call_id,
                input_json={
                    "arguments": validated_json,
                    "idempotent": True,
                    "reused_from_step": previous.step_index,
                },
                output_json=previous.output_json,
                evidence_refs=list(previous.evidence_refs_json or []),
            )
            checkpoint = self.store.checkpoint(
                run_id,
                state_json={
                    "state": AgentRunState.running.value,
                    "reused_tool_call_id": action.tool_call_id,
                    "reused_from_step": previous.step_index,
                },
            )
            self._emit(
                RuntimeEventType.after_tool,
                run_id,
                step_index=reused.step_index,
                tool_name=tool.name,
                tool_call_id=action.tool_call_id,
                payload={"status": "reused", "executed": False},
            )
            self._emit(
                RuntimeEventType.checkpoint_committed,
                run_id,
                step_index=checkpoint.step_index,
                tool_name=tool.name,
                tool_call_id=action.tool_call_id,
                payload={"state": AgentRunState.running.value, "reused": True},
            )
            return None
        return self._fail(
            run_id,
            "duplicate_tool_call_blocked",
            f"Previous tool_call_id outcome is not replay-safe: {previous.status}.",
        )

    def _domain_needs_human(
        self,
        run_id: str,
        step: Any,
        action: NextAction,
        tool: Any,
        exc: ToolNeedsHumanError,
    ) -> RuntimeOutcome:
        persisted = exc.result.model_dump(mode="json") if exc.result is not None else None
        evidence_refs = exc.result.evidence_refs if exc.result is not None else None
        self.store.update_step(
            step.id,
            status="needs_human",
            output_json=persisted,
            evidence_refs=evidence_refs,
            error_category=exc.category,
            error_detail=exc.detail,
        )
        self.store.set_state(
            run_id,
            AgentRunState.needs_human,
            error_category=exc.category,
            error_detail=exc.detail,
        )
        checkpoint = self.store.checkpoint(
            run_id,
            state_json={
                "state": AgentRunState.needs_human.value,
                "tool_call_id": action.tool_call_id,
                "domain_category": exc.category,
            },
        )
        self._emit(
            RuntimeEventType.after_tool,
            run_id,
            step_index=step.step_index,
            tool_name=tool.name,
            tool_call_id=action.tool_call_id,
            payload={"status": "needs_human", "error_category": exc.category},
        )
        self._emit(
            RuntimeEventType.checkpoint_committed,
            run_id,
            step_index=checkpoint.step_index,
            tool_name=tool.name,
            tool_call_id=action.tool_call_id,
            payload={"state": AgentRunState.needs_human.value},
        )
        return self._outcome(run_id)

    def _uncertain_tool_side_effect(
        self,
        run_id: str,
        *,
        step: Any,
        action: NextAction,
        tool: Any,
        detail: str,
    ) -> RuntimeOutcome:
        self.store.update_step(
            step.id,
            status="uncertain",
            error_category="uncertain_tool_side_effect",
            error_detail=detail,
        )
        self.store.set_state(
            run_id,
            AgentRunState.needs_human,
            error_category="uncertain_tool_side_effect",
            error_detail=(
                "Tool execution ended without a provably safe outcome; automatic replay is forbidden. "
                + detail
            ),
        )
        checkpoint = self.store.checkpoint(
            run_id,
            state_json={
                "state": AgentRunState.needs_human.value,
                "uncertain_tool_call_id": action.tool_call_id,
                "automatic_replay_forbidden": True,
            },
        )
        self._emit(
            RuntimeEventType.after_tool,
            run_id,
            step_index=step.step_index,
            tool_name=tool.name,
            tool_call_id=action.tool_call_id,
            payload={"status": "uncertain", "error_category": "uncertain_tool_side_effect"},
        )
        self._emit(
            RuntimeEventType.checkpoint_committed,
            run_id,
            step_index=checkpoint.step_index,
            tool_name=tool.name,
            tool_call_id=action.tool_call_id,
            payload={"state": AgentRunState.needs_human.value},
        )
        return self._outcome(run_id)

    @staticmethod
    def _tool_may_have_side_effect(tool: Any) -> bool:
        return bool(tool.external_side_effect or tool.destructive or not tool.read_only)
