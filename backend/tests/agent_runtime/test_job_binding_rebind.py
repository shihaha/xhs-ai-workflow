from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from backend.app.agent_runtime.job_binding import AgentJobCoordinator
from backend.app.agent_runtime.types import RunBudget
from backend.app.db import Database
from backend.app.models.jobs import JobState
from backend.app.services.jobs import JobService


def test_same_agent_run_id_cannot_bind_second_job_and_second_claim_rolls_back(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "workbench.sqlite3")
    jobs = JobService(database)
    first_job = jobs.create(job_type="agent_orchestration", input_data={"n": 1})
    second_job = jobs.create(job_type="agent_orchestration", input_data={"n": 2})
    coordinator = AgentJobCoordinator(
        database,
        run_id_factory=lambda: "one-run-id",
    )

    first = coordinator.claim_and_create_run(
        first_job.id,
        goal="first owner",
        budget=RunBudget(max_wall_time_seconds=10),
    )
    assert coordinator.job_id_for_run(first.run_id) == first_job.id

    with pytest.raises(IntegrityError):
        coordinator.claim_and_create_run(
            second_job.id,
            goal="must not rebind",
            budget=RunBudget(max_wall_time_seconds=10),
        )

    second_after = jobs.get(second_job.id)
    assert second_after.state is JobState.queued
    assert second_after.started_at is None
    assert second_after.lease_expires_at is None
    assert coordinator.run_ids_for_job(second_job.id) == []
    assert coordinator.job_id_for_run(first.run_id) == first_job.id
