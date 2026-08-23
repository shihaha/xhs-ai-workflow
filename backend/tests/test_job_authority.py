from datetime import UTC, datetime, timedelta

import pytest

from backend.app.services.job_authority import (
    ExecutionBinding,
    JobAuthorityDenied,
    JobAuthorityGuard,
    JobSnapshot,
)


def test_terminal_job_is_blocked():
    guard = JobAuthorityGuard()
    with pytest.raises(JobAuthorityDenied):
        guard.check(
            ExecutionBinding("job-1", "run-1", None),
            JobSnapshot("job-1", "cancelled", None),
        )


def test_expired_lease_is_blocked():
    guard = JobAuthorityGuard()
    with pytest.raises(JobAuthorityDenied):
        guard.check(
            ExecutionBinding(
                "job-1",
                "run-1",
                datetime.now(UTC) - timedelta(minutes=1),
            ),
            JobSnapshot("job-1", "running", None),
        )


def test_matching_running_job_passes():
    guard = JobAuthorityGuard()
    guard.check(
        ExecutionBinding("job-1", "run-1", None),
        JobSnapshot("job-1", "running", None),
    )
