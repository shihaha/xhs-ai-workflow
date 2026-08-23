"""Tests for guarded model execution."""

from backend.app.services.agent_execution import AgentExecutionService, ExecutionContext
from backend.app.services.job_authority import ExecutionBinding, JobAuthorityGuard, JobSnapshot
from backend.app.services.model_execution import ModelExecutionService


def context(state="running"):
    return ExecutionContext(
        binding=ExecutionBinding(job_id="job-1", run_id="run-1", lease_expires_at=None),
        job=JobSnapshot(id="job-1", state=state, lease_expires_at=None),
        run_id="run-1",
    )


def test_model_runs_after_authority_check():
    result = ModelExecutionService(
        AgentExecutionService(JobAuthorityGuard())
    ).execute(context(), lambda: "ok")
    assert result == "ok"


def test_model_blocked_after_human_gate():
    try:
        ModelExecutionService(
            AgentExecutionService(JobAuthorityGuard())
        ).execute(context("needs_human"), lambda: "bad")
    except Exception:
        return
    raise AssertionError("model execution should be blocked")
