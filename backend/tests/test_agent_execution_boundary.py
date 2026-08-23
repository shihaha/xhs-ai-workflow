"""Tests for the guarded agent execution boundary."""

from datetime import UTC, datetime, timedelta

import pytest

from backend.app.services.agent_execution import (
    AgentExecutionDenied,
    AgentExecutionService,
    ExecutionContext,
)
from backend.app.services.job_authority import (
    ExecutionBinding,
    JobAuthorityGuard,
    JobSnapshot,
)


def context(state="running", expired=False, mismatch=False):
    expires = datetime.now(UTC) + timedelta(minutes=-1 if expired else 5)
    return ExecutionContext(
        binding=ExecutionBinding(
            job_id="job-1" if not mismatch else "job-x",
            run_id="run-1",
            lease_expires_at=expires,
        ),
        job=JobSnapshot(
            id="job-1",
            state=state,
            lease_expires_at=expires,
        ),
        run_id="run-1",
    )


def test_running_job_is_authorized():
    result = AgentExecutionService(JobAuthorityGuard()).authorize(context())
    assert result["authorized"] is True


@pytest.mark.parametrize("ctx", [
    context(state="cancelled"),
    context(expired=True),
    context(mismatch=True),
])
def test_invalid_execution_is_denied(ctx):
    with pytest.raises(AgentExecutionDenied):
        AgentExecutionService(JobAuthorityGuard()).authorize(ctx)
