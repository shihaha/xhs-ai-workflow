"""Centralized tool permission policy.

Domain tools never decide their own final authorization.  Metadata can request
approval, but the application policy owns allow/ask/deny.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from backend.app.agent_runtime.tools import ToolSpec
from backend.app.agent_runtime.types import PermissionDecision


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    run_id: str
    tool_call_id: str
    tool: ToolSpec
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PermissionResult:
    decision: PermissionDecision
    reason: str


class PermissionPolicy(Protocol):
    def decide(self, request: PermissionRequest) -> PermissionResult: ...


@dataclass(slots=True)
class RuleBasedPermissionPolicy:
    """Conservative Stage 1 default with explicit per-tool overrides."""

    overrides: dict[str, PermissionDecision] = field(default_factory=dict)

    def decide(self, request: PermissionRequest) -> PermissionResult:
        override = self.overrides.get(request.tool.name)
        if override is not None:
            return PermissionResult(override, "explicit tool policy override")
        if request.tool.destructive:
            return PermissionResult(PermissionDecision.ask, "destructive tool requires approval")
        if request.tool.requires_approval:
            return PermissionResult(PermissionDecision.ask, "tool metadata requires approval")
        if request.tool.read_only:
            return PermissionResult(PermissionDecision.allow, "bounded read-only tool")
        return PermissionResult(PermissionDecision.ask, "state-changing tool requires approval")
