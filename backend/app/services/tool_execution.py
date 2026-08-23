"""Guarded tool execution boundary.

All future external side effects from tools should pass through this layer
after job authority has been verified.
"""

from typing import Any, Callable

from backend.app.services.agent_execution import (
    AgentExecutionDenied,
    AgentExecutionService,
    ExecutionContext,
)


class ToolExecutionService:
    """Execute tools only after the agent execution boundary is authorized."""

    def __init__(self, execution: AgentExecutionService) -> None:
        self.execution = execution

    def execute(
        self,
        context: ExecutionContext,
        tool: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        self.execution.authorize(context)
        return tool(*args, **kwargs)
