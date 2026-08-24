"""Strict external and model schemas for grounded analysis."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


EvidenceId = str
OpportunityStatus = Literal["观察中", "升温", "已验证", "降温", "放弃"]
OpportunityReviewStatus = Literal["pending_review", "approved", "rejected"]
OpportunityEvidenceLevel = Literal[
    "warming_candidate", "validated_candidate", "legacy_ungraded"
]
_MAX_SQLITE_ID = 9_223_372_036_854_775_807


def _canonical_evidence_ids(value: list[str]) -> list[str]:
    import re

    pattern = re.compile(
        r"^(?:account-note|artifact|rank-item):([1-9][0-9]*)$", re.ASCII
    )
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


class AccountDemandProfile(_StrictModel):
    account_user_id: str = Field(min_length=1, max_length=500)
    primary_offering: str = Field(min_length=1, max_length=2000)
    target_user: str = Field(min_length=1, max_length=2000)
    core_purchase_motivation: str = Field(min_length=1, max_length=2000)
    delivery_format: str = Field(min_length=1, max_length=2000)
    usage_scenarios: list[str] = Field(min_length=1, max_length=20)
    evidence_ids: list[EvidenceId] = Field(min_length=1)

    _validate_evidence_ids = field_validator("evidence_ids")(_canonical_evidence_ids)


class CrossAccountDemandConclusion(_StrictModel):
    has_specific_shared_demand: bool
    common_demand: str | None = Field(default=None, max_length=2000)
    commonalities: list[str] = Field(max_length=20)
    key_differences: list[str] = Field(max_length=20)
    rationale: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[EvidenceId] = Field(min_length=1)

    _validate_evidence_ids = field_validator("evidence_ids")(_canonical_evidence_ids)

    @model_validator(mode="after")
    def require_specific_demand_only_for_positive_conclusion(
        self,
    ) -> "CrossAccountDemandConclusion":
        if self.has_specific_shared_demand:
            if self.common_demand is None or not self.common_demand.strip():
                raise ValueError("a positive conclusion requires a specific common demand")
            if not self.commonalities:
                raise ValueError("a positive conclusion requires concrete commonalities")
            self.common_demand = self.common_demand.strip()
        elif self.common_demand is not None:
            raise ValueError("a negative conclusion must not claim a common demand")
        return self


class OpportunityAccountSupport(_StrictModel):
    account_user_id: str = Field(min_length=1, max_length=500)
    shop_evidence_ids: list[EvidenceId] = Field(min_length=1, max_length=100)
    note_evidence_ids: list[EvidenceId] = Field(min_length=1, max_length=500)

    _validate_shop_ids = field_validator("shop_evidence_ids")(_canonical_evidence_ids)
    _validate_note_ids = field_validator("note_evidence_ids")(_canonical_evidence_ids)

    @field_validator("account_user_id")
    @classmethod
    def normalize_account_user_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("account_user_id must not be blank")
        return normalized


class OpportunityCard(_StrictModel):
    title: str = Field(min_length=1, max_length=500)
    status: OpportunityStatus
    summary: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[EvidenceId] = Field(min_length=1)
    next_action: str = Field(min_length=1, max_length=4000)
    supporting_accounts: list[OpportunityAccountSupport] = Field(min_length=2)

    _validate_evidence_ids = field_validator("evidence_ids")(_canonical_evidence_ids)

    @field_validator("supporting_accounts")
    @classmethod
    def unique_supporting_accounts(
        cls, value: list[OpportunityAccountSupport]
    ) -> list[OpportunityAccountSupport]:
        accounts = [item.account_user_id for item in value]
        if len(accounts) != len(set(accounts)):
            raise ValueError("supporting_accounts must be unique")
        return value


class AnalysisOutput(_StrictModel):
    claims: list[CitedClaim] = Field(min_length=1)
    product_clusters: list[ProductCluster]
    account_demand_profiles: list[AccountDemandProfile] = Field(default_factory=list)
    cross_account_conclusion: CrossAccountDemandConclusion | None = None
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
        elif self.account_user_id is not None or len(self.account_user_ids) < 2:
            raise ValueError(
                "cross-account analyses require at least two distinct accounts"
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


class SupportingProductRead(_StrictModel):
    account_user_id: str
    evidence_id: EvidenceId
    product_id: str
    title: str | None
    source_url: str
    image_evidence_count: int = Field(ge=0)


class SupportingNoteRead(_StrictModel):
    account_user_id: str
    evidence_id: EvidenceId
    note_id: str
    title: str | None
    source_url: str


class OpportunityReviewCreate(_StrictModel):
    decision: Literal["approve", "reject"]
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def require_rejection_reason(self) -> "OpportunityReviewCreate":
        if self.decision == "reject":
            if self.reason is None or not self.reason.strip():
                raise ValueError("reject requires a non-empty reason")
            self.reason = self.reason.strip()
        elif self.reason is not None:
            raise ValueError("approve does not accept a rejection reason")
        return self


class OpportunityRead(_StrictModel):
    id: str
    analysis_id: str
    title: str
    status: OpportunityStatus
    summary: str
    evidence_ids: list[str]
    review_status: OpportunityReviewStatus
    evidence_level: OpportunityEvidenceLevel
    supporting_account_count: int = Field(ge=0)
    supporting_accounts: list[OpportunityAccountSupport]
    supporting_products: list[SupportingProductRead]
    supporting_notes: list[SupportingNoteRead]
    reviewed_at: datetime | None
    rejection_reason: str | None
    next_action: str
    created_at: datetime


class AnalysisEvidenceRead(_StrictModel):
    evidence_id: str
    kind: str
    account_user_id: str | None
    source_date: str | None = None
    eligible_for_opportunity: bool
