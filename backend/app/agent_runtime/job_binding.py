"""Durable Job ↔ AgentRun binding for the staged integration spike.

JobService remains the authoritative task lifecycle.  This module only binds an
AgentRun execution trace to an already-existing Job and performs the initial
Job claim + AgentRun + binding insert in one SQLite transaction.

It intentionally does not drive Android/XHS/browser physical work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil
from typing import Callable
from uuid import uuid4

from pydantic import BaseModel
from sqlalchemy import DateTime, String, case, select, update
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from backend.app.agent_runtime.persistence import AgentRunRecord, AgentRunStore
from backend.app.agent_runtime.tools import ToolSpec
from backend.app.agent_runtime.types import AgentRunState, RunBudget, ToolExecutionResult
from backend.app.db import Database
from backend.app.models.jobs import JobLogRecord, JobRecord, JobState
from backend.app.services.jobs import InvalidJobTransition, JobNotFound, JobService


AGENT_ORCHESTRATION_JOB_TYPE = "agent_orchestration"
DEFAULT_SHUTDOWN_MARGIN_SECONDS = 60
JOB_READ_TOOL_NAME = "job.read"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AgentJobBindingBase(DeclarativeBase):
    """Separate metadata keeps the staged binding out of the production registry."""


class AgentJobBindingRecord(AgentJobBindingBase):
    __tablename__ = "agent_job_bindings"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class JobAuthorityError(RuntimeError):
    """The authoritative Job is no longer runnable for Agent work."""


@dataclass(frozen=True, slots=True)
class BoundAgentRun:
    job_id: str
    run_id: str
    lease_expires_at: datetime


class AgentJobCoordinator:
    """Own the narrow transactional boundary between Job and AgentRun persistence."""

    def __init__(
        self,
        database: Database,
        *,
        run_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.database = database
        self._run_id_factory = run_id_factory or (lambda: str(uuid4()))
        # A fresh integration database must not depend on some other caller
        # having initialized the Agent Runtime tables first.
        self.run_store = AgentRunStore(database)
        AgentJobBindingRecord.__table__.create(bind=database.engine, checkfirst=True)

    def claim_and_create_run(
        self,
        job_id: str,
        *,
        goal: str,
        budget: RunBudget,
        model_name: str | None = None,
        prompt_version: str | None = None,
        shutdown_margin_seconds: int = DEFAULT_SHUTDOWN_MARGIN_SECONDS,
    ) -> BoundAgentRun:
        """Claim one orchestration Job and create its AgentRun atomically.

        A failure anywhere before transaction commit rolls back the Job claim,
        AgentRun insert, binding insert, and audit log together. Physical/domain
        worker Job types are intentionally ineligible for this coordinator.
        """

        if shutdown_margin_seconds < 1:
            raise ValueError("shutdown_margin_seconds must be positive")

        now = _utcnow()
        lease_seconds = ceil(budget.max_wall_time_seconds + shutdown_margin_seconds)
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        run_id = self._run_id_factory()

        with self.database.sessions.begin() as session:
            current = session.get(JobRecord, job_id)
            if current is None:
                raise JobNotFound(f"Job {job_id} does not exist.")
            if current.type != AGENT_ORCHESTRATION_JOB_TYPE:
                raise InvalidJobTransition(
                    f"Job type {current.type!r} cannot be claimed by AgentRuntime; "
                    f"expected {AGENT_ORCHESTRATION_JOB_TYPE!r}."
                )

            current_state = JobState(current.state)
            if current_state not in {JobState.queued, JobState.needs_human}:
                raise InvalidJobTransition(
                    f"Cannot claim a {current_state.value} job for an Agent run."
                )

            changed = session.execute(
                update(JobRecord)
                .where(
                    JobRecord.id == job_id,
                    JobRecord.type == AGENT_ORCHESTRATION_JOB_TYPE,
                    JobRecord.state == current_state.value,
                )
                .values(
                    state=JobState.running.value,
                    retry_count=case(
                        (
                            JobRecord.state == JobState.needs_human.value,
                            JobRecord.retry_count + 1,
                        ),
                        else_=JobRecord.retry_count,
                    ),
                    started_at=case(
                        (JobRecord.started_at.is_(None), now),
                        else_=JobRecord.started_at,
                    ),
                    lease_expires_at=lease_expires_at,
                    updated_at=now,
                )
            )
            if changed.rowcount != 1:
                raise InvalidJobTransition(
                    "Job authority changed while creating the Agent run; no partial claim was committed."
                )

            session.add(
                AgentRunRecord(
                    id=run_id,
                    goal=goal,
                    state=AgentRunState.running.value,
                    model_name=model_name,
                    prompt_version=prompt_version,
                    budget_json=budget.model_dump(mode="json"),
                    step_count=0,
                    model_calls=0,
                    input_tokens=0,
                    output_tokens=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                AgentJobBindingRecord(
                    run_id=run_id,
                    job_id=job_id,
                    created_at=now,
                )
            )
            session.add(
                JobLogRecord(
                    job_id=job_id,
                    level="info",
                    message=f"Agent run {run_id} claimed this job.",
                    created_at=now,
                )
            )

        return BoundAgentRun(
            job_id=job_id,
            run_id=run_id,
            lease_expires_at=lease_expires_at,
        )

    def job_id_for_run(self, run_id: str) -> str:
        with self.database.sessions() as session:
            binding = session.get(AgentJobBindingRecord, run_id)
            if binding is None:
                raise KeyError(f"agent run is not bound to a Job: {run_id}")
            return binding.job_id

    def run_ids_for_job(self, job_id: str) -> list[str]:
        with self.database.sessions() as session:
            return list(
                session.scalars(
                    select(AgentJobBindingRecord.run_id)
                    .where(AgentJobBindingRecord.job_id == job_id)
                    .order_by(AgentJobBindingRecord.created_at, AgentJobBindingRecord.run_id)
                )
            )


class JobAuthorityGuard:
    """Validate Job authority through a durable AgentRun→Job binding only."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def require_run_running(self, run_id: str, *, now: datetime | None = None) -> str:
        """Return the bound Job ID only when that exact run still has authority."""

        with self.database.sessions() as session:
            binding = session.get(AgentJobBindingRecord, run_id)
            if binding is None:
                raise JobAuthorityError(
                    f"Agent run {run_id} has no durable Job binding; Agent work must stop."
                )
            job_id = binding.job_id
        self._require_job_running(job_id, now=now)
        return job_id

    def _require_job_running(self, job_id: str, *, now: datetime | None = None) -> None:
        current_time = now or _utcnow()
        with self.database.sessions() as session:
            record = session.get(JobRecord, job_id)
            if record is None:
                raise JobAuthorityError(
                    f"Bound Job {job_id} does not exist; Agent work must stop."
                )
            if record.type != AGENT_ORCHESTRATION_JOB_TYPE:
                raise JobAuthorityError(
                    f"Bound Job {job_id} has type {record.type!r}; Agent authority requires "
                    f"{AGENT_ORCHESTRATION_JOB_TYPE!r}."
                )
            state = JobState(record.state)
            if state is not JobState.running:
                raise JobAuthorityError(
                    f"Bound Job {job_id} is {state.value}; Agent work requires running authority."
                )
            if record.lease_expires_at is None or record.lease_expires_at <= current_time:
                raise JobAuthorityError(
                    f"Bound Job {job_id} has no valid running lease; Agent work must stop."
                )


class JobReadInput(BaseModel):
    job_id: str


def build_job_read_tool(service: JobService) -> ToolSpec:
    """Expose a narrow read-only Job projection; never raw SQL or worker control."""

    def read(payload: JobReadInput) -> ToolExecutionResult:
        job = service.get(payload.job_id)
        return ToolExecutionResult(
            output={
                "id": job.id,
                "type": job.type,
                "state": job.state.value,
                "progress_current": job.progress_current,
                "progress_total": job.progress_total,
                "current_stage": job.current_stage,
                "error_category": job.error_category,
                "retry_count": job.retry_count,
                "lease_expires_at": (
                    job.lease_expires_at.isoformat()
                    if job.lease_expires_at is not None
                    else None
                ),
            },
            summary=f"job:{job.id}:{job.state.value}",
        )

    return ToolSpec(
        name=JOB_READ_TOOL_NAME,
        description=(
            "Read the authoritative durable Job lifecycle projection for one explicit job ID. "
            "This tool cannot claim, mutate, cancel, or run physical work."
        ),
        input_model=JobReadInput,
        handler=read,
        read_only=True,
        destructive=False,
        external_side_effect=False,
        requires_approval=False,
        timeout_seconds=5.0,
        idempotent=True,
        retry_limit=0,
    )
