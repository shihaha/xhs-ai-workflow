"""Bounded context construction for model decisions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, Field


class StepView(BaseModel):
    step_index: int
    kind: str
    tool_name: str | None = None
    status: str
    input_summary: str | None = None
    output_summary: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)


class RuntimeContext(BaseModel):
    goal: str
    run_id: str
    usage: dict[str, int]
    remaining_budget: dict[str, int | float]
    tools: list[dict[str, Any]]
    recent_steps: list[StepView]


class ContextStep(Protocol):
    step_index: int
    kind: str
    tool_name: str | None
    status: str
    input_json: dict[str, Any] | None
    output_json: dict[str, Any] | None
    evidence_refs_json: list[str] | None


def _bounded_json(value: Any, max_chars: int) -> str | None:
    if value is None:
        return None
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(rendered) <= max_chars:
        return rendered
    keep = max(0, max_chars - 32)
    return rendered[:keep] + "…[truncated]"


@dataclass(slots=True)
class DefaultContextBuilder:
    max_recent_steps: int = 20
    max_step_chars: int = 2_000

    def build(
        self,
        *,
        goal: str,
        run_id: str,
        steps: list[ContextStep],
        tools: list[dict[str, Any]],
        usage: dict[str, int],
        remaining_budget: dict[str, int | float],
    ) -> RuntimeContext:
        recent = steps[-self.max_recent_steps :]
        return RuntimeContext(
            goal=goal,
            run_id=run_id,
            usage=usage,
            remaining_budget=remaining_budget,
            tools=tools,
            recent_steps=[
                StepView(
                    step_index=step.step_index,
                    kind=step.kind,
                    tool_name=step.tool_name,
                    status=step.status,
                    input_summary=_bounded_json(step.input_json, self.max_step_chars),
                    output_summary=_bounded_json(step.output_json, self.max_step_chars),
                    evidence_refs=list(step.evidence_refs_json or []),
                )
                for step in recent
            ],
        )
