"""Fail-closed authority checks for agent/job execution.

This module intentionally does not mutate job state. It only validates whether an
execution context is still allowed to continue before model/tool boundaries.
"""

from dataclasses import dataclass
from datetime import UTC, datetime


class JobAuthorityDenied(Exception):
    """Raised when an execution attempt is no longer authorized."""


@dataclass(frozen=True)
class ExecutionBinding:
    job_id: str
    run_id: str
    lease_expires_at: datetime | None


@dataclass(frozen=True)
class JobSnapshot:
    id: str
    state: str
    lease_expires_at: datetime | None


_TERMINAL_STATES = {"succeeded", "failed", "cancelled"}
_HUMAN_GATED_STATES = {"needs_human", "paused", "blocked"}


class JobAuthorityGuard:
    """Validate execution authority before crossing unsafe boundaries."""

    def check(self, binding: ExecutionBinding, job: JobSnapshot) -> None:
        if binding.job_id != job.id:
            raise JobAuthorityDenied("Execution binding does not match job.")

        if job.state in _TERMINAL_STATES:
            raise JobAuthorityDenied(
                f"Job is terminal and cannot continue: {job.state}."
            )

        if job.state in _HUMAN_GATED_STATES:
            raise JobAuthorityDenied(
                f"Job requires human handling and cannot continue: {job.state}."
            )

        expires_at = binding.lease_expires_at or job.lease_expires_at
        if expires_at is not None and expires_at <= datetime.now(UTC):
            raise JobAuthorityDenied("Execution lease expired.")
