from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import update

from backend.app.agent_runtime.job_binding import (
    AgentJobBindingRecord,
    AgentJobCoordinator,
    JobAuthorityError,
    JobAuthorityGuard,
)
from backend.app.agent_runtime.persistence import AgentRunStore
from backend.app.agent_runtime.types import RunBudget
from backend.app.db import Database
from backend.app.models.jobs import JobRecord, JobState
from backend.app.services.jobs import JobService


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_binding_survives_database_restart_and_reconstructs_same_authority(tmp_path: Path) -> None:
    database_path = tmp_path / "workbench.sqlite3"

    first_database = Database(database_path)
    first_jobs = JobService(first_database)
    job = first_jobs.create(job_type="agent_orchestration", input_data={"scope": "read-only"})
    first_coordinator = AgentJobCoordinator(
        first_database,
        run_id_factory=lambda: "run-restart",
    )
    bound = first_coordinator.claim_and_create_run(
        job.id,
        goal="survive restart",
        budget=RunBudget(max_wall_time_seconds=30),
    )

    restarted_database = Database(database_path)
    restarted_jobs = JobService(restarted_database)
    restarted_coordinator = AgentJobCoordinator(restarted_database)
    restarted_store = AgentRunStore(restarted_database)
    restarted_guard = JobAuthorityGuard(restarted_database)

    assert restarted_jobs.get(job.id).state is JobState.running
    assert restarted_store.get_run(bound.run_id).id == bound.run_id
    assert restarted_coordinator.job_id_for_run(bound.run_id) == job.id
    assert restarted_coordinator.run_ids_for_job(job.id) == [bound.run_id]
    assert restarted_guard.require_run_running(bound.run_id) == job.id


def test_expired_bound_job_is_recovered_by_job_service_and_guard_fails_closed(tmp_path: Path) -> None:
    database = Database(tmp_path / "workbench.sqlite3")
    jobs = JobService(database)
    job = jobs.create(job_type="agent_orchestration", input_data={})
    coordinator = AgentJobCoordinator(database, run_id_factory=lambda: "run-expired")
    bound = coordinator.claim_and_create_run(
        job.id,
        goal="expire authority",
        budget=RunBudget(max_wall_time_seconds=10),
    )

    with database.sessions.begin() as session:
        session.execute(
            update(JobRecord)
            .where(JobRecord.id == job.id)
            .values(lease_expires_at=_utcnow() - timedelta(seconds=1))
        )

    assert jobs.recover_expired_running() == 1
    recovered = jobs.get(job.id)
    assert recovered.state is JobState.needs_human
    assert recovered.lease_expires_at is None
    assert coordinator.job_id_for_run(bound.run_id) == job.id

    with pytest.raises(JobAuthorityError):
        JobAuthorityGuard(database).require_run_running(bound.run_id)


def test_human_wait_clears_lease_and_reclaim_creates_new_bound_run(tmp_path: Path) -> None:
    database = Database(tmp_path / "workbench.sqlite3")
    jobs = JobService(database)
    job = jobs.create(job_type="agent_orchestration", input_data={})
    coordinator = AgentJobCoordinator(
        database,
        run_id_factory=iter(("run-before-human", "run-after-human")).__next__,
    )

    first = coordinator.claim_and_create_run(
        job.id,
        goal="wait for human",
        budget=RunBudget(max_wall_time_seconds=10),
    )
    waiting = jobs.transition(
        job.id,
        JobState.needs_human,
        error_category="approval_required",
    )
    assert waiting.lease_expires_at is None

    with pytest.raises(JobAuthorityError):
        JobAuthorityGuard(database).require_run_running(first.run_id)

    second = coordinator.claim_and_create_run(
        job.id,
        goal="continue after human",
        budget=RunBudget(max_wall_time_seconds=10),
    )
    resumed = jobs.get(job.id)
    assert resumed.state is JobState.running
    assert resumed.retry_count == 1
    assert resumed.lease_expires_at is not None
    assert coordinator.run_ids_for_job(job.id) == [first.run_id, second.run_id]
    assert JobAuthorityGuard(database).require_run_running(second.run_id) == job.id


def test_orphan_binding_fails_closed_when_bound_job_is_missing(tmp_path: Path) -> None:
    database = Database(tmp_path / "workbench.sqlite3")
    coordinator = AgentJobCoordinator(database)

    with database.sessions.begin() as session:
        session.add(
            AgentJobBindingRecord(
                run_id="orphan-run",
                job_id="missing-job",
                created_at=_utcnow(),
            )
        )

    assert coordinator.job_id_for_run("orphan-run") == "missing-job"
    with pytest.raises(JobAuthorityError):
        JobAuthorityGuard(database).require_run_running("orphan-run")
