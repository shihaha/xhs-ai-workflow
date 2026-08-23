"""Tests for the guarded agent execution boundary."""

from dataclasses import dataclass

import pytest

from backend.app.services.agent_execution import (
    AgentExecutionService,
    ExecutionContext,
)


@dataclass
class FakeAuthority:
    allowed: bool = True

    def check(self, *, job_id: str, binding_job_id: str):
        if not self.allowed:
            raise RuntimeError("execution denied")
        return None


def test_execution_boundary_requires_authority_check():
    service = AgentExecutionService(FakeAuthority())

    result = service.authorize(
        ExecutionContext(
            job_id="job-1",
            run_id="run-1",
            binding_job_id="job-1",
        )
    )

    assert result["authorized"] is True
    assert result["run_id"] == "run-1"


def test_execution_boundary_denies_when_authority_rejects():
    service = AgentExecutionService(FakeAuthority(allowed=False))

    with pytest.raises(RuntimeError):
        service.authorize(
            ExecutionContext(
                job_id="job-1",
                run_id="run-1",
                binding_job_id="job-1",
            )
        )
