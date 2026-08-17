"""Strict external and model schemas for grounded analysis."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


EvidenceId = str
OpportunityStatus = Literal["观察中", "升温", "已验证", "降温", "放弃"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CitedClaim(_StrictModel):
    claim: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[EvidenceId] = Field(min_length=1)


class ProductCluster(_StrictModel):
    name: str = Field(min_length=1, max_length=500)
    summary: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[EvidenceId] = Field(min_length=1)


class OpportunityCard(_StrictModel):
    title: str = Field(min_length=1, max_length=500)
    status: OpportunityStatus
    summary: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[EvidenceId] = Field(min_length=1)
    next_action: str = Field(min_length=1, max_length=4000)


class AnalysisOutput(_StrictModel):
    claims: list[CitedClaim] = Field(min_length=1)
    product_clusters: list[ProductCluster]
    opportunities: list[OpportunityCard]


class AnalysisCreate(_StrictModel):
    analysis_type: Literal["account_report", "product_cluster", "account_opportunity"]
    account_user_id: str | None = Field(default=None, min_length=1, max_length=500)
    evidence_ids: list[EvidenceId] = Field(min_length=1, max_length=500)

    @field_validator("evidence_ids")
    @classmethod
    def unique_non_empty_evidence_ids(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("evidence ids must contain non-whitespace text")
        if len(value) != len(set(value)):
            raise ValueError("evidence ids must be unique")
        return value


class AnalysisRead(_StrictModel):
    id: str
    analysis_type: str
    account_user_id: str | None
    status: Literal["succeeded", "failed", "needs_human"]
    prompt_version: str
    provider: str
    model: str
    input_digest: str
    evidence_ids: list[str]
    output: AnalysisOutput | None
    usage: dict[str, int]
    duration_ms: int | None
    attempts: list[dict[str, object]]
    error_category: str | None
    error_detail: str | None
    created_at: datetime


class OpportunityRead(_StrictModel):
    id: str
    analysis_id: str
    title: str
    status: OpportunityStatus
    summary: str
    evidence_ids: list[str]
    next_action: str
    created_at: datetime


class AnalysisEvidenceRead(_StrictModel):
    evidence_id: str
    kind: str
    account_user_id: str | None
    eligible_for_opportunity: bool
