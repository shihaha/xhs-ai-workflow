"""Durable job state transitions, logs and evidence persistence."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.db import Database
from backend.app.models.jobs import JobArtifactRecord, JobLogRecord, JobRecord, JobState


class JobNotFound(Exception):
    """Raised when a requested durable job does not exist."""


class InvalidJobTransition(Exception):
    """Raised when an attempted job-state edge is not allowed."""


@dataclass(frozen=True)
class JobLog:
    level: str
    message: str


@dataclass(frozen=True)
class JobArtifact:
    kind: str
    path: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class Job:
    id: str
    type: str
    input: dict[str, Any]
    state: JobState
    progress_current: int
    progress_total: int | None
    current_stage: str | None
    error_category: str | None
    retry_count: int
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    lease_expires_at: datetime | None
    logs: list[JobLog]
    artifacts: list[JobArtifact]


_PERMITTED_TRANSITIONS = {
    JobState.queued: {JobState.running, JobState.cancelled},
    JobState.running: {
        JobState.needs_human,
        JobState.succeeded,
        JobState.failed,
        JobState.cancelled,
    },
    JobState.needs_human: {JobState.running, JobState.cancelled},
    JobState.succeeded: set(),
    JobState.failed: set(),
    JobState.cancelled: set(),
}
_TERMINAL_STATES = {JobState.succeeded, JobState.failed, JobState.cancelled}


class JobService:
    """Persist state-machine operations in the configured SQLite database."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        *,
        job_type: str,
        input_data: dict[str, Any],
        progress_current: int = 0,
        progress_total: int | None = None,
        current_stage: str | None = None,
    ) -> Job:
        now = _utc_now()
        record = JobRecord(
            type=job_type,
            input_data=input_data,
            state=JobState.queued,
            progress_current=progress_current,
            progress_total=progress_total,
            current_stage=current_stage,
            created_at=now,
            updated_at=now,
        )
        with self.database.session() as session:
            session.add(record)
            session.commit()
            session.refresh(record)
            return _as_job(record)

    def get(self, job_id: str) -> Job:
        with self.database.session() as session:
            return _as_job(self._record(session, job_id))

    def list(self) -> list[Job]:
        with self.database.session() as session:
            records = session.scalars(
                select(JobRecord)
                .options(selectinload(JobRecord.logs), selectinload(JobRecord.artifacts))
                .order_by(JobRecord.created_at.desc())
            ).all()
            return [_as_job(record) for record in records]

    def claim(self, job_id: str, *, lease_seconds: int = 300) -> Job:
        with self.database.session() as session:
            record = self._record(session, job_id)
            current_state = JobState(record.state)
            if current_state not in {JobState.queued, JobState.needs_human}:
                raise InvalidJobTransition(f"Cannot claim a {current_state.value} job.")
            now = _utc_now()
            if current_state is JobState.needs_human:
                record.retry_count += 1
            record.state = JobState.running
            record.started_at = record.started_at or now
            record.lease_expires_at = now + timedelta(seconds=lease_seconds)
            record.updated_at = now
            session.commit()
            return _as_job(record)

    def transition(
        self,
        job_id: str,
        state: JobState,
        *,
        progress_current: int | None = None,
        progress_total: int | None = None,
        current_stage: str | None = None,
        error_category: str | None = None,
    ) -> Job:
        with self.database.session() as session:
            record = self._record(session, job_id)
            current_state = JobState(record.state)
            if state not in _PERMITTED_TRANSITIONS[current_state]:
                raise InvalidJobTransition(
                    f"Cannot transition {current_state.value} to {state.value}."
                )
            now = _utc_now()
            record.state = state
            if progress_current is not None:
                record.progress_current = progress_current
            if progress_total is not None:
                record.progress_total = progress_total
            if current_stage is not None:
                record.current_stage = current_stage
            if error_category is not None:
                record.error_category = error_category
            record.updated_at = now
            if state in _TERMINAL_STATES:
                record.completed_at = now
                record.lease_expires_at = None
            elif state is JobState.needs_human:
                record.lease_expires_at = None
            session.commit()
            return _as_job(record)

    def append_log(self, job_id: str, *, level: str, message: str) -> JobLog:
        with self.database.session() as session:
            record = self._record(session, job_id)
            log = JobLogRecord(
                job_id=record.id, level=level, message=message, created_at=_utc_now()
            )
            session.add(log)
            record.updated_at = _utc_now()
            session.commit()
            return JobLog(level=log.level, message=log.message)

    def attach_artifact(
        self, job_id: str, *, kind: str, path: str, metadata: dict[str, Any]
    ) -> JobArtifact:
        with self.database.session() as session:
            record = self._record(session, job_id)
            artifact = JobArtifactRecord(
                job_id=record.id,
                kind=kind,
                path=path,
                metadata_json=metadata,
                created_at=_utc_now(),
            )
            session.add(artifact)
            record.updated_at = _utc_now()
            session.commit()
            return JobArtifact(
                kind=artifact.kind, path=artifact.path, metadata=artifact.metadata_json
            )

    def recover_expired_running(self) -> int:
        """Turn abandoned running leases into an explicit human-action state."""
        now = _utc_now()
        with self.database.session() as session:
            records = session.scalars(
                select(JobRecord).where(
                    JobRecord.state == JobState.running,
                    JobRecord.lease_expires_at.is_not(None),
                    JobRecord.lease_expires_at < now,
                )
            ).all()
            for record in records:
                record.state = JobState.needs_human
                record.lease_expires_at = None
                record.updated_at = now
                session.add(
                    JobLogRecord(
                        job_id=record.id,
                        level="warning",
                        message="Running lease expired; human recovery required.",
                        created_at=now,
                    )
                )
            session.commit()
            return len(records)

    @staticmethod
    def _record(session: Any, job_id: str) -> JobRecord:
        record = session.scalar(
            select(JobRecord)
            .options(selectinload(JobRecord.logs), selectinload(JobRecord.artifacts))
            .where(JobRecord.id == job_id)
        )
        if record is None:
            raise JobNotFound(f"Job {job_id} does not exist.")
        return record


def _as_job(record: JobRecord) -> Job:
    return Job(
        id=record.id,
        type=record.type,
        input=dict(record.input_data),
        state=JobState(record.state),
        progress_current=record.progress_current,
        progress_total=record.progress_total,
        current_stage=record.current_stage,
        error_category=record.error_category,
        retry_count=record.retry_count,
        created_at=record.created_at,
        updated_at=record.updated_at,
        started_at=record.started_at,
        completed_at=record.completed_at,
        lease_expires_at=record.lease_expires_at,
        logs=[JobLog(level=log.level, message=log.message) for log in record.logs],
        artifacts=[
            JobArtifact(
                kind=artifact.kind, path=artifact.path, metadata=dict(artifact.metadata_json)
            )
            for artifact in record.artifacts
        ],
    )


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
