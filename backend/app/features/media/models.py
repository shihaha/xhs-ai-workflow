"""SQLAlchemy record for durable content image generation and analysis runs."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db import Base


class ContentMediaRunRecord(Base):
    __tablename__ = "content_media_runs"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_content_media_job"),
        ForeignKeyConstraint(
            ["revision_id", "content_item_id"],
            ["content_revisions.id", "content_revisions.content_item_id"],
            name="fk_content_media_revision_item",
            ondelete="RESTRICT",
        ),
        CheckConstraint("is_canonical_uuid(id) = 1", name="ck_content_media_id"),
        CheckConstraint(
            "capability IN ('generate','analyze')", name="ck_content_media_capability"
        ),
        CheckConstraint(
            "status IN ('queued','running','needs_human','succeeded','failed','cancelled')",
            name="ck_content_media_status",
        ),
        CheckConstraint("state_version >= 0", name="ck_content_media_state_version"),
        CheckConstraint(
            "length(provider) BETWEEN 1 AND 100 AND length(model) BETWEEN 1 AND 300 "
            "AND length(prompt_version) BETWEEN 1 AND 100",
            name="ck_content_media_provider_model",
        ),
        CheckConstraint(
            "length(input_digest) = 64 AND input_digest NOT GLOB '*[^0-9a-f]*'",
            name="ck_content_media_input_digest",
        ),
        CheckConstraint(
            "json_valid(allowed_evidence_ids_json) = 1 AND "
            "json_type(allowed_evidence_ids_json) = 'array' AND "
            "json_array_length(allowed_evidence_ids_json) <= 500 AND "
            "json_valid(allowed_material_ids_json) = 1 AND "
            "json_type(allowed_material_ids_json) = 'array' AND "
            "json_array_length(allowed_material_ids_json) <= 500 AND "
            "json_valid(usage_json) = 1 AND json_type(usage_json) = 'object' AND "
            "json_valid(attempts_json) = 1 AND json_type(attempts_json) = 'array' AND "
            "json_array_length(attempts_json) <= 20",
            name="ck_content_media_json",
        ),
        CheckConstraint(
            "(capability = 'generate' AND plan_entry_id IS NOT NULL "
            "AND length(plan_entry_id) BETWEEN 1 AND 200) OR "
            "(capability = 'analyze' AND plan_entry_id IS NULL)",
            name="ck_content_media_plan_entry",
        ),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms BETWEEN 0 AND 86400000",
            name="ck_content_media_duration",
        ),
        CheckConstraint(
            "(status = 'running' AND lease_token IS NOT NULL "
            "AND is_canonical_uuid(lease_token) = 1 AND lease_expires_at IS NOT NULL) OR "
            "(status != 'running' AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_content_media_lease",
        ),
        CheckConstraint(
            "(status IN ('queued','running') AND completed_at IS NULL "
            "AND output_material_id IS NULL AND analysis_artifact_id IS NULL "
            "AND error_category IS NULL AND error_detail IS NULL) OR "
            "(status = 'succeeded' AND completed_at IS NOT NULL "
            "AND error_category IS NULL AND error_detail IS NULL AND "
            "((capability = 'generate' AND output_material_id IS NOT NULL "
            "AND analysis_artifact_id IS NULL) OR "
            "(capability = 'analyze' AND output_material_id IS NULL "
            "AND analysis_artifact_id IS NOT NULL))) OR "
            "(status IN ('failed','needs_human') AND completed_at IS NOT NULL "
            "AND output_material_id IS NULL AND analysis_artifact_id IS NULL "
            "AND error_category IS NOT NULL AND length(error_category) BETWEEN 1 AND 100 "
            "AND error_detail IS NOT NULL AND length(error_detail) BETWEEN 1 AND 2000) OR "
            "(status = 'cancelled' AND completed_at IS NOT NULL "
            "AND output_material_id IS NULL AND analysis_artifact_id IS NULL "
            "AND error_category IS NULL AND error_detail IS NULL)",
            name="ck_content_media_outcome",
        ),
        Index(
            "uq_content_media_open_generation",
            "content_item_id", "revision_id", "plan_entry_id",
            unique=True,
            sqlite_where=text(
                "capability = 'generate' AND status IN ('queued','running')"
            ),
        ),
        Index("ix_content_media_item_created", "content_item_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=False
    )
    owner_product_id: Mapped[str] = mapped_column(
        ForeignKey("content_products.id", ondelete="RESTRICT"), nullable=False
    )
    content_item_id: Mapped[str] = mapped_column(
        ForeignKey("content_items.id", ondelete="RESTRICT"), nullable=False
    )
    revision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    plan_entry_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    capability: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(300), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    allowed_evidence_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    allowed_material_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    output_material_id: Mapped[str | None] = mapped_column(
        ForeignKey("content_product_materials.id", ondelete="RESTRICT"), nullable=True
    )
    analysis_artifact_id: Mapped[int | None] = mapped_column(
        ForeignKey("job_artifacts.id", ondelete="RESTRICT"), nullable=True
    )
    usage_json: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False, default=dict)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts_json: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    error_category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
