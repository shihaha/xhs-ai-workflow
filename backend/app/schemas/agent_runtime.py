"""Read models for the local Agent workbench surface."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AgentRunSummaryRead(BaseModel):
    run_id: str
    goal: str
    state: str
    model_name: str | None = None
    prompt_version: str | None = None
    step_count: int
    model_calls: int
    input_tokens: int
    output_tokens: int
    final_output: dict[str, Any] | None = None
    error_category: str | None = None
    error_detail: str | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class AgentStepRead(BaseModel):
    step_index: int
    kind: str
    tool_name: str | None = None
    tool_call_id: str | None = None
    status: str
    evidence_refs: list[str] = Field(default_factory=list)
    error_category: str | None = None
    error_detail: str | None = None
    created_at: datetime
    updated_at: datetime


class HumanActionSummaryRead(BaseModel):
    id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    status: str
    created_at: datetime
    resolved_at: datetime | None = None


class AgentJobRuntimeRead(BaseModel):
    job_id: str
    job_state: str
    current_stage: str | None = None
    error_category: str | None = None
    retry_count: int
    lease_expires_at: datetime | None = None
    current_run_id: str | None = None
    authority_ambiguous: bool = False
    runs: list[AgentRunSummaryRead] = Field(default_factory=list)
    pending_human_actions: list[HumanActionSummaryRead] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class AgentRunDetailRead(AgentRunSummaryRead):
    job_id: str
    steps: list[AgentStepRead] = Field(default_factory=list)
    human_actions: list[HumanActionSummaryRead] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
