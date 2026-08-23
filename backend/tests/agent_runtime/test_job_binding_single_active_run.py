from pathlib import Path

import pytest

from backend.app.agent_runtime.job_binding import AgentJobCoordinator
from backend.app.agent_runtime.types import RunBudget
from backend.app.db import Database
from backend.app.models.jobs import JobState
from backend.app.services.jobs import InvalidJobTransition, JobService


def test_running_orchestration_job_cannot_start_second_agent_run(tmp_path: Path) -> None:
    database = Database(tmp_path / "workbench.sqlite3")
    jobs = JobService(database)
    job = jobs.create(job_type="agent_orchestration", input_data={})
    coordinator = AgentJobCoordinator(
        database,
        run_id_factory=iter(("run-active", "run-must-not-start")).__next__,
    )

    first = coordinator.claim_and_create_run(
        job.id,
        goal="first active run",
        budget=RunBudget(max_wall_time_seconds=10),
    )

    with pytest.raises(InvalidJobTransition):
        coordinator.claim_and_create_run(
            job.id,
            goal="must not overlap active run",
            budget=RunBudget(max_wall_time_seconds=10),
        )

    current = jobs.get(job.id)
    assert current.state is JobState.running
    assert current.retry_count == 0
    assert coordinator.run_ids_for_job(job.id) == [first.run_id]
