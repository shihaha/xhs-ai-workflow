"""Tests for unified execution gateway."""

import pytest

from backend.app.services.execution_gateway import ExecutionGateway
from backend.app.services.agent_execution import AgentExecutionService, ExecutionContext
from backend.app.services.job_authority import JobAuthorityGuard, ExecutionBinding, JobSnapshot


def context(state="running"):
    return ExecutionContext(
        binding=ExecutionBinding("job-1", "run-1", None),
        job=JobSnapshot("job-1", state, None),
        run_id="run-1",
    )


def test_gateway_allows_authorized_execution():
    gateway = ExecutionGateway(AgentExecutionService(JobAuthorityGuard()))
    assert gateway.authorize(context())["authorized"] is True


def test_gateway_blocks_human_gate():
    gateway = ExecutionGateway(AgentExecutionService(JobAuthorityGuard()))
    with pytest.raises(Exception):
        gateway.authorize(context("needs_human"))
