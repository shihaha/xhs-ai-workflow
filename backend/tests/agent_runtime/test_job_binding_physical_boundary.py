from pathlib import Path

import pytest

from backend.app.agent_runtime.job_binding import (
    AgentJobCoordinator,
    JobAuthorityError,
    JobAuthorityGuard,
)
from backend.app.agent_runtime.types import RunBudget
from backend.app.db import Database
from backend.app.models.jobs import JobState
from backend.app.services.jobs import InvalidJobTransition, JobService


def test_agent_coordinator_cannot_claim_physical_collection_job(tmp_path: Path) -> None:
    database = Database(tmp_path / "workbench.sqlite3")
    jobs = JobService(database)
    physical = jobs.create(
        job_type="xhs_collection",
        input_data={"account": "physical-worker-owned"},
    )
    coordinator = AgentJobCoordinator(database)

    with pytest.raises(InvalidJobTransition):
        coordinator.claim_and_create_run(
            physical.id,
            goal="must not steal a physical worker job",
            budget=RunBudget(),
        )

    persisted = jobs.get(physical.id)
    assert persisted.state is JobState.queued
    assert persisted.started_at is None
    assert persisted.lease_expires_at is None
    assert coordinator.run_ids_for_job(physical.id) == []


def test_agent_authority_guard_rejects_running_physical_collection_job(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "workbench.sqlite3")
    jobs = JobService(database)
    physical = jobs.create(
        job_type="xhs_collection",
        input_data={"account": "owned-by-existing-worker"},
    )
    jobs.claim(physical.id, lease_seconds=300)

    with pytest.raises(JobAuthorityError):
        JobAuthorityGuard(database).require_running(physical.id)

    persisted = jobs.get(physical.id)
    assert persisted.state is JobState.running
    assert persisted.lease_expires_at is not None
