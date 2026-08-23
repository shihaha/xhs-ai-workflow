"""State matrix coverage for execution authority."""

import pytest

from backend.app.services.agent_execution import AgentExecutionDenied, AgentExecutionService, ExecutionContext
from backend.app.services.job_authority import ExecutionBinding, JobAuthorityGuard, JobSnapshot


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled", "needs_human", "paused", "blocked"])
def test_non_executable_states_are_denied(state):
    service = AgentExecutionService(JobAuthorityGuard())
    context = ExecutionContext(
        binding=ExecutionBinding("job-1", "run-1", None),
        job=JobSnapshot("job-1", state, None),
        run_id="run-1",
    )
    with pytest.raises(AgentExecutionDenied):
        service.authorize(context)
