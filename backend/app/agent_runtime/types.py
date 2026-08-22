"""Project-owned Agent Runtime contracts.

These types deliberately do not expose Pydantic AI internals.  The runtime can
therefore change model libraries without rewriting domain tools or persistence.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AgentRunState(str, Enum):
    running = "running"
    needs_human = "needs_human"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class AgentStepKind(str, Enum):
    model = "model"
    permission = "permission"
    tool = "tool"
    checkpoint = "checkpoint"
    output = "output"
    error = "error"


class PermissionDecision(str, Enum):
    allow = "allow"
    ask = "ask"
    deny = "deny"


class NextAction(BaseModel):
    """One bounded model decision; never hidden chain-of-thought."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["tool", "finish"]
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    tool_call_id: str = Field(default_factory=lambda: str(uuid4()))
    final_output: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "NextAction":
        if self.action == "tool":
            if not self.tool_name:
                raise ValueError("tool action requires tool_name")
            if self.final_output is not None:
                raise ValueError("tool action cannot include final_output")
        else:
            if self.tool_name is not None or self.arguments:
                raise ValueError("finish action cannot include tool data")
            if self.final_output is None:
                raise ValueError("finish action requires final_output")
        return self


class ToolExecutionResult(BaseModel):
    """Structured, persistable tool result."""

    output: Any = None
    evidence_refs: list[str] = Field(default_factory=list)
    summary: str | None = None


class ModelTurn(BaseModel):
    """Validated model decision plus provider usage accounting."""

    action: NextAction
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    model_name: str | None = None


class RunBudget(BaseModel):
    max_steps: int = Field(default=24, ge=1)
    max_model_calls: int = Field(default=12, ge=1)
    max_input_tokens: int = Field(default=120_000, ge=1)
    max_output_tokens: int = Field(default=20_000, ge=1)
    max_wall_time_seconds: float = Field(default=300.0, gt=0)


class RunUsage(BaseModel):
    model_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class RuntimeOutcome(BaseModel):
    run_id: str
    state: AgentRunState
    final_output: dict[str, Any] | None = None
    needs_human_action_id: str | None = None
    error_category: str | None = None
    error_detail: str | None = None
    usage: RunUsage = Field(default_factory=RunUsage)
