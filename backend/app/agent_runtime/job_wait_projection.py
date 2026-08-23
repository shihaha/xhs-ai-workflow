"""Project a durable AgentRun human wait into the authoritative Job state.

This slice intentionally stops at wait projection/reconciliation.  It does not
resolve the human action, re-claim the Job, create a continuation run, or carry
forward continuation context/budget.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.app.agent_runtime.job_bound_runtime import ActiveRunJobAuthorityGuard
from backend.app.agent_runtime.persistence import AgentRunStore
from backend.app.agent_runtime.types import AgentRunState
from backend.app.db import Database
from backend.app.models.jobs import JobState
from backend.app.services.jobs import InvalidJobTransition, JobService


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
    Runtime return or after process restart.  It only permits one safe direction:

        latest AgentRun needs_human + Job running -> Job needs_human

    A terminal Job always wins a race and is never reopened or overwritten.
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

        try:
            projected = self.jobs.transition(
                job_id,
                JobState.needs_human,
                error_category=run.error_category or "agent_needs_human",
            )
        except InvalidJobTransition as exc:
            # A cancellation/other operator transition can win between our read
            # and the JobService CAS. Re-read and honor authoritative Job truth.
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
                f"Job {job_id} changed during wait projection and cannot be reconciled safely."
            ) from exc

        if projected.lease_expires_at is not None:
            raise JobWaitProjectionError(
                f"JobService projected {job_id} to needs_human without clearing its lease."
            )
        return JobWaitProjectionResult(
            run_id=run_id,
            job_id=job_id,
            job_state=projected.state,
            projected=True,
        )

    def reconcile_wait_after_restart(self, run_id: str) -> JobWaitProjectionResult:
        """Replay only the safe state projection, never the pending Agent Tool."""

        return self.project_wait(run_id)
