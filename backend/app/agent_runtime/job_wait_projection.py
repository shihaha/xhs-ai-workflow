"""Project a durable AgentRun human wait into the authoritative Job state.

This slice intentionally stops at wait projection/reconciliation. It does not
resolve the human action, re-claim the Job, create a continuation run, or carry
forward continuation context/budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import exists, func, select, update

from backend.app.agent_runtime.job_binding import (
    AGENT_ORCHESTRATION_JOB_TYPE,
    AgentJobBindingRecord,
)
from backend.app.agent_runtime.job_bound_runtime import ActiveRunJobAuthorityGuard
from backend.app.agent_runtime.persistence import AgentRunStore
from backend.app.agent_runtime.types import AgentRunState
from backend.app.db import Database
from backend.app.models.jobs import JobRecord, JobState
from backend.app.services.jobs import JobService


_TERMINAL_JOB_STATES = {JobState.succeeded, JobState.failed, JobState.cancelled}


class JobWaitProjectionError(RuntimeError):
    """Persisted Agent wait and authoritative Job state cannot be reconciled safely."""


@dataclass(frozen=True, slots=True)
class JobWaitProjectionResult:
    run_id: str
    job_id: str
    job_state: JobState
    projected: bool
    terminal_job_won: bool = False


class JobHumanWaitProjector:
    """Tighten Job authority after an AgentRun durably enters ``needs_human``.

    The operation is intentionally idempotent and can be called during normal
    Runtime return or after process restart. It only permits one safe direction:

        latest AgentRun needs_human + same Job claim running -> Job needs_human

    A terminal Job always wins a race and is never reopened or overwritten.
    The write CAS is bound both to the observed claim generation and to the
    AgentRun still being the unique latest binding *inside the UPDATE itself*.
    """

    def __init__(self, database: Database) -> None:
        self.database = database
        self.store = AgentRunStore(database)
        self.jobs = JobService(database)
        self.guard = ActiveRunJobAuthorityGuard(database)

    def project_wait(self, run_id: str) -> JobWaitProjectionResult:
        # This checks immutable binding/type/current-run identity, but
        # deliberately does not require running execution authority because the
        # AgentRun has already durably transitioned to needs_human.
        job_id = self.guard.require_current_binding(run_id)
        run = self.store.get_run(run_id)
        if run.state != AgentRunState.needs_human.value:
            raise JobWaitProjectionError(
                f"Agent run {run_id} is {run.state!r}; only needs_human may be projected."
            )

        current = self.jobs.get(job_id)
        if current.state is JobState.needs_human:
            if current.lease_expires_at is not None:
                raise JobWaitProjectionError(
                    f"Job {job_id} is needs_human but still owns a lease; refusing raw repair."
                )
            return JobWaitProjectionResult(
                run_id=run_id,
                job_id=job_id,
                job_state=current.state,
                projected=False,
            )

        if current.state in _TERMINAL_JOB_STATES:
            return JobWaitProjectionResult(
                run_id=run_id,
                job_id=job_id,
                job_state=current.state,
                projected=False,
                terminal_job_won=True,
            )

        if current.state is not JobState.running:
            raise JobWaitProjectionError(
                f"Job {job_id} is {current.state.value}; cannot project Agent human wait safely."
            )
        if current.lease_expires_at is None:
            raise JobWaitProjectionError(
                f"Job {job_id} is running without a lease; refusing wait projection."
            )

        # There are two distinct TOCTOU windows to close:
        # 1. a newer claim can replace retry_count/lease after we read Job;
        # 2. a newer binding can appear after require_current_binding but before
        #    we read Job, in which case reading retry_count/lease alone would
        #    accidentally capture the *new* claim and let the old run clobber it.
        #
        # Therefore the same SQL UPDATE must prove both claim generation and
        # unique-latest-binding identity at its own write boundary.
        binding_table = AgentJobBindingRecord.__table__
        max_binding = binding_table.alias("wait_max_binding")
        count_binding = binding_table.alias("wait_count_binding")
        latest_created_at = (
            select(func.max(max_binding.c.created_at))
            .where(max_binding.c.job_id == job_id)
            .scalar_subquery()
        )
        latest_binding_count = (
            select(func.count())
            .select_from(count_binding)
            .where(
                count_binding.c.job_id == job_id,
                count_binding.c.created_at == latest_created_at,
            )
            .scalar_subquery()
        )
        current_run_is_latest = exists().where(
            binding_table.c.job_id == job_id,
            binding_table.c.run_id == run_id,
            binding_table.c.created_at == latest_created_at,
        )

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self.database.sessions.begin() as session:
            changed = session.execute(
                update(JobRecord)
                .where(
                    JobRecord.id == job_id,
                    JobRecord.type == AGENT_ORCHESTRATION_JOB_TYPE,
                    JobRecord.state == JobState.running.value,
                    JobRecord.retry_count == current.retry_count,
                    JobRecord.lease_expires_at == current.lease_expires_at,
                    current_run_is_latest,
                    latest_binding_count == 1,
                )
                .values(
                    state=JobState.needs_human.value,
                    error_category=run.error_category or "agent_needs_human",
                    lease_expires_at=None,
                    updated_at=now,
                )
            )
            projected = changed.rowcount == 1

        if not projected:
            # A cancellation, another projector, or a newer continuation claim
            # won after our point-in-time read. Re-read and honor Job authority.
            latest = self.jobs.get(job_id)
            if latest.state in _TERMINAL_JOB_STATES:
                return JobWaitProjectionResult(
                    run_id=run_id,
                    job_id=job_id,
                    job_state=latest.state,
                    projected=False,
                    terminal_job_won=True,
                )
            if latest.state is JobState.needs_human and latest.lease_expires_at is None:
                return JobWaitProjectionResult(
                    run_id=run_id,
                    job_id=job_id,
                    job_state=latest.state,
                    projected=False,
                )
            raise JobWaitProjectionError(
                f"Job {job_id} claim/binding changed during wait projection; "
                "stale Agent wait was not applied."
            )

        latest = self.jobs.get(job_id)
        if latest.state is not JobState.needs_human or latest.lease_expires_at is not None:
            raise JobWaitProjectionError(
                f"Job {job_id} wait projection committed an invalid lifecycle state."
            )
        return JobWaitProjectionResult(
            run_id=run_id,
            job_id=job_id,
            job_state=latest.state,
            projected=True,
        )

    def reconcile_wait_after_restart(self, run_id: str) -> JobWaitProjectionResult:
        """Replay only the safe state projection, never the pending Agent Tool."""

        return self.project_wait(run_id)
