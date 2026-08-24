"""Read models for the local Agent workbench surface."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


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
    approval_summary: str | None = None
    external_side_effect: bool = False
    can_deny: bool = False
    can_approve: bool = False
    created_at: datetime
    resolved_at: datetime | None = None


class HumanActionWorkbenchRead(HumanActionSummaryRead):
    job_id: str


class JobArtifactSummaryRead(BaseModel):
    id: int
    job_id: str
    kind: str
    producer: str
    created_at: datetime


class AgentJobSummaryRead(BaseModel):
    job_id: str
    job_state: str
    current_stage: str | None = None
    error_category: str | None = None
    retry_count: int
    current_run_id: str | None = None
    authority_ambiguous: bool = False
    run_count: int
    pending_human_action_count: int
    evidence_count: int
    artifact_count: int
    created_at: datetime
    updated_at: datetime


class AgentJobRuntimeRead(BaseModel):
    job_id: str
    job_state: str
    current_stage: str | None = None
    error_category: str | None = None
    retry_count: int
    lease_expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    current_run_id: str | None = None
    authority_ambiguous: bool = False
    runs: list[AgentRunSummaryRead] = Field(default_factory=list)
    pending_human_actions: list[HumanActionSummaryRead] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    artifacts: list[JobArtifactSummaryRead] = Field(default_factory=list)


class AgentRunListItemRead(AgentRunSummaryRead):
    job_id: str


class AgentRunDetailRead(AgentRunSummaryRead):
    job_id: str
    can_request_interrupted_reapproval: bool = False
    steps: list[AgentStepRead] = Field(default_factory=list)
    human_actions: list[HumanActionSummaryRead] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class AgentEvidenceRefsRead(BaseModel):
    job_id: str
    evidence_refs: list[str] = Field(default_factory=list)


class ChatGPTHandoffTaskRead(BaseModel):
    """Redacted lifecycle/identity projection for one durable ChatGPT handoff."""

    handoff_id: str
    job_id: str
    source_run_id: str
    human_action_id: str
    status: str
    human_action_status: str
    job_state: str
    current_stage: str | None = None
    stage_revision: str
    schema_version: str
    input_hash: str
    context_ref_count: int
    has_result: bool
    is_current_binding: bool
    authority_ambiguous: bool
    needs_chatgpt: bool
    result_ready: bool
    created_at: datetime
    accepted_at: datetime | None = None


class AgentActionCapabilitiesRead(BaseModel):
    cancel_job: bool
    deny_permission_action: bool
    approve_continuation: bool
    approve_physical_continuation: bool = False
    start_grounded_orchestration: bool = False
    continuation_reason: str | None = None


class AgentGroundedOrchestrationCreate(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)

    @field_validator("goal")
    @classmethod
    def normalize_goal(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("goal must not be blank")
        return normalized

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("evidence_ids must be unique")
        pattern = re.compile(r"^(?:account-note|artifact|rank-item):[1-9][0-9]*$", re.ASCII)
        if any(pattern.fullmatch(item) is None for item in value):
            raise ValueError("evidence_ids must use canonical durable evidence identities")
        return value


class AgentGroundedOrchestrationRead(BaseModel):
    job_id: str
    run_id: str
    evidence_count: int = Field(ge=1, le=20)
    dispatch_enqueued: bool
    handoff_created: bool = False
    handoff_id: str | None = None


class AgentJobCancelRead(BaseModel):
    job_id: str
    job_state: Literal["cancelled"]
    cancelled_run_ids: list[str] = Field(default_factory=list)
    resolved_human_action_ids: list[str] = Field(default_factory=list)
    already_cancelled: bool = False


class HumanActionDenyRequest(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class HumanActionApproveRequest(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class HumanActionApprovalRead(BaseModel):
    human_action_id: str
    job_id: str
    source_run_id: str
    continuation_run_id: str
    human_action_status: Literal["approved"]
    continuation_enqueued: Literal[True]


class InterruptedReapprovalRead(BaseModel):
    human_action_id: str
    job_id: str
    run_id: str
    tool_name: str
    human_action_status: Literal["pending"]


class HumanActionDecisionRead(BaseModel):
    human_action_id: str
    job_id: str
    run_id: str
    human_action_status: Literal["denied"]
    job_state: Literal["failed"]
    run_state: Literal["failed"]
