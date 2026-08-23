from pathlib import Path

import pytest

from backend.app.agent_runtime import AgentRunState, AgentRunStore, RunBudget
from backend.app.agent_runtime.job_binding import AgentJobCoordinator
from backend.app.agent_runtime.job_bound_runtime import ActiveRunJobAuthorityGuard
from backend.app.agent_runtime.job_wait_projection import (
    JobHumanWaitProjector,
    JobWaitProjectionError,
)
from backend.app.db import Database
from backend.app.models.jobs import JobState
from backend.app.services.jobs import JobService


def test_old_wait_projection_cannot_clobber_concurrently_reclaimed_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "workbench.sqlite3")
    jobs = JobService(database)
    job = jobs.create(job_type="agent_orchestration", input_data={})
    run_ids = iter(("old-run", "new-run"))
    coordinator = AgentJobCoordinator(database, run_id_factory=run_ids.__next__)
    old = coordinator.claim_and_create_run(
        job.id,
        goal="old claim",
        budget=RunBudget(max_wall_time_seconds=30),
    )
    AgentRunStore(database).set_state(
        old.run_id,
        AgentRunState.needs_human,
        error_category="approval_required",
    )

    projector = JobHumanWaitProjector(database)
    original_get = projector.jobs.get
    reads = {"count": 0}
    new_bound = {"value": None}

    def get_with_reclaim(job_id: str):
        snapshot = original_get(job_id)
        reads["count"] += 1
        if reads["count"] == 1:
            # Interleave exactly after the stale projector has captured the old
            # running claim generation and before its write CAS.
            JobService(database).transition(job_id, JobState.needs_human)
            new_bound["value"] = coordinator.claim_and_create_run(
                job_id,
                goal="new continuation claim",
                budget=RunBudget(max_wall_time_seconds=30),
            )
        return snapshot

    monkeypatch.setattr(projector.jobs, "get", get_with_reclaim)

    with pytest.raises(JobWaitProjectionError, match="claim changed"):
        projector.project_wait(old.run_id)

    new = new_bound["value"]
    assert new is not None
    latest_job = JobService(database).get(job.id)
    assert latest_job.state is JobState.running
    assert latest_job.retry_count == 1
    assert latest_job.lease_expires_at is not None
    assert ActiveRunJobAuthorityGuard(database).require_run_running(new.run_id) == job.id
