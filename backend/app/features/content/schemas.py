"""Strict HTTP and service schemas for content production."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.app.db import canonical_artifact_path_key, is_canonical_uuid_text


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


def windows_name_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


class ProductCreate(StrictModel):
    name: str = Field(min_length=1, max_length=300)
    target_user: str = Field(min_length=1, max_length=4000)
    opportunity_id: str = Field(min_length=1, max_length=36)
    _name = field_validator("name", "target_user")(_strip_nonblank)


class MaterialCreate(StrictModel):
    logical_name: str = Field(min_length=1, max_length=200, pattern=r"^[^/\\\x00]+$")
    path: str = Field(min_length=1, max_length=1000)
    media_type: str = Field(min_length=1, max_length=200)
    kind: Literal["source", "output_image"] = "source"
    _media_type = field_validator("media_type")(_strip_nonblank)

    @field_validator("logical_name")
    @classmethod
    def safe_logical_name(cls, value: str) -> str:
        value = unicodedata.normalize("NFC", value)
        stem = value.split(".", 1)[0].casefold()
        reserved = {
            "con", "prn", "aux", "nul", "conin$", "conout$", "clock$",
            *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10)),
            "com¹", "com²", "com³", "lpt¹", "lpt²", "lpt³",
        }
        if (
            value in {".", ".."} or value.strip() != value or value.endswith((".", " "))
            or stem in reserved or any(
                ord(char) < 32 or 127 <= ord(char) <= 159 or char in '<>:"/\\|?*'
                for char in value
            )
        ):
            raise ValueError("logical_name must be a safe single filename")
        return value


class MaterialRead(StrictModel):
    id: str
    product_id: str
    logical_name: str
    version: int
    path: str
    sha256: str
    size_bytes: int
    media_type: str
    kind: Literal["source", "output_image"]
    availability: Literal["available", "missing", "corrupt"]
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
    image_material_ids: list[str] = Field(min_length=1, max_length=20)
    cover_material_id: str = Field(min_length=1, max_length=36)
    research_facts: list[ResearchFact] = Field(min_length=1, max_length=500)

    _evidence = field_validator("evidence_ids")(canonical_evidence_ids)

    @field_validator("material_ids")
    @classmethod
    def unique_materials(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("material ids must be unique")
        return value

    @field_validator("image_material_ids")
    @classmethod
    def unique_images(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("image material ids must be unique")
        return value

    @model_validator(mode="after")
    def cover_is_first_image(self) -> "ContentItemCreate":
        if self.image_material_ids[0] != self.cover_material_id:
            raise ValueError("cover_material_id must be the first ordered image")
        if set(self.image_material_ids) & set(self.material_ids):
            raise ValueError("source and output image material ids must be disjoint")
        return self


class CitedContentClaim(StrictModel):
    claim: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[str] = Field(min_length=1, max_length=100)
    _evidence = field_validator("evidence_ids")(canonical_evidence_ids)
    _claim = field_validator("claim")(_strip_nonblank)


class ImagePlanPage(StrictModel):
    page_number: int = Field(ge=1, le=20)
    material_id: str = Field(min_length=1, max_length=36)
    role: Literal["cover", "page"]
    headline: str = Field(min_length=1, max_length=300)
    visual_direction: str = Field(min_length=1, max_length=1000)
    _text = field_validator("headline", "visual_direction")(_strip_nonblank)


class ContentDraftOutput(StrictModel):
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=50000)
    claims: list[CitedContentClaim] = Field(min_length=1, max_length=500)
    source_evidence_ids: list[str] = Field(min_length=1, max_length=500)
    image_plan: list[ImagePlanPage] = Field(min_length=1, max_length=20)
    _sources = field_validator("source_evidence_ids")(canonical_evidence_ids)
    _text = field_validator("title", "body")(_strip_nonblank)


class RevisionRead(StrictModel):
    id: str
    number: int
    title: str
    body: str
    claims: list[CitedContentClaim]
    source_evidence_ids: list[str]
    image_plan: list[ImagePlanPage]
    model_provider: str
    model_name: str
    prompt_version: str
    usage: dict[str, int]
    attempts: list[dict[str, object]]
    created_at: datetime


class VisualCheck(StrictModel):
    material_id: str = Field(min_length=1, max_length=36)
    passed: bool
    observation: str = Field(min_length=1, max_length=2000)
    _observation = field_validator("observation")(_strip_nonblank)


class ReviewCreate(StrictModel):
    decision: Literal["approve", "reject"]
    expected_revision_id: str = Field(min_length=1, max_length=36)
    actor: str = Field(min_length=1, max_length=200)
    note: str = Field(min_length=1, max_length=4000)
    visual_checks: list[VisualCheck] = Field(default_factory=list, max_length=20)
    _text = field_validator("actor", "note")(_strip_nonblank)


class RegenerateCreate(StrictModel):
    expected_revision_id: str = Field(min_length=1, max_length=36)


class ExportCreate(StrictModel):
    expected_revision_id: str = Field(min_length=1, max_length=36)


class ReviewRead(StrictModel):
    id: int
    revision_id: str
    decision: Literal["approve", "reject", "regenerate"]
    actor: str
    note: str
    visual_checks: list[VisualCheck]
    created_at: datetime


class ContentItemRead(StrictModel):
    id: str
    product_id: str
    opportunity_id: str
    template_key: str
    status: Literal["research", "draft", "review", "rejected", "approved", "exported"]
    evidence_ids: list[str]
    material_ids: list[str]
    image_material_ids: list[str]
    cover_material_id: str
    research_facts: list[ResearchFact]
    current_revision: RevisionRead | None
    revisions: list[RevisionRead]
    reviews: list[ReviewRead]
    export_availability: Literal["available", "missing", "corrupt", "building", "failed"] | None = None
    created_at: datetime
    updated_at: datetime


class ContentPackageRead(StrictModel):
    id: str
    content_item_id: str
    revision_id: str
    status: Literal["building", "ready", "failed"]
    availability: Literal["available", "missing", "corrupt", "building", "failed"]
    path: str
    sha256: str
    size_bytes: int
    created_at: datetime


class ArtifactCleanupRead(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=36, max_length=36)
    owner_type: Literal["material", "content_package"]
    owner_id: str = Field(min_length=36, max_length=36)
    source_build_token: str | None
    relative_path: str = Field(min_length=1, max_length=1000)
    path_key: str = Field(min_length=1, max_length=1000)
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_size_bytes: int = Field(ge=0, le=250 * 1024 * 1024)
    state: Literal[
        "pending", "claimed", "quarantined", "deleted", "needs_human", "cancelled"
    ]
    reason: str = Field(min_length=1, max_length=64)
    not_before: datetime
    lease_token: str | None
    lease_expires_at: datetime | None
    quarantine_path: str | None = Field(max_length=1000)
    quarantine_volume_id: int | None = Field(ge=0)
    quarantine_file_id: int | None = Field(ge=0)
    quarantine_size_bytes: int | None = Field(ge=0, le=250 * 1024 * 1024)
    quarantine_mtime_ns: int | None = Field(ge=0)
    attempt_count: int = Field(ge=0)
    last_error_category: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    @field_validator("id", "owner_id")
    @classmethod
    def canonical_identity(cls, value: str) -> str:
        if not is_canonical_uuid_text(value):
            raise ValueError("cleanup identities must be canonical UUIDs")
        return value

    @field_validator("lease_token", "source_build_token")
    @classmethod
    def canonical_lease(cls, value: str | None) -> str | None:
        if value is not None and not is_canonical_uuid_text(value):
            raise ValueError("lease_token must be a canonical UUID")
        return value

    @model_validator(mode="after")
    def canonical_paths_and_lease_state(self) -> "ArtifactCleanupRead":
        if (self.owner_type == "material") != (self.source_build_token is None):
            raise ValueError("cleanup source token must identify only package generations")
        expected_key = canonical_artifact_path_key(self.relative_path)
        if expected_key is None or self.path_key != expected_key:
            raise ValueError("relative_path and path_key must be one canonical identity")
        if (
            self.quarantine_path is not None
            and canonical_artifact_path_key(self.quarantine_path) is None
        ):
            raise ValueError("quarantine_path must be a canonical managed path")
        identity = (
            self.quarantine_volume_id,
            self.quarantine_file_id,
            self.quarantine_size_bytes,
            self.quarantine_mtime_ns,
        )
        if (self.quarantine_path is None) != all(value is None for value in identity):
            raise ValueError("quarantine path and physical identity must be all-or-none")
        if self.quarantine_path is not None and any(value is None for value in identity):
            raise ValueError("quarantine physical identity must be complete")
        if self.state == "quarantined" and self.quarantine_path is None:
            raise ValueError("quarantined cleanup requires physical identity")
        if self.state == "claimed":
            lease_valid = self.lease_token is not None and self.lease_expires_at is not None
        else:
            lease_valid = self.lease_token is None and self.lease_expires_at is None
        if not lease_valid:
            raise ValueError("only claimed cleanup records may hold a complete lease")
        return self
