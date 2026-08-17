"""Strict external and model schemas for grounded analysis."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


EvidenceId = str
OpportunityStatus = Literal["观察中", "升温", "已验证", "降温", "放弃"]
_MAX_SQLITE_ID = 9_223_372_036_854_775_807


def _canonical_evidence_ids(value: list[str]) -> list[str]:
    import re

    pattern = re.compile(r"^(?:artifact|rank-item):([1-9][0-9]*)$", re.ASCII)
    if len(value) != len(set(value)):
        raise ValueError("evidence ids must be unique")
    for evidence_id in value:
        match = pattern.fullmatch(evidence_id)
        if match is None or int(match.group(1)) > _MAX_SQLITE_ID:
            raise ValueError("evidence ids must be canonical SQLite identities")
    return value


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CitedClaim(_StrictModel):
    claim: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[EvidenceId] = Field(min_length=1)

    _validate_evidence_ids = field_validator("evidence_ids")(_canonical_evidence_ids)


class ProductCluster(_StrictModel):
    name: str = Field(min_length=1, max_length=500)
    summary: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[EvidenceId] = Field(min_length=1)

    _validate_evidence_ids = field_validator("evidence_ids")(_canonical_evidence_ids)


class OpportunityCard(_StrictModel):
    title: str = Field(min_length=1, max_length=500)
    status: OpportunityStatus
    summary: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[EvidenceId] = Field(min_length=1)
    next_action: str = Field(min_length=1, max_length=4000)

    _validate_evidence_ids = field_validator("evidence_ids")(_canonical_evidence_ids)


class AnalysisOutput(_StrictModel):
    claims: list[CitedClaim] = Field(min_length=1)
    product_clusters: list[ProductCluster]
    opportunities: list[OpportunityCard]


class AnalysisCreate(_StrictModel):
    analysis_type: Literal["account_report", "product_cluster", "account_opportunity"]
    account_user_id: str | None = Field(default=None, min_length=1, max_length=500)
    account_user_ids: list[str] = Field(default_factory=list, max_length=500)
    evidence_ids: list[EvidenceId] = Field(min_length=1, max_length=500)

    @field_validator("evidence_ids")
    @classmethod
    def unique_non_empty_evidence_ids(cls, value: list[str]) -> list[str]:
        return _canonical_evidence_ids(value)

    @field_validator("account_user_id")
    @classmethod
    def normalize_account_user_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("account_user_id must not be blank")
        return normalized

    @field_validator("account_user_ids")
    @classmethod
    def normalize_account_user_ids(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item or len(item) > 500 for item in normalized):
            raise ValueError("account_user_ids must be non-empty bounded strings")
        if len(normalized) != len(set(normalized)):
            raise ValueError("account_user_ids must be unique")
        return normalized

    @model_validator(mode="after")
    def require_typed_account_scope(self) -> "AnalysisCreate":
        if self.analysis_type == "account_report":
            if self.account_user_id is None or self.account_user_ids:
                raise ValueError("account_report requires exactly one account_user_id")
        elif self.account_user_id is not None or not self.account_user_ids:
            raise ValueError(
                "cross-account analyses require an explicit account_user_ids collection"
            )
        return self

    @property
    def account_scope(self) -> frozenset[str]:
        if self.account_user_id is not None:
            return frozenset((self.account_user_id,))
        return frozenset(self.account_user_ids)


class AnalysisRead(_StrictModel):
    id: str
    analysis_type: str
    account_user_id: str | None
    account_user_ids: list[str]
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
