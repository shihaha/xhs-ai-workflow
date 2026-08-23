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


def test_old_wait_cannot_capture_new_claim_after_initial_binding_check(
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
    original_require = projector.guard.require_current_binding
    new_bound = {"value": None}

    def require_then_reclaim(run_id: str) -> str:
        job_id = original_require(run_id)
        # Interleave after the initial current-binding check but before the
        # projector reads Job. Without binding identity inside the UPDATE, the
        # stale projector could capture this new retry/lease and clobber it.
        JobService(database).transition(job_id, JobState.needs_human)
        new_bound["value"] = coordinator.claim_and_create_run(
            job_id,
            goal="new continuation claim",
            budget=RunBudget(max_wall_time_seconds=30),
        )
        return job_id

    monkeypatch.setattr(projector.guard, "require_current_binding", require_then_reclaim)

    with pytest.raises(JobWaitProjectionError, match="claim/binding changed"):
        projector.project_wait(old.run_id)

    new = new_bound["value"]
    assert new is not None
    latest_job = JobService(database).get(job.id)
    assert latest_job.state is JobState.running
    assert latest_job.retry_count == 1
    assert latest_job.lease_expires_at is not None
    assert ActiveRunJobAuthorityGuard(database).require_run_running(new.run_id) == job.id
