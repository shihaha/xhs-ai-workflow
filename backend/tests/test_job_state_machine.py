from pathlib import Path

import pytest

from backend.app.db import Database
from backend.app.models.jobs import JobState
from backend.app.services.jobs import InvalidJobTransition, JobService


PATHS_TO_STATE = {
    JobState.queued: (),
    JobState.running: (JobState.running,),
    JobState.needs_human: (JobState.running, JobState.needs_human),
    JobState.succeeded: (JobState.running, JobState.succeeded),
    JobState.failed: (JobState.running, JobState.failed),
    JobState.cancelled: (JobState.cancelled,),
}

PERMITTED_TRANSITIONS = {
    (JobState.queued, JobState.running),
    (JobState.queued, JobState.cancelled),
    (JobState.running, JobState.needs_human),
    (JobState.running, JobState.succeeded),
    (JobState.running, JobState.failed),
    (JobState.running, JobState.cancelled),
    (JobState.needs_human, JobState.running),
    (JobState.needs_human, JobState.cancelled),
}


@pytest.mark.parametrize(
    ("source", "target", "permitted"),
    [
        (source, target, (source, target) in PERMITTED_TRANSITIONS)
        for source in JobState
        for target in JobState
    ],
)
def test_job_state_machine_allows_only_fixed_transitions(
    tmp_path: Path, source: JobState, target: JobState, permitted: bool
) -> None:
    """A disallowed state edge must never alter a persisted job."""
    service = JobService(Database(tmp_path / "workbench.sqlite3"))
    job = service.create(job_type="collection", input_data={"query": "tea"})

    for state in PATHS_TO_STATE[source]:
        job = service.transition(job.id, state)
    assert job.state is source

    if permitted:
        assert service.transition(job.id, target).state is target
    else:
        with pytest.raises(InvalidJobTransition):
            service.transition(job.id, target)
        assert service.get(job.id).state is source


def test_expired_running_job_is_recovered_with_a_persistent_log(tmp_path: Path) -> None:
    """A process restart must surface an expired lease for human recovery."""
    database_path = tmp_path / "workbench.sqlite3"
    first_service = JobService(Database(database_path))
    job = first_service.create(job_type="collection", input_data={})
    first_service.claim(job.id, lease_seconds=-1)

    restarted_service = JobService(Database(database_path))

    assert restarted_service.recover_expired_running() == 1
    recovered = restarted_service.get(job.id)
    assert recovered.state is JobState.needs_human
    assert recovered.logs[-1].message == "Running lease expired; human recovery required."
