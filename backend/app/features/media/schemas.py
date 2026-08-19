"""Strict internal create/read contracts for durable media runs."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.app.db import is_canonical_uuid_text
from backend.app.adapters.contracts import VisualAssessment


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _nonblank(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("value must not be blank")
    return normalized


class ContentMediaRunCreate(_StrictModel):
    id: str = Field(min_length=36, max_length=36)
    job_id: str = Field(min_length=1, max_length=36)
    owner_product_id: str = Field(min_length=1, max_length=36)
    content_item_id: str = Field(min_length=1, max_length=36)
    revision_id: str = Field(min_length=1, max_length=36)
    plan_entry_id: str | None = Field(default=None, min_length=1, max_length=200)
    capability: Literal["generate", "analyze"]
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=300)
    prompt_version: str = Field(min_length=1, max_length=100)
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    allowed_evidence_ids: list[str] = Field(max_length=500)
    allowed_material_ids: list[str] = Field(max_length=500)

    _names = field_validator("provider", "model", "prompt_version")(_nonblank)

    @field_validator("id", "job_id", "owner_product_id", "content_item_id", "revision_id")
    @classmethod
    def canonical_ids(cls, value: str) -> str:
        if not is_canonical_uuid_text(value):
            raise ValueError("media run identities must be canonical UUIDs")
        return value

    @field_validator("allowed_evidence_ids", "allowed_material_ids")
    @classmethod
    def unique_bounded_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(not item.strip() or len(item) > 500 for item in value):
            raise ValueError("allowed identities must be unique bounded strings")
        return value

    @model_validator(mode="after")
    def capability_has_exact_plan_identity(self) -> "ContentMediaRunCreate":
        if (self.capability == "generate") != (self.plan_entry_id is not None):
            raise ValueError("only generation runs require one image-plan entry")
        return self


class ContentMediaRunRead(_StrictModel):
    id: str = Field(min_length=36, max_length=36)
    job_id: str = Field(min_length=36, max_length=36)
    owner_product_id: str = Field(min_length=36, max_length=36)
    content_item_id: str = Field(min_length=36, max_length=36)
    revision_id: str = Field(min_length=36, max_length=36)
    plan_entry_id: str | None = Field(default=None, min_length=1, max_length=200)
    capability: Literal["generate", "analyze"]
    status: Literal["queued", "running", "needs_human", "succeeded", "failed", "cancelled"]
    state_version: int = Field(ge=0)
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=300)
    prompt_version: str = Field(min_length=1, max_length=100)
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    allowed_evidence_ids: list[str] = Field(max_length=500)
    allowed_material_ids: list[str] = Field(max_length=500)
    output_material_id: str | None = Field(default=None, min_length=36, max_length=36)
    analysis_artifact_id: int | None = Field(default=None, ge=1)
    usage: dict[str, int] = Field(max_length=50)
    duration_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    attempts: list[dict[str, object]] = Field(max_length=20)
    error_category: str | None = Field(default=None, min_length=1, max_length=100)
    error_detail: str | None = Field(default=None, min_length=1, max_length=2000)
    lease_token: str | None = Field(default=None, min_length=36, max_length=36)
    lease_expires_at: datetime | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    @field_validator(
        "id", "job_id", "owner_product_id", "content_item_id", "revision_id"
    )
    @classmethod
    def canonical_required_ids(cls, value: str) -> str:
        if not is_canonical_uuid_text(value):
            raise ValueError("media run identities must be canonical UUIDs")
        return value

    @field_validator("output_material_id", "lease_token")
    @classmethod
    def canonical_optional_ids(cls, value: str | None) -> str | None:
        if value is not None and not is_canonical_uuid_text(value):
            raise ValueError("optional media run identity must be a canonical UUID")
        return value

    @field_validator("allowed_evidence_ids", "allowed_material_ids")
    @classmethod
    def unique_read_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(
            not item.strip() or len(item) > 500 for item in value
        ):
            raise ValueError("allowed identities must be unique bounded strings")
        return value

    @field_validator("usage")
    @classmethod
    def bounded_read_usage(cls, value: dict[str, int]) -> dict[str, int]:
        if any(
            not key or len(key) > 100 or amount < 0 or amount > 1_000_000_000
            for key, amount in value.items()
        ):
            raise ValueError("usage must contain bounded non-negative counters")
        return value

    @field_validator("attempts")
    @classmethod
    def sanitized_attempts(cls, value: list[dict[str, object]]) -> list[dict[str, object]]:
        for attempt in value:
            if set(attempt) != {"attempt", "category"}:
                raise ValueError("attempt facts must contain only safe fields")
            attempt_no = attempt.get("attempt")
            category = attempt.get("category")
            if (
                isinstance(attempt_no, bool) or not isinstance(attempt_no, int)
                or not 1 <= attempt_no <= 20 or not isinstance(category, str)
                or not category or len(category) > 100
                or not category.replace("_", "a").isalnum()
                or category.lower() != category
            ):
                raise ValueError("attempt facts are invalid")
        return value


class MediaAttemptFact(_StrictModel):
    attempt: int = Field(ge=1, le=20)
    category: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_]+$")


class MediaCompletionFacts(_StrictModel):
    usage: dict[str, int] = Field(max_length=50)
    duration_ms: int = Field(ge=0, le=86_400_000)
    attempts: list[MediaAttemptFact] = Field(max_length=20)

    @field_validator("usage")
    @classmethod
    def bounded_usage(cls, value: dict[str, int]) -> dict[str, int]:
        if any(
            not key or len(key) > 100 or amount < 0 or amount > 1_000_000_000
            for key, amount in value.items()
        ):
            raise ValueError("usage must contain bounded non-negative counters")
        return value


class VisualAssessmentRead(_StrictModel):
    """Durable advisory output bound to one run and exact managed images."""

    run_id: str = Field(min_length=36, max_length=36)
    content_item_id: str = Field(min_length=36, max_length=36)
    revision_id: str = Field(min_length=36, max_length=36)
    material_ids: list[str] = Field(min_length=1, max_length=20)
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=300)
    prompt_version: str = Field(min_length=1, max_length=100)
    provider_request_id: str | None = Field(default=None, max_length=500)
    assessment: VisualAssessment
    usage: dict[str, int] = Field(max_length=50)
    duration_ms: int = Field(ge=0, le=86_400_000)

    @field_validator("run_id", "content_item_id", "revision_id")
    @classmethod
    def canonical_assessment_ids(cls, value: str) -> str:
        if not is_canonical_uuid_text(value):
            raise ValueError("assessment identities must be canonical UUIDs")
        return value

    @field_validator("material_ids")
    @classmethod
    def canonical_material_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(
            not is_canonical_uuid_text(item) for item in value
        ):
            raise ValueError("assessment material identities must be canonical and unique")
        return value

    @field_validator("usage")
    @classmethod
    def bounded_assessment_usage(cls, value: dict[str, int]) -> dict[str, int]:
        return MediaCompletionFacts(
            usage=value, duration_ms=0, attempts=[]
        ).usage
