"""Final lifecycle adapter for Job-bound Agent terminal projection.

This stacked slice keeps the already-proven ``JobBoundAgentRuntime`` unchanged
and adds only the terminal return boundary.  Once dynamically accepted it can be
folded into the canonical Job-bound adapter without duplicating its loop.
"""

from __future__ import annotations

from typing import Any

from backend.app.agent_runtime.job_bound_runtime import JobBoundAgentRuntime
from backend.app.agent_runtime.job_terminal_projection import JobTerminalProjector
from backend.app.agent_runtime.types import AgentRunState, RuntimeOutcome


class TerminalProjectingJobBoundAgentRuntime(JobBoundAgentRuntime):
    """Project succeeded/failed AgentRun proof into the authoritative Job."""

    def __init__(
        self,
        *args: Any,
        terminal_projector: JobTerminalProjector | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.terminal_projector = terminal_projector or JobTerminalProjector(
            self.authority_guard.database
        )

    def _project_wait_if_needed(
        self,
        run_id: str,
        outcome: RuntimeOutcome,
    ) -> RuntimeOutcome:
        # Preserve the already-proven human-wait projection first.
        outcome = super()._project_wait_if_needed(run_id, outcome)
        if outcome.state in {AgentRunState.succeeded, AgentRunState.failed}:
            # The AgentRun final output/error step and terminal state have already
            # committed before this call.  The projector only performs the Job
            # CAS; it never replays model or Tool work.
            self.terminal_projector.project_terminal(run_id)
        return outcome
