"""Tests for guarded tool execution."""

import pytest

from backend.app.services.agent_execution import (
    AgentExecutionService,
    ExecutionContext,
)
from backend.app.services.job_authority import (
    ExecutionBinding,
    JobAuthorityGuard,
    JobSnapshot,
)
from backend.app.services.tool_execution import ToolExecutionService


def make_context(state="running"):
    return ExecutionContext(
        binding=ExecutionBinding(
            job_id="job-1",
            run_id="run-1",
            lease_expires_at=None,
        ),
        job=JobSnapshot(
            id="job-1",
            state=state,
            lease_expires_at=None,
        ),
        run_id="run-1",
    )


def test_tool_runs_after_authority_check():
    result = ToolExecutionService(
        AgentExecutionService(JobAuthorityGuard())
    ).execute(make_context(), lambda: "ok")
    assert result == "ok"


def test_tool_blocked_after_cancel():
    with pytest.raises(Exception):
        ToolExecutionService(
            AgentExecutionService(JobAuthorityGuard())
        ).execute(make_context("cancelled"), lambda: "should-not-run")
