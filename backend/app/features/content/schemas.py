"""Strict HTTP and service schemas for content production."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_EVIDENCE = re.compile(r"^(?:artifact|rank-item):([1-9][0-9]*)$", re.ASCII)
_MAX_SQLITE_ID = 9_223_372_036_854_775_807


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def canonical_evidence_ids(value: list[str]) -> list[str]:
    if not value or len(value) != len(set(value)):
        raise ValueError("evidence ids must be non-empty and unique")
    for evidence_id in value:
        match = _EVIDENCE.fullmatch(evidence_id)
        if match is None or int(match.group(1)) > _MAX_SQLITE_ID:
            raise ValueError("evidence ids must be canonical persisted identities")
    return value


def _strip_nonblank(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("value must not be blank")
    return normalized


class ProductCreate(StrictModel):
    name: str = Field(min_length=1, max_length=300)
    target_user: str = Field(min_length=1, max_length=4000)
    opportunity_id: str = Field(min_length=1, max_length=36)
    _name = field_validator("name", "target_user")(_strip_nonblank)


class MaterialCreate(StrictModel):
    logical_name: str = Field(min_length=1, max_length=200, pattern=r"^[^/\\\x00]+$")
    path: str = Field(min_length=1, max_length=1000)
    media_type: str = Field(min_length=1, max_length=200)
    _media_type = field_validator("media_type")(_strip_nonblank)

    @field_validator("logical_name")
    @classmethod
    def safe_logical_name(cls, value: str) -> str:
        if value in {".", ".."} or value.strip() != value:
            raise ValueError("logical_name must be a safe single filename")
        return value


class MaterialRead(StrictModel):
    id: str
    logical_name: str
    version: int
    path: str
    sha256: str
    size_bytes: int
    media_type: str
    created_at: datetime


class ProductRead(StrictModel):
    id: str
    name: str
    target_user: str
    opportunity_id: str
    materials: list[MaterialRead]
    created_at: datetime


class ResearchFact(StrictModel):
    fact: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[str] = Field(min_length=1, max_length=100)
    _evidence = field_validator("evidence_ids")(canonical_evidence_ids)
    _fact = field_validator("fact")(_strip_nonblank)


class ContentItemCreate(StrictModel):
    product_id: str = Field(min_length=1, max_length=36)
    opportunity_id: str = Field(min_length=1, max_length=36)
    template_key: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")
    evidence_ids: list[str] = Field(min_length=1, max_length=500)
    material_ids: list[str] = Field(default_factory=list, max_length=500)
    research_facts: list[ResearchFact] = Field(min_length=1, max_length=500)

    _evidence = field_validator("evidence_ids")(canonical_evidence_ids)

    @field_validator("material_ids")
    @classmethod
    def unique_materials(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("material ids must be unique")
        return value


class CitedContentClaim(StrictModel):
    claim: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[str] = Field(min_length=1, max_length=100)
    _evidence = field_validator("evidence_ids")(canonical_evidence_ids)
    _claim = field_validator("claim")(_strip_nonblank)


class ContentDraftOutput(StrictModel):
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=50000)
    claims: list[CitedContentClaim] = Field(min_length=1, max_length=500)
    source_evidence_ids: list[str] = Field(min_length=1, max_length=500)
    _sources = field_validator("source_evidence_ids")(canonical_evidence_ids)
    _text = field_validator("title", "body")(_strip_nonblank)


class RevisionRead(StrictModel):
    id: str
    number: int
    title: str
    body: str
    claims: list[CitedContentClaim]
    source_evidence_ids: list[str]
    model_provider: str
    model_name: str
    prompt_version: str
    usage: dict[str, int]
    attempts: list[dict[str, object]]
    created_at: datetime


class ReviewCreate(StrictModel):
    decision: Literal["approve", "reject"]
    actor: str = Field(min_length=1, max_length=200)
    note: str = Field(min_length=1, max_length=4000)
    _text = field_validator("actor", "note")(_strip_nonblank)


class ReviewRead(StrictModel):
    id: int
    revision_id: str
    decision: Literal["approve", "reject", "regenerate"]
    actor: str
    note: str
    created_at: datetime


class ContentItemRead(StrictModel):
    id: str
    product_id: str
    opportunity_id: str
    template_key: str
    status: Literal["research", "draft", "review", "rejected", "approved", "exported"]
    evidence_ids: list[str]
    material_ids: list[str]
    research_facts: list[ResearchFact]
    current_revision: RevisionRead | None
    revisions: list[RevisionRead]
    reviews: list[ReviewRead]
    created_at: datetime
    updated_at: datetime


class ContentPackageRead(StrictModel):
    id: str
    content_item_id: str
    revision_id: str
    status: Literal["ready"]
    path: str
    sha256: str
    size_bytes: int
    created_at: datetime
