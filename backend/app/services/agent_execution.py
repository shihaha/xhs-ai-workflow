"""Guarded execution boundary primitives.

This module defines the single entry boundary that future agent executors must
pass through before model/tool side effects.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from backend.app.services.job_authority import (
    ExecutionBinding,
    JobAuthorityGuard,
    JobSnapshot,
)


@dataclass(frozen=True)
class ExecutionContext:
    binding: ExecutionBinding
    job: JobSnapshot
    run_id: str


class AgentExecutionDenied(Exception):
    """Raised when an execution attempt is not authorized."""


class AgentExecutionService:
    def __init__(self, authority: JobAuthorityGuard) -> None:
        self.authority = authority

    def authorize(self, context: ExecutionContext) -> dict[str, Any]:
        """Validate execution before any future model/tool side effect."""
        try:
            self.authority.check(context.binding, context.job)
        except Exception as exc:
            raise AgentExecutionDenied(str(exc)) from exc

        return {
            "job_id": context.job.id,
            "run_id": context.run_id,
            "authorized": True,
        }
