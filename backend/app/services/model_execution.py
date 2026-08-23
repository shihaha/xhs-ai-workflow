"""Guarded model execution boundary.

All future model calls should pass through this layer after execution
authorization has been verified.
"""

from typing import Any, Callable

from backend.app.services.agent_execution import (
    AgentExecutionService,
    ExecutionContext,
)


class ModelExecutionService:
    """Execute model operations only after authority validation."""

    def __init__(self, execution: AgentExecutionService) -> None:
        self.execution = execution

    def execute(
        self,
        context: ExecutionContext,
        model_call: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        self.execution.authorize(context)
        return model_call(*args, **kwargs)
