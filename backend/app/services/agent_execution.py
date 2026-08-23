"""Guarded execution boundary primitives.

This module intentionally does not call models or tools yet. It defines the
single entry boundary that future agent executors must pass through.
"""

from dataclasses import dataclass
from typing import Any

from backend.app.services.job_authority import JobAuthorityGuard


@dataclass(frozen=True)
class ExecutionContext:
    job_id: str
    run_id: str
    binding_job_id: str


class AgentExecutionDenied(Exception):
    """Raised when an execution attempt is not authorized."""


class AgentExecutionService:
    def __init__(self, authority: JobAuthorityGuard) -> None:
        self.authority = authority

    def authorize(self, context: ExecutionContext) -> dict[str, Any]:
        """Validate execution before any future model/tool side effect."""
        self.authority.check(
            job_id=context.job_id,
            binding_job_id=context.binding_job_id,
        )
        return {
            "job_id": context.job_id,
            "run_id": context.run_id,
            "authorized": True,
        }
