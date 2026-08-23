"""Safe operator mutations for the durable Agent workbench.

Job remains lifecycle authority. Cancel/deny reconcile Job, AgentRun, and
HumanAction state transactionally. Approval delegates to the app-owned durable
continuation executor, whose dispatch row is committed atomically with the new
continuation AgentRun. React never manufactures lifecycle authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select, update

from backend.app.agent_runtime.continuation_executor import (
    AgentContinuationExecutor,
    ContinuationExecutorClosed,
)
from backend.app.agent_runtime.job_continuation import ContinuationApprovalError
from backend.app.agent_runtime.job_binding import (
    AGENT_ORCHESTRATION_JOB_TYPE,
    AgentJobBindingRecord,
)
from backend.app.agent_runtime.persistence import (
    AgentRunRecord,
    AgentRunStore,
    AgentStepRecord,
    HumanActionRecord,
)
from backend.app.agent_runtime.types import AgentRunState
from backend.app.db import Database
from backend.app.models.jobs import JobLogRecord, JobRecord, JobState


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AgentWorkbenchActionError(RuntimeError):
    """An operator mutation cannot be proven safe from durable state."""


@dataclass(frozen=True, slots=True)
class AgentCancelResult:
    job_id: str
    job_state: str
    cancelled_run_ids: tuple[str, ...]
    resolved_human_action_ids: tuple[str, ...]
    already_cancelled: bool = False


@dataclass(frozen=True, slots=True)
class HumanActionDenialResult:
    human_action_id: str
    job_id: str
    run_id: str
    human_action_status: str
    job_state: str
    run_state: str


@dataclass(frozen=True, slots=True)
class HumanActionApprovalResult:
    human_action_id: str
    job_id: str
    source_run_id: str
    continuation_run_id: str


class AgentWorkbenchActionService:
    """Narrow, backend-authoritative operator command surface."""

    def __init__(
        self,
        database: Database,
        *,
        continuation_executor: AgentContinuationExecutor | None = None,
    ) -> None:
        self.database = database
        self.continuation_executor = continuation_executor
        # Ensure staged Agent tables exist on a fresh local database.
        self.store = AgentRunStore(database)
        AgentJobBindingRecord.__table__.create(bind=database.engine, checkfirst=True)

    def capabilities(self) -> dict[str, object]:
        executor_ready = bool(
            self.continuation_executor is not None
            and self.continuation_executor.accepting
        )
        return {
            "cancel_job": True,
            "deny_permission_action": True,
            "approve_continuation": executor_ready,
            "continuation_reason": (
                None
                if executor_ready
                else "Automatic Agent continuation requires a configured app-owned executor/model."
            ),
        }

    def approve_permission_action(
        self,
        human_action_id: str,
        *,
        note: str | None = None,
    ) -> HumanActionApprovalResult:
        executor = self.continuation_executor
        if executor is None or not executor.accepting:
            raise AgentWorkbenchActionError(
                "Automatic Agent continuation executor is unavailable."
            )
        try:
            approved = executor.approve(human_action_id, note=note)
        except KeyError:
            raise
        except (ContinuationApprovalError, ContinuationExecutorClosed) as error:
            raise AgentWorkbenchActionError(str(error)) from error
        return HumanActionApprovalResult(
            human_action_id=approved.human_action_id,
            job_id=approved.job_id,
            source_run_id=approved.source_run_id,
            continuation_run_id=approved.run_id,
        )

    def cancel_job(self, job_id: str) -> AgentCancelResult:
        """Cancel one Agent orchestration Job and stop its current execution trace.

        Cancellation is terminal Job authority. Only the latest binding(s) that
        could still claim current execution are marked cancelled; older waiting
        runs remain immutable historical audit records. Every still-pending
        HumanAction is resolved as denied because a cancelled Job can never
        authorize a later continuation.
        """

        now = _utcnow()
        with self.database.sessions.begin() as session:
            job = session.get(JobRecord, job_id)
            if job is None:
                raise KeyError(f"Agent Job not found: {job_id}")
            if job.type != AGENT_ORCHESTRATION_JOB_TYPE:
                raise AgentWorkbenchActionError(
                    f"Job {job_id} has type {job.type!r}; Agent operator actions require "
                    f"{AGENT_ORCHESTRATION_JOB_TYPE!r}."
                )

            bindings = list(
                session.scalars(
                    select(AgentJobBindingRecord)
                    .where(AgentJobBindingRecord.job_id == job_id)
                    .order_by(AgentJobBindingRecord.created_at, AgentJobBindingRecord.run_id)
                )
            )
            run_ids = [binding.run_id for binding in bindings]
            latest_run_ids: list[str] = []
            if bindings:
                latest_at = max(binding.created_at for binding in bindings)
                latest_run_ids = [
                    binding.run_id for binding in bindings if binding.created_at == latest_at
                ]

            observed_job_state = JobState(job.state)
            already_cancelled = observed_job_state is JobState.cancelled
            if observed_job_state in {JobState.succeeded, JobState.failed}:
                raise AgentWorkbenchActionError(
                    f"Terminal Job {job_id} is {observed_job_state.value}; cancellation cannot overwrite it."
                )

            if not already_cancelled:
                observed_state = observed_job_state.value
                observed_retry = job.retry_count
                observed_lease = job.lease_expires_at
                conditions = [
                    JobRecord.id == job_id,
                    JobRecord.type == AGENT_ORCHESTRATION_JOB_TYPE,
                    JobRecord.state == observed_state,
                    JobRecord.retry_count == observed_retry,
                ]
                if observed_lease is None:
                    conditions.append(JobRecord.lease_expires_at.is_(None))
                else:
                    conditions.append(JobRecord.lease_expires_at == observed_lease)
                changed = session.execute(
                    update(JobRecord)
                    .where(*conditions)
                    .values(
                        state=JobState.cancelled.value,
                        current_stage="operator_cancelled",
                        error_category="operator_cancelled",
                        lease_expires_at=None,
                        completed_at=now,
                        updated_at=now,
                    )
                )
                if changed.rowcount != 1:
                    raise AgentWorkbenchActionError(
                        "Job authority changed during cancellation; no operator mutation was committed."
                    )

            cancelled_run_ids: list[str] = []
            resolved_action_ids: list[str] = []
            if run_ids:
                current_runs = list(
                    session.scalars(
                        select(AgentRunRecord).where(AgentRunRecord.id.in_(latest_run_ids))
                    )
                ) if latest_run_ids else []
                for run in current_runs:
                    if run.state in {
                        AgentRunState.running.value,
                        AgentRunState.needs_human.value,
                    }:
                        run.state = AgentRunState.cancelled.value
                        run.error_category = "operator_cancelled"
                        run.error_detail = "Authoritative Agent Job was cancelled by the operator."
                        run.updated_at = now
                        run.completed_at = now
                        cancelled_run_ids.append(run.id)

                pending_actions = list(
                    session.scalars(
                        select(HumanActionRecord).where(
                            HumanActionRecord.run_id.in_(run_ids),
                            HumanActionRecord.status == "pending",
                        )
                    )
                )
                for action in pending_actions:
                    action.status = "denied"
                    action.resolution_json = {
                        "approved": False,
                        "note": "operator_cancelled",
                    }
                    action.resolved_at = now
                    resolved_action_ids.append(action.id)

            if not already_cancelled or cancelled_run_ids or resolved_action_ids:
                session.add(
                    JobLogRecord(
                        job_id=job_id,
                        level="warning",
                        message=(
                            "Agent orchestration Job cancelled by operator; active Agent traces stopped."
                            if not already_cancelled
                            else "Reconciled active Agent traces for an already-cancelled Job."
                        ),
                        created_at=now,
                    )
                )

        return AgentCancelResult(
            job_id=job_id,
            job_state=JobState.cancelled.value,
            cancelled_run_ids=tuple(sorted(cancelled_run_ids)),
            resolved_human_action_ids=tuple(sorted(resolved_action_ids)),
            already_cancelled=already_cancelled,
        )

    def deny_permission_action(
        self,
        human_action_id: str,
        *,
        note: str | None = None,
    ) -> HumanActionDenialResult:
        """Deny exactly one latest permission-gated HumanAction and stop its Job.

        Manual ChatGPT handoffs and generic domain waits are intentionally not
        routed through this binary permission decision path.
        """

        now = _utcnow()
        with self.database.sessions.begin() as session:
            human = session.get(HumanActionRecord, human_action_id)
            if human is None:
                raise KeyError(f"HumanAction not found: {human_action_id}")
            if human.status != "pending":
                raise AgentWorkbenchActionError("HumanAction is already resolved.")

            source = session.get(AgentRunRecord, human.run_id)
            binding = session.get(AgentJobBindingRecord, human.run_id)
            if source is None or binding is None:
                raise AgentWorkbenchActionError(
                    "HumanAction lost its source AgentRun or durable Job binding."
                )
            job = session.get(JobRecord, binding.job_id)
            if job is None or job.type != AGENT_ORCHESTRATION_JOB_TYPE:
                raise AgentWorkbenchActionError(
                    "HumanAction is not bound to an authoritative Agent orchestration Job."
                )
            if source.state != AgentRunState.needs_human.value:
                raise AgentWorkbenchActionError("Source AgentRun is not waiting for a human decision.")
            if source.error_category != "approval_required":
                raise AgentWorkbenchActionError(
                    "Only permission-gated approval_required waits may be denied here."
                )
            if JobState(job.state) is not JobState.needs_human or job.lease_expires_at is not None:
                raise AgentWorkbenchActionError(
                    "Authoritative Job is not a lease-free needs_human permission wait."
                )

            latest_at = session.scalar(
                select(func.max(AgentJobBindingRecord.created_at)).where(
                    AgentJobBindingRecord.job_id == job.id
                )
            )
            if latest_at is None:
                raise AgentWorkbenchActionError("Job has no AgentRun binding history.")
            latest_ids = list(
                session.scalars(
                    select(AgentJobBindingRecord.run_id).where(
                        AgentJobBindingRecord.job_id == job.id,
                        AgentJobBindingRecord.created_at == latest_at,
                    )
                )
            )
            if len(latest_ids) != 1 or latest_ids[0] != source.id:
                raise AgentWorkbenchActionError(
                    "HumanAction source is not the single unambiguous current AgentRun binding."
                )

            changed = session.execute(
                update(JobRecord)
                .where(
                    JobRecord.id == job.id,
                    JobRecord.type == AGENT_ORCHESTRATION_JOB_TYPE,
                    JobRecord.state == JobState.needs_human.value,
                    JobRecord.retry_count == job.retry_count,
                    JobRecord.lease_expires_at.is_(None),
                )
                .values(
                    state=JobState.failed.value,
                    current_stage="human_denied",
                    error_category="human_denied",
                    lease_expires_at=None,
                    completed_at=now,
                    updated_at=now,
                )
            )
            if changed.rowcount != 1:
                raise AgentWorkbenchActionError(
                    "Job authority changed during denial; no operator mutation was committed."
                )

            human.status = "denied"
            human.resolution_json = {"approved": False, "note": note}
            human.resolved_at = now
            denial_detail = "Operator denied the exact pending permission action."
            source.step_count += 1
            session.add(
                AgentStepRecord(
                    run_id=source.id,
                    step_index=source.step_count,
                    kind="error",
                    tool_name=human.tool_name,
                    tool_call_id=human.tool_call_id,
                    input_json=None,
                    output_json=None,
                    evidence_refs_json=[],
                    status="failed",
                    error_category="human_denied",
                    error_detail=denial_detail,
                    created_at=now,
                    updated_at=now,
                )
            )
            source.state = AgentRunState.failed.value
            source.error_category = "human_denied"
            source.error_detail = denial_detail
            source.updated_at = now
            source.completed_at = now
            result_job_id = job.id
            result_run_id = source.id
            session.add(
                JobLogRecord(
                    job_id=job.id,
                    level="info",
                    message=(
                        f"HumanAction {human_action_id} denied; Agent run {source.id} and Job failed closed."
                    ),
                    created_at=now,
                )
            )

        return HumanActionDenialResult(
            human_action_id=human_action_id,
            job_id=result_job_id,
            run_id=result_run_id,
            human_action_status="denied",
            job_state=JobState.failed.value,
            run_state=AgentRunState.failed.value,
        )
