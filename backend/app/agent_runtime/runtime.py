"""Bounded, durable, fail-closed Agent Runtime loop."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from backend.app.agent_runtime.context import DefaultContextBuilder
from backend.app.agent_runtime.model import ModelDriver
from backend.app.agent_runtime.permissions import PermissionPolicy, PermissionRequest
from backend.app.agent_runtime.persistence import AgentRunRecord, AgentRunStore
from backend.app.agent_runtime.tools import (
    ToolInputError,
    ToolRegistry,
    ToolUnavailableError,
)
from backend.app.agent_runtime.types import (
    AgentRunState,
    AgentStepKind,
    NextAction,
    PermissionDecision,
    RunBudget,
    RuntimeOutcome,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AgentRuntime:
    def __init__(
        self,
        *,
        store: AgentRunStore,
        model: ModelDriver,
        tools: ToolRegistry,
        permissions: PermissionPolicy,
        context_builder: DefaultContextBuilder | None = None,
        budget: RunBudget | None = None,
        prompt_version: str = "agent-runtime-spike-v1",
    ) -> None:
        self.store = store
        self.model = model
        self.tools = tools
        self.permissions = permissions
        self.context_builder = context_builder or DefaultContextBuilder()
        self.budget = budget or RunBudget()
        self.prompt_version = prompt_version

    def start(self, goal: str) -> RuntimeOutcome:
        run = self.store.create_run(
            goal=goal,
            budget=self.budget,
            prompt_version=self.prompt_version,
        )
        return self._drive(run.id)

    def resume(self, run_id: str, *, approved: bool, note: str | None = None) -> RuntimeOutcome:
        run = self.store.get_run(run_id)
        if run.state != AgentRunState.needs_human.value:
            raise ValueError("only needs_human runs can be resumed")
        human = self.store.pending_human_action(run_id)
        if human is None:
            raise ValueError("run has no pending human action")
        self.store.resolve_human_action(
            human.id,
            approved=approved,
            note=note,
            resume_run=approved,
        )
        if not approved:
            return self._fail(
                run_id,
                "human_denied",
                f"Human denied tool call {human.tool_call_id} ({human.tool_name}).",
            )

        try:
            action = NextAction.model_validate(human.request_json["action"])
            assert action.tool_name is not None
            tool = self.tools.resolve(action.tool_name)
            validated = self.tools.validate(tool, action.arguments)
        except (KeyError, AssertionError, ToolUnavailableError, ToolInputError, ValueError) as exc:
            return self._fail(run_id, "resume_validation_failed", str(exc))

        outcome = self._execute_tool(run_id, action, tool, validated.model_dump(mode="json"))
        if outcome is not None:
            return outcome
        return self._drive(run_id)

    def recover_interrupted(self, run_id: str) -> RuntimeOutcome:
        self.store.recover_interrupted(run_id)
        return self._outcome(run_id)

    def _drive(self, run_id: str) -> RuntimeOutcome:
        while True:
            run = self.store.get_run(run_id)
            exceeded = self._budget_failure(run, before_model=True)
            if exceeded is not None:
                return self._fail(run_id, "budget_exhausted", exceeded)

            context = self.context_builder.build(
                goal=run.goal,
                run_id=run.id,
                steps=self.store.list_steps(run_id),
                tools=self.tools.public_definitions(),
                usage={
                    "model_calls": run.model_calls,
                    "input_tokens": run.input_tokens,
                    "output_tokens": run.output_tokens,
                },
                remaining_budget={
                    "steps": max(0, self.budget.max_steps - run.step_count),
                    "model_calls": max(0, self.budget.max_model_calls - run.model_calls),
                    "input_tokens": max(0, self.budget.max_input_tokens - run.input_tokens),
                    "output_tokens": max(0, self.budget.max_output_tokens - run.output_tokens),
                    "wall_time_seconds": max(
                        0.0,
                        self.budget.max_wall_time_seconds
                        - (_utcnow() - run.created_at).total_seconds(),
                    ),
                },
            )

            try:
                turn = self.model.next_action(context)
            except Exception as exc:
                return self._fail(run_id, "model_error", f"{type(exc).__name__}: {exc}")

            self.store.add_model_usage(run_id, turn)
            self.store.append_step(
                run_id,
                kind=AgentStepKind.model.value,
                status="succeeded",
                tool_name=turn.action.tool_name,
                tool_call_id=turn.action.tool_call_id,
                output_json={"action": turn.action.model_dump(mode="json")},
            )

            run = self.store.get_run(run_id)
            exceeded = self._budget_failure(run, before_model=False)
            if exceeded is not None:
                return self._fail(run_id, "budget_exhausted", exceeded)

            action = turn.action
            if action.action == "finish":
                assert action.final_output is not None
                self.store.append_step(
                    run_id,
                    kind=AgentStepKind.output.value,
                    status="succeeded",
                    output_json=action.final_output,
                )
                self.store.set_state(
                    run_id,
                    AgentRunState.succeeded,
                    final_output=action.final_output,
                )
                self.store.checkpoint(
                    run_id,
                    state_json={"state": AgentRunState.succeeded.value, "final_output": action.final_output},
                )
                return self._outcome(run_id)

            assert action.tool_name is not None
            try:
                tool = self.tools.resolve(action.tool_name)
                validated = self.tools.validate(tool, action.arguments)
            except ToolUnavailableError as exc:
                return self._fail(run_id, "tool_unavailable", str(exc))
            except ToolInputError as exc:
                return self._fail(run_id, "tool_input_invalid", str(exc))

            permission = self.permissions.decide(
                PermissionRequest(
                    run_id=run_id,
                    tool_call_id=action.tool_call_id,
                    tool=tool,
                    arguments=validated.model_dump(mode="json"),
                )
            )
            self.store.record_permission(
                run_id=run_id,
                tool_call_id=action.tool_call_id,
                tool_name=tool.name,
                decision=permission.decision.value,
                reason=permission.reason,
            )
            self.store.append_step(
                run_id,
                kind=AgentStepKind.permission.value,
                status=permission.decision.value,
                tool_name=tool.name,
                tool_call_id=action.tool_call_id,
                input_json={"arguments": validated.model_dump(mode="json")},
                output_json={"decision": permission.decision.value, "reason": permission.reason},
            )

            if permission.decision == PermissionDecision.deny:
                return self._fail(run_id, "permission_denied", permission.reason)
            if permission.decision == PermissionDecision.ask:
                human = self.store.create_human_action(
                    run_id=run_id,
                    tool_call_id=action.tool_call_id,
                    tool_name=tool.name,
                    request_json={"action": action.model_dump(mode="json"), "reason": permission.reason},
                )
                self.store.set_state(
                    run_id,
                    AgentRunState.needs_human,
                    error_category="approval_required",
                    error_detail=permission.reason,
                )
                self.store.checkpoint(
                    run_id,
                    state_json={
                        "state": AgentRunState.needs_human.value,
                        "human_action_id": human.id,
                        "tool_call_id": action.tool_call_id,
                    },
                )
                return self._outcome(run_id, human_action_id=human.id)

            outcome = self._execute_tool(
                run_id,
                action,
                tool,
                validated.model_dump(mode="json"),
            )
            if outcome is not None:
                return outcome

    def _execute_tool(
        self,
        run_id: str,
        action: NextAction,
        tool: Any,
        validated_json: dict[str, Any],
    ) -> RuntimeOutcome | None:
        validated = self.tools.validate(tool, validated_json)
        step = self.store.append_step(
            run_id,
            kind=AgentStepKind.tool.value,
            status="started",
            tool_name=tool.name,
            tool_call_id=action.tool_call_id,
            input_json={"arguments": validated_json, "idempotent": tool.idempotent},
        )
        try:
            result = self.tools.execute_validated(tool, validated)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            self.store.update_step(
                step.id,
                status="failed",
                error_category="tool_execution_failed",
                error_detail=detail,
            )
            return self._fail(run_id, "tool_execution_failed", detail)

        persisted = result.model_dump(mode="json")
        self.store.update_step(
            step.id,
            status="succeeded",
            output_json=persisted,
            evidence_refs=result.evidence_refs,
        )
        self.store.checkpoint(
            run_id,
            state_json={
                "state": AgentRunState.running.value,
                "last_committed_tool_call_id": action.tool_call_id,
                "last_committed_tool": tool.name,
            },
        )
        return None

    def _budget_failure(self, run: AgentRunRecord, *, before_model: bool) -> str | None:
        if run.step_count >= self.budget.max_steps:
            return f"max_steps={self.budget.max_steps} reached"
        # Equality blocks the *next* request but must not invalidate the request
        # that just consumed the final allowed slot.
        if before_model and run.model_calls >= self.budget.max_model_calls:
            return f"max_model_calls={self.budget.max_model_calls} reached"
        if run.input_tokens > self.budget.max_input_tokens:
            return f"input token budget exceeded ({run.input_tokens}>{self.budget.max_input_tokens})"
        if run.output_tokens > self.budget.max_output_tokens:
            return f"output token budget exceeded ({run.output_tokens}>{self.budget.max_output_tokens})"
        elapsed = (_utcnow() - run.created_at).total_seconds()
        if elapsed > self.budget.max_wall_time_seconds:
            return f"wall-time budget exceeded ({elapsed:.3f}s>{self.budget.max_wall_time_seconds}s)"
        return None

    def _fail(self, run_id: str, category: str, detail: str) -> RuntimeOutcome:
        self.store.append_step(
            run_id,
            kind=AgentStepKind.error.value,
            status="failed",
            error_category=category,
            error_detail=detail,
        )
        self.store.set_state(
            run_id,
            AgentRunState.failed,
            error_category=category,
            error_detail=detail,
        )
        return self._outcome(run_id)

    def _outcome(self, run_id: str, *, human_action_id: str | None = None) -> RuntimeOutcome:
        run = self.store.get_run(run_id)
        return RuntimeOutcome(
            run_id=run.id,
            state=AgentRunState(run.state),
            final_output=run.final_output_json,
            needs_human_action_id=human_action_id,
            error_category=run.error_category,
            error_detail=run.error_detail,
            usage=self.store.usage(run_id),
        )
