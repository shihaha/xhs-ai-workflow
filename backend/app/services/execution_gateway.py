"""Unified execution gateway for model and tool side effects."""

from typing import Any, Callable

from backend.app.services.agent_execution import AgentExecutionService, ExecutionContext


class ExecutionGateway:
    """Single gateway all future agent side effects should cross."""

    def __init__(self, execution: AgentExecutionService) -> None:
        self.execution = execution

    def run(
        self,
        context: ExecutionContext,
        operation: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        self.execution.authorize(context)
        return operation(*args, **kwargs)
