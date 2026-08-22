"""Strict Phase-D API schemas for finished products and content research."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _strip_nonblank(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("value must not be blank")
    return normalized


def _unique_nonblank(values: list[str]) -> list[str]:
    normalized = [_strip_nonblank(value) for value in values]
    if len(normalized) != len(set(normalized)):
        raise ValueError("values must be unique")
    return normalized


class FaqEntry(StrictModel):
    question: str = Field(min_length=1, max_length=1000)
    answer: str = Field(min_length=1, max_length=4000)
    _text = field_validator("question", "answer")(_strip_nonblank)


class FinishedProductDossierCreate(StrictModel):
    product_key: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._-]+$")
    name: str = Field(min_length=1, max_length=300)
    version: str = Field(min_length=1, max_length=100)
    target_user: str = Field(min_length=1, max_length=4000)
    core_need: str = Field(min_length=1, max_length=4000)
    deliverables: list[str] = Field(min_length=1, max_length=100)
    usage_instructions: str = Field(min_length=1, max_length=12000)
    faq: list[FaqEntry] = Field(default_factory=list, max_length=100)
    allowed_claims: list[str] = Field(min_length=1, max_length=200)
    forbidden_claims: list[str] = Field(default_factory=list, max_length=200)
    source_index: list[str] = Field(min_length=1, max_length=500)
    uat_status: Literal["passed"]

    _text = field_validator("name", "version", "target_user", "core_need", "usage_instructions")(_strip_nonblank)
    _deliverables = field_validator("deliverables")(_unique_nonblank)
    _allowed = field_validator("allowed_claims")(_unique_nonblank)
    _forbidden = field_validator("forbidden_claims")(_unique_nonblank)
    _sources = field_validator("source_index")(_unique_nonblank)

    @model_validator(mode="after")
    def claims_do_not_conflict(self) -> "FinishedProductDossierCreate":
        overlap = set(self.allowed_claims) & set(self.forbidden_claims)
        if overlap:
            raise ValueError("allowed and forbidden claims must not overlap")
        return self


class FinishedProductDossierRead(StrictModel):
    id: str
    product_key: str
    name: str
    version: str
    target_user: str
    core_need: str
    deliverables: list[str]
    usage_instructions: str
    faq: list[FaqEntry]
    allowed_claims: list[str]
    forbidden_claims: list[str]
    source_index: list[str]
    uat_status: Literal["passed"]
    created_at: datetime


KeywordCategory = Literal[
    "main",
    "positioning",
    "visual",
    "audience",
    "pain",
    "scenario",
    "selling_point",
    "question",
    "comparison",
]


class KeywordPlanItemCreate(StrictModel):
    keyword: str = Field(min_length=1, max_length=500)
    category: KeywordCategory
    expand: bool = False
    scope: str = Field(min_length=1, max_length=100)
    target_count: int = Field(ge=5, le=10)

    _text = field_validator("keyword", "scope")(_strip_nonblank)


class KeywordPlanReplace(StrictModel):
    items: list[KeywordPlanItemCreate] = Field(min_length=10, max_length=20)

    @field_validator("items")
    @classmethod
    def unique_keywords(cls, items: list[KeywordPlanItemCreate]) -> list[KeywordPlanItemCreate]:
        keys = [item.keyword.casefold() for item in items]
        if len(keys) != len(set(keys)):
            raise ValueError("keywords must be unique within a product")
        return items


class GeneratedKeywordPlanOutput(KeywordPlanReplace):
    """Exact model output contract for the tutorial keyword-layout step."""


class KeywordPlanItemRead(StrictModel):
    id: str
    run_id: str
    position: int
    keyword: str
    category: KeywordCategory
    expand: bool
    scope: str
    target_count: int
    created_at: datetime


class KeywordPlanRead(StrictModel):
    dossier_id: str
    run_id: str | None
    source: Literal["manual", "ai"] | None
    provider: str | None
    model: str | None
    prompt_version: str | None
    usage: dict[str, int]
    duration_ms: int | None
    created_at: datetime | None
    count: int
    items: list[KeywordPlanItemRead]
