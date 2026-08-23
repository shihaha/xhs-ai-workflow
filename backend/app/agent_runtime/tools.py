"""Tool protocol and registry for the bounded Agent Runtime."""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from backend.app.agent_runtime.types import ToolExecutionResult


class ToolRegistrationError(ValueError):
    pass


class ToolUnavailableError(RuntimeError):
    pass


class ToolInputError(ValueError):
    pass


class ToolTimeoutError(TimeoutError):
    pass


class ToolControlError(RuntimeError):
    """A tool completed enough work to return a durable domain control outcome."""

    def __init__(
        self,
        *,
        category: str,
        detail: str,
        result: ToolExecutionResult | None = None,
    ) -> None:
        super().__init__(detail)
        self.category = category
        self.detail = detail
        self.result = result


class ToolNeedsHumanError(ToolControlError):
    """The domain workflow requires explicit human intervention before continuing."""


class ToolDomainFailureError(ToolControlError):
    """The domain workflow reached a known durable failed outcome."""


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    run_id: str
    tool_call_id: str
    timeout_seconds: float
    deadline_monotonic: float
    attempt: int = 1

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline_monotonic - time.monotonic())

    def check_deadline(self) -> None:
        if self.remaining_seconds() <= 0:
            raise ToolTimeoutError(
                f"tool call {self.tool_call_id} exceeded {self.timeout_seconds:.3f}s deadline"
            )


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[..., ToolExecutionResult]
    version: str = "1"
    read_only: bool = True
    destructive: bool = False
    external_side_effect: bool = False
    concurrency_safe: bool = True
    requires_approval: bool = False
    available: Callable[[], bool] = lambda: True
    timeout_seconds: float = 30.0
    idempotent: bool = True
    retry_limit: int = 0


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, tool: ToolSpec) -> None:
        if not tool.name or tool.name in self._tools:
            raise ToolRegistrationError(f"duplicate or empty tool name: {tool.name!r}")
        if tool.timeout_seconds <= 0:
            raise ToolRegistrationError(f"tool timeout must be positive: {tool.name}")
        if tool.retry_limit < 0:
            raise ToolRegistrationError(f"tool retry_limit cannot be negative: {tool.name}")
        self._tools[tool.name] = tool

    def resolve(self, name: str) -> ToolSpec:
        try:
            tool = self._tools[name]
        except KeyError as exc:
            raise ToolUnavailableError(f"unknown tool: {name}") from exc
        if not tool.available():
            raise ToolUnavailableError(f"tool is unavailable: {name}")
        return tool

    def validate(self, tool: ToolSpec, arguments: dict[str, Any]) -> BaseModel:
        known_fields = set(tool.input_model.model_fields)
        unknown_fields = sorted(set(arguments) - known_fields)
        if unknown_fields:
            raise ToolInputError(
                f"unknown arguments for {tool.name}: {', '.join(unknown_fields)}"
            )
        try:
            return tool.input_model.model_validate(arguments)
        except ValidationError as exc:
            raise ToolInputError(str(exc)) from exc

    def execute_validated(
        self,
        tool: ToolSpec,
        validated: BaseModel,
        *,
        run_id: str,
        tool_call_id: str,
        attempt: int = 1,
    ) -> ToolExecutionResult:
        execution_context = ToolExecutionContext(
            run_id=run_id,
            tool_call_id=tool_call_id,
            timeout_seconds=tool.timeout_seconds,
            deadline_monotonic=time.monotonic() + tool.timeout_seconds,
            attempt=attempt,
        )
        execution_context.check_deadline()

        parameters = inspect.signature(tool.handler).parameters
        if len(parameters) >= 2:
            result = tool.handler(validated, execution_context)
        else:
            # Stage 1 backward-compatible path for simple deterministic fake tools.
            result = tool.handler(validated)

        if inspect.isawaitable(result):
            raise TypeError("async tool handlers are not supported by the Stage 1 sync runtime")
        execution_context.check_deadline()
        if not isinstance(result, ToolExecutionResult):
            raise TypeError("tool handler must return ToolExecutionResult")
        return result

    def public_definitions(self) -> list[dict[str, Any]]:
        definitions: list[dict[str, Any]] = []
        for tool in sorted(self._tools.values(), key=lambda item: item.name):
            if not tool.available():
                continue
            definitions.append(
                {
                    "name": tool.name,
                    "version": tool.version,
                    "description": tool.description,
                    "input_schema": tool.input_model.model_json_schema(),
                    "read_only": tool.read_only,
                    "destructive": tool.destructive,
                    "external_side_effect": tool.external_side_effect,
                    "requires_approval": tool.requires_approval,
                    "timeout_seconds": tool.timeout_seconds,
                    "idempotent": tool.idempotent,
                    "retry_limit": tool.retry_limit,
                }
            )
        return definitions
