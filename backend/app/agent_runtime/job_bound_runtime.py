"""Job-bound integration adapter for the canonical Agent Runtime.

This adapter composes the proven canonical ``AgentRuntime`` with durable Job
authority, human-wait projection, approved continuation, and terminal Job
projection. It is not exported from ``backend.app.agent_runtime`` and must not
own physical workers.
"""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import func, select

from backend.app.agent_runtime.events import RuntimeEventType
from backend.app.agent_runtime.job_binding import (
    AGENT_ORCHESTRATION_JOB_TYPE,
    AgentJobBindingRecord,
    JobAuthorityError,
    JobAuthorityGuard,
)
from backend.app.agent_runtime.job_terminal_projection import JobTerminalProjector
from backend.app.agent_runtime.persistence import AgentRunRecord
from backend.app.agent_runtime.runtime import AgentRuntime
from backend.app.agent_runtime.tools import ToolInputError, ToolUnavailableError
from backend.app.agent_runtime.types import AgentRunState, NextAction, RuntimeOutcome
from backend.app.models.jobs import JobRecord


class JobWaitProjector(Protocol):
    def project_wait(self, run_id: str) -> Any: ...


class ApprovedContinuationResolver(Protocol):
    def approved_action_for_run(self, run_id: str) -> NextAction: ...


class ActiveRunJobAuthorityGuard(JobAuthorityGuard):
    """Require one unambiguous current binding, then runnable execution authority.

    A Job may accumulate historical AgentRun bindings across human-wait /
    continuation claims. Merely checking that the Job is currently ``running``
    is therefore insufficient: an older run must not regain authority after a
    newer continuation run has been created.

    ``require_current_binding`` deliberately does *not* require the AgentRun or
    Job to be running. It exists for state projection/reconciliation after an
    AgentRun has already durably moved to ``needs_human``. Real model/tool
    execution must use ``require_run_running`` instead.
    """

    def require_current_binding(self, run_id: str) -> str:
        with self.database.sessions() as session:
            binding = session.get(AgentJobBindingRecord, run_id)
            if binding is None:
                raise JobAuthorityError(
                    f"Agent run {run_id} has no durable Job binding; Agent work must stop."
                )
            job_id = binding.job_id
            job = session.get(JobRecord, job_id)
            if job is None:
                raise JobAuthorityError(
                    f"Bound Job {job_id} does not exist; Agent work must stop."
                )
            if job.type != AGENT_ORCHESTRATION_JOB_TYPE:
                raise JobAuthorityError(
                    f"Bound Job {job.id} has type {job.type!r}; current AgentRun authority "
                    f"requires {AGENT_ORCHESTRATION_JOB_TYPE!r}."
                )

            latest_created_at = session.scalar(
                select(func.max(AgentJobBindingRecord.created_at)).where(
                    AgentJobBindingRecord.job_id == job.id
                )
            )
            if latest_created_at is None:
                raise JobAuthorityError(
                    f"Bound Job {job.id} has no current AgentRun binding; Agent work must stop."
                )
            latest_run_ids = list(
                session.scalars(
                    select(AgentJobBindingRecord.run_id).where(
                        AgentJobBindingRecord.job_id == job.id,
                        AgentJobBindingRecord.created_at == latest_created_at,
                    )
                )
            )

        if len(latest_run_ids) != 1:
            raise JobAuthorityError(
                f"Bound Job {job_id} has ambiguous latest AgentRun authority; Agent work must stop."
            )
        if latest_run_ids[0] != run_id:
            raise JobAuthorityError(
                f"Agent run {run_id} has been superseded by a newer continuation run; "
                "Agent work must stop."
            )
        return job_id

    def require_run_running(self, run_id: str, *, now=None) -> str:
        job_id = self.require_current_binding(run_id)
        self._require_job_running(job_id, now=now)
        with self.database.sessions() as session:
            run = session.get(AgentRunRecord, run_id)
            if run is None or run.state != AgentRunState.running.value:
                state = None if run is None else run.state
                raise JobAuthorityError(
                    f"Agent run {run_id} is not running (state={state!r}); Agent work must stop."
                )
        return job_id


class _AuthorityCheckedModel:
    """Check Job authority immediately before and after one provider call."""

    def __init__(self, delegate: Any, guard: ActiveRunJobAuthorityGuard) -> None:
        self._delegate = delegate
        self._guard = guard

    def next_action(self, context):
        try:
            self._guard.require_run_running(context.run_id)
        except JobAuthorityError as exc:
            raise JobAuthorityError(f"before_model: {exc}") from exc

        turn = self._delegate.next_action(context)

        # A cancel/lease loss can happen while the provider is in flight. Do not
        # apply a stale finish/tool decision after authority was revoked.
        try:
            self._guard.require_run_running(context.run_id)
        except JobAuthorityError as exc:
            raise JobAuthorityError(f"after_model: {exc}") from exc
        return turn


class JobBoundAgentRuntime(AgentRuntime):
    """Canonical Runtime plus authoritative Job lifecycle projection.

    New bound runs must be created by ``AgentJobCoordinator`` or the approved
    continuation coordinator first; this adapter only drives an already-bound,
    currently-authoritative run. The AgentRun trace is persisted first. Only
    after that durable proof exists may wait or terminal state be projected into
    the authoritative Job.
    """

    def __init__(
        self,
        *args: Any,
        authority_guard: ActiveRunJobAuthorityGuard,
        wait_projector: JobWaitProjector | None = None,
        continuation_resolver: ApprovedContinuationResolver | None = None,
        terminal_projector: JobTerminalProjector | None = None,
        **kwargs: Any,
    ) -> None:
        self.authority_guard = authority_guard
        self.wait_projector = wait_projector
        self.continuation_resolver = continuation_resolver
        self.terminal_projector = terminal_projector or JobTerminalProjector(
            authority_guard.database
        )
        super().__init__(*args, **kwargs)
        self.model = _AuthorityCheckedModel(self.model, authority_guard)

    def start(self, goal: str) -> RuntimeOutcome:
        raise RuntimeError(
            "Job-bound Agent runs must be created atomically by AgentJobCoordinator; "
            "use run_bound(run_id) after claim_and_create_run()."
        )

    def resume(self, run_id: str, *, approved: bool, note: str | None = None) -> RuntimeOutcome:
        raise RuntimeError(
            "Job-bound continuation must re-claim the needs_human Job and create a new AgentRun; "
            "resuming an older bound run is forbidden."
        )

    def resume_interrupted(
        self,
        run_id: str,
        *,
        acknowledge_uncertain: bool = False,
    ) -> RuntimeOutcome:
        raise RuntimeError(
            "Job-bound interrupted work requires explicit Job recovery/re-claim and a new continuation run."
        )

    def run_bound(self, run_id: str) -> RuntimeOutcome:
        run = self.store.get_run(run_id)
        if run.state != AgentRunState.running.value:
            raise ValueError("only a running bound AgentRun can be driven")
        return self._drive_and_project_wait(run_id)

    def run_approved_continuation(self, run_id: str) -> RuntimeOutcome:
        """Execute the exact durable approved action, then continue the model loop.

        A restart may call this method again. If the exact approved Tool result is
        already durably committed in the continuation run, execution is skipped
        and the model loop continues. If the call is only started/uncertain, the
        canonical duplicate-call guard moves the run to needs_human without
        replaying the handler.
        """

        run = self.store.get_run(run_id)
        if run.state != AgentRunState.running.value:
            raise ValueError("only a running approved continuation can be driven")
        if self.continuation_resolver is None:
            raise RuntimeError(
                "Approved continuation run has no durable continuation resolver."
            )

        action = self.continuation_resolver.approved_action_for_run(run_id)
        if action.action != "tool" or action.tool_name is None:
            raise RuntimeError("durable approved continuation is not a tool action")

        self._emit(
            RuntimeEventType.before_tool_validate,
            run_id,
            tool_name=action.tool_name,
            tool_call_id=action.tool_call_id,
            payload={"approved_continuation": True},
        )
        try:
            tool = self.tools.resolve(action.tool_name)
            validated = self.tools.validate(tool, action.arguments)
        except (ToolUnavailableError, ToolInputError, ValueError) as exc:
            return self._fail(run_id, "resume_validation_failed", str(exc))
        validated_json = validated.model_dump(mode="json")
        self._emit(
            RuntimeEventType.after_tool_validate,
            run_id,
            tool_name=tool.name,
            tool_call_id=action.tool_call_id,
            payload={"approved_continuation": True, "valid": True},
        )

        matches = [
            step
            for step in self.store.list_steps(run_id)
            if step.kind == "tool" and step.tool_call_id == action.tool_call_id
        ]
        if matches:
            previous = matches[-1]
            previous_arguments = (previous.input_json or {}).get("arguments")
            if previous.tool_name != tool.name or previous_arguments != validated_json:
                return self._fail(
                    run_id,
                    "tool_call_id_conflict",
                    "Approved continuation tool_call_id no longer matches durable Tool identity/arguments.",
                )
            if previous.status in {"succeeded", "reused"}:
                # The approved result already committed before a crash. Never
                # replay even a non-idempotent handler merely to resume the loop.
                return self._drive_and_project_wait(run_id)

        outcome = self._execute_tool(
            run_id,
            action,
            tool,
            validated_json,
        )
        if outcome is not None:
            return self._project_wait_if_needed(run_id, outcome)
        return self._drive_and_project_wait(run_id)

    def _drive_and_project_wait(self, run_id: str) -> RuntimeOutcome:
        outcome = self._drive(run_id)
        return self._project_wait_if_needed(run_id, outcome)

    def _project_wait_if_needed(
        self,
        run_id: str,
        outcome: RuntimeOutcome,
    ) -> RuntimeOutcome:
        if outcome.state is AgentRunState.needs_human:
            # AgentRuntime has already durably persisted the wait/checkpoint at
            # this point. Projection may therefore safely tighten Job authority
            # and clear its lease. Crash reconciliation calls the same projector.
            if self.wait_projector is None:
                raise RuntimeError(
                    "Job-bound Runtime reached needs_human without a Job wait projector; "
                    "the persisted Agent wait requires explicit restart reconciliation."
                )
            self.wait_projector.project_wait(run_id)
        elif outcome.state in {AgentRunState.succeeded, AgentRunState.failed}:
            # Final output/error proof is already durable. The terminal projector
            # performs only the authoritative Job CAS and never replays work.
            self.terminal_projector.project_terminal(run_id)
        return outcome

    def _execute_tool(
        self,
        run_id: str,
        action: NextAction,
        tool: Any,
        validated_json: dict[str, Any],
    ) -> RuntimeOutcome | None:
        try:
            self.authority_guard.require_run_running(run_id)
        except JobAuthorityError as exc:
            return self._fail(run_id, "job_authority_lost", f"before_tool: {exc}")
        return super()._execute_tool(run_id, action, tool, validated_json)

    def _fail(self, run_id: str, category: str, detail: str) -> RuntimeOutcome:
        # The canonical Stage 1 loop intentionally catches provider exceptions
        # generically. The guarded model raises JobAuthorityError to stop before
        # calling/applying the model; normalize that one known control failure.
        if category == "model_error" and detail.startswith("JobAuthorityError: "):
            category = "job_authority_lost"
            detail = detail.removeprefix("JobAuthorityError: ")
        return super()._fail(run_id, category, detail)
