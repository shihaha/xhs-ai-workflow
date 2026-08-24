"""Safe business-first projections derived from durable domain records."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DemandRadarProductRead(_StrictModel):
    account_user_id: str
    product_id: str
    title: str | None
    source_url: str
    price: str | None = None
    sold: str | None = None
    image_evidence_count: int = Field(ge=0)


class DemandRadarLinkedProductRead(_StrictModel):
    product_id: str
    name: str
    target_user: str
    created_at: datetime


class DemandRadarDirectionRead(_StrictModel):
    opportunity_id: str
    title: str
    summary: str
    evidence_level: Literal[
        "warming_candidate", "validated_candidate", "legacy_ungraded"
    ]
    review_status: Literal["pending_review", "approved", "rejected"]
    can_follow_up: bool
    journey_stage: Literal[
        "demand_decision",
        "product_definition",
        "legacy_product_workspace",
        "closed",
    ]
    is_new_today: bool
    supporting_account_count: int = Field(ge=0)
    supporting_product_count: int = Field(ge=0)
    supporting_note_count: int = Field(ge=0)
    image_evidence_count: int = Field(ge=0)
    representative_products: list[DemandRadarProductRead]
    linked_products: list[DemandRadarLinkedProductRead]
    next_business_action: str
    created_at: datetime
    reviewed_at: datetime | None


class DemandRadarSummaryRead(_StrictModel):
    generated_on: date
    total_direction_count: int = Field(ge=0)
    new_today_count: int = Field(ge=0)
    warming_count: int = Field(ge=0)
    validated_count: int = Field(ge=0)
    pending_decision_count: int = Field(ge=0)
    approved_count: int = Field(ge=0)
    linked_product_count: int = Field(ge=0)


class DemandRadarRead(_StrictModel):
    summary: DemandRadarSummaryRead
    directions: list[DemandRadarDirectionRead]
