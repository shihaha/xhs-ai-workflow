from backend.app.agent_runtime.job_bound_runtime import JobBoundAgentRuntime
from backend.app.agent_runtime.job_terminal_runtime import (
    TerminalProjectingJobBoundAgentRuntime,
)


def test_terminal_compatibility_name_is_canonical_job_bound_runtime() -> None:
    assert TerminalProjectingJobBoundAgentRuntime is JobBoundAgentRuntime
