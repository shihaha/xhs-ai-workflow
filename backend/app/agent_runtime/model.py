"""Model-driver boundary for Agent Runtime.

Only this adapter knows about Pydantic AI.  Runtime/domain code consumes the
project-owned ModelDriver protocol and ModelTurn type.
"""

from __future__ import annotations

from typing import Protocol

from backend.app.agent_runtime.context import RuntimeContext
from backend.app.agent_runtime.types import ModelTurn, NextAction


class ModelDriver(Protocol):
    def next_action(self, context: RuntimeContext) -> ModelTurn: ...


class PydanticDecisionModel:
    """One structured Pydantic AI decision per runtime turn."""

    def __init__(
        self,
        model: object,
        *,
        instructions: str = (
            "Choose exactly one next action. Use only tools present in the supplied "
            "context. Never invent tool success or evidence. Return finish only when "
            "the goal can be truthfully completed from committed results."
        ),
    ) -> None:
        # Import locally so project-owned runtime modules stay framework-neutral.
        from pydantic_ai import Agent

        self._agent = Agent(model, output_type=NextAction, instructions=instructions)

    def next_action(self, context: RuntimeContext) -> ModelTurn:
        result = self._agent.run_sync(context.model_dump_json())
        usage = result.usage
        return ModelTurn(
            action=result.output,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            model_name=result.response.model_name,
        )
