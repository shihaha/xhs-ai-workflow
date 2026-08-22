"""Tool protocol and registry for the bounded Agent Runtime."""

from __future__ import annotations

import inspect
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


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[BaseModel], ToolExecutionResult]
    read_only: bool = True
    destructive: bool = False
    concurrency_safe: bool = True
    requires_approval: bool = False
    available: Callable[[], bool] = lambda: True
    timeout_seconds: float = 30.0
    idempotent: bool = True


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, tool: ToolSpec) -> None:
        if not tool.name or tool.name in self._tools:
            raise ToolRegistrationError(f"duplicate or empty tool name: {tool.name!r}")
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
        try:
            return tool.input_model.model_validate(arguments)
        except ValidationError as exc:
            raise ToolInputError(str(exc)) from exc

    def execute_validated(self, tool: ToolSpec, validated: BaseModel) -> ToolExecutionResult:
        result = tool.handler(validated)
        if inspect.isawaitable(result):
            raise TypeError("async tool handlers are not supported by the Stage 1 sync runtime")
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
                    "description": tool.description,
                    "input_schema": tool.input_model.model_json_schema(),
                    "read_only": tool.read_only,
                    "destructive": tool.destructive,
                    "requires_approval": tool.requires_approval,
                    "idempotent": tool.idempotent,
                }
            )
        return definitions
