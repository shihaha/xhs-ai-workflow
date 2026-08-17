"""Durable product materials, immutable content revisions and packages."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    CheckConstraint, ForeignKey, ForeignKeyConstraint, Index, JSON, Integer, String,
    Text, UniqueConstraint, text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.db import Base


def _uuid() -> str:
    return str(uuid4())


class ProductRecord(Base):
    __tablename__ = "content_products"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    opportunity_id: Mapped[str] = mapped_column(
        ForeignKey("opportunities.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    target_user: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    materials: Mapped[list["ProductMaterialRecord"]] = relationship(
        back_populates="product", cascade="all, delete-orphan", order_by="ProductMaterialRecord.id"
    )


class ProductMaterialRecord(Base):
    __tablename__ = "content_product_materials"
    __table_args__ = (
        UniqueConstraint("product_id", "logical_key", "version", name="uq_material_version"),
        CheckConstraint("version > 0", name="ck_material_version_positive"),
        CheckConstraint("size_bytes > 0", name="ck_material_size_positive"),
        CheckConstraint(
            "length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_material_sha_format",
        ),
        CheckConstraint("kind IN ('source','output_image')", name="ck_material_kind"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    product_id: Mapped[str] = mapped_column(
        ForeignKey("content_products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    logical_name: Mapped[str] = mapped_column(String(200), nullable=False)
    logical_key: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    media_type: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    product: Mapped[ProductRecord] = relationship(back_populates="materials")


class ContentItemRecord(Base):
    __tablename__ = "content_items"
    __table_args__ = (
        CheckConstraint(
            "status IN ('research','draft','review','rejected','approved','exported')",
            name="ck_content_item_status",
        ),
        ForeignKeyConstraint(
            ["current_revision_id", "id"],
            ["content_revisions.id", "content_revisions.content_item_id"],
            name="fk_current_revision_same_item",
            use_alter=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    product_id: Mapped[str] = mapped_column(
        ForeignKey("content_products.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    opportunity_id: Mapped[str] = mapped_column(
        ForeignKey("opportunities.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    template_key: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    evidence_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    material_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    image_material_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    cover_material_id: Mapped[str] = mapped_column(String(36), nullable=False)
    research_facts_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    current_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    revisions: Mapped[list["ContentRevisionRecord"]] = relationship(
        back_populates="item", cascade="all, delete-orphan",
        foreign_keys="ContentRevisionRecord.content_item_id",
        order_by="ContentRevisionRecord.number",
    )
    reviews: Mapped[list["ContentReviewRecord"]] = relationship(
        back_populates="item", cascade="all, delete-orphan", order_by="ContentReviewRecord.id"
    )


class ContentRevisionRecord(Base):
    __tablename__ = "content_revisions"
    __table_args__ = (
        UniqueConstraint("content_item_id", "number", name="uq_content_revision_number"),
        UniqueConstraint("id", "content_item_id", name="uq_revision_id_item"),
        CheckConstraint("number > 0", name="ck_revision_number_positive"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    content_item_id: Mapped[str] = mapped_column(
        ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    claims_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    source_evidence_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    image_plan_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    model_provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model_name: Mapped[str] = mapped_column(String(300), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    usage_json: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False, default=dict)
    attempts_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    item: Mapped[ContentItemRecord] = relationship(
        back_populates="revisions", foreign_keys=[content_item_id]
    )


class ContentReviewRecord(Base):
    __tablename__ = "content_reviews"
    __table_args__ = (
        ForeignKeyConstraint(
            ["revision_id", "content_item_id"],
            ["content_revisions.id", "content_revisions.content_item_id"],
            name="fk_review_revision_same_item",
        ),
        CheckConstraint(
            "decision IN ('approve','reject','regenerate')", name="ck_review_decision"
        ),
        Index(
            "uq_review_terminal_revision", "revision_id", unique=True,
            sqlite_where=text("decision IN ('approve','reject')"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_item_id: Mapped[str] = mapped_column(
        ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision_id: Mapped[str] = mapped_column(
        String(36), nullable=False
    )
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False)
    visual_checks_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    item: Mapped[ContentItemRecord] = relationship(back_populates="reviews")


class ContentPackageRecord(Base):
    __tablename__ = "content_packages"
    __table_args__ = (
        UniqueConstraint("revision_id", name="uq_content_package_revision"),
        ForeignKeyConstraint(
            ["revision_id", "content_item_id"],
            ["content_revisions.id", "content_revisions.content_item_id"],
            name="fk_package_revision_same_item",
        ),
        CheckConstraint("status IN ('building','ready','failed')", name="ck_package_status"),
        CheckConstraint("size_bytes >= 0", name="ck_package_size_nonnegative"),
        CheckConstraint(
            "length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_package_sha_format",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    content_item_id: Mapped[str] = mapped_column(
        ForeignKey("content_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    revision_id: Mapped[str] = mapped_column(
        String(36), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    build_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class ArtifactCleanupRecord(Base):
    __tablename__ = "artifact_gc_queue"
    __table_args__ = (
        CheckConstraint(
            "owner_type IN ('material','content_package')",
            name="ck_artifact_gc_owner_type",
        ),
        CheckConstraint(
            "state IN ('pending','claimed','quarantined','deleted','needs_human','cancelled')",
            name="ck_artifact_gc_state",
        ),
        CheckConstraint(
            "expected_size_bytes >= 0 AND expected_size_bytes <= 262144000",
            name="ck_artifact_gc_size",
        ),
        CheckConstraint(
            "length(expected_sha256) = 64 AND expected_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_artifact_gc_sha_format",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_artifact_gc_attempts"),
        CheckConstraint(
            "is_canonical_uuid(id) = 1 AND is_canonical_uuid(owner_id) = 1 "
            "AND (lease_token IS NULL OR is_canonical_uuid(lease_token) = 1)",
            name="ck_artifact_gc_uuid_identity",
        ),
        CheckConstraint(
            "((state = 'claimed' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (state != 'claimed' AND lease_token IS NULL AND lease_expires_at IS NULL))",
            name="ck_artifact_gc_lease_state",
        ),
        CheckConstraint(
            "datetime(not_before) IS NOT NULL AND datetime(created_at) IS NOT NULL "
            "AND datetime(updated_at) IS NOT NULL "
            "AND (lease_expires_at IS NULL OR datetime(lease_expires_at) IS NOT NULL) "
            "AND (completed_at IS NULL OR datetime(completed_at) IS NOT NULL)",
            name="ck_artifact_gc_timestamps",
        ),
        CheckConstraint(
            "artifact_path_key(relative_path) IS NOT NULL "
            "AND length(relative_path) <= 1000 "
            "AND path_key = artifact_path_key(relative_path)",
            name="ck_artifact_gc_relative_path",
        ),
        CheckConstraint(
            "quarantine_path IS NULL OR (length(quarantine_path) <= 1000 "
            "AND artifact_path_key(quarantine_path) IS NOT NULL)",
            name="ck_artifact_gc_quarantine_path",
        ),
        CheckConstraint(
            "((quarantine_path IS NULL AND quarantine_volume_id IS NULL "
            "AND quarantine_file_id IS NULL AND quarantine_size_bytes IS NULL "
            "AND quarantine_mtime_ns IS NULL) OR "
            "(quarantine_path IS NOT NULL AND quarantine_volume_id IS NOT NULL "
            "AND quarantine_file_id IS NOT NULL AND quarantine_size_bytes IS NOT NULL "
            "AND quarantine_mtime_ns IS NOT NULL AND quarantine_volume_id >= 0 "
            "AND quarantine_file_id >= 0 AND quarantine_size_bytes >= 0 "
            "AND quarantine_size_bytes <= 262144000 AND quarantine_mtime_ns >= 0)) "
            "AND (state != 'quarantined' OR quarantine_path IS NOT NULL)",
            name="ck_artifact_gc_quarantine_identity",
        ),
        Index(
            "uq_artifact_gc_open_owner",
            "owner_type",
            "owner_id",
            "path_key",
            unique=True,
            sqlite_where=text(
                "state IN ('pending','claimed','quarantined','needs_human')"
            ),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_type: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(36), nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    path_key: Mapped[str] = mapped_column(Text, nullable=False)
    expected_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    not_before: Mapped[datetime] = mapped_column(nullable=False)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    quarantine_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    quarantine_volume_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quarantine_file_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quarantine_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quarantine_mtime_ns: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
