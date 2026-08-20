"""Durable model attempts, validated analyses and opportunity cards."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import CheckConstraint, ForeignKey, JSON, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.db import Base


class AnalysisRecord(Base):
    __tablename__ = "analyses"
    __table_args__ = (
        CheckConstraint(
            "evidence_snapshot_json IS NULL OR "
            "(json_valid(evidence_snapshot_json) = 1 AND "
            "json_type(evidence_snapshot_json) = 'object')",
            name="ck_analysis_evidence_snapshot_json",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    analysis_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    account_user_id: Mapped[str | None] = mapped_column(String(500), nullable=True, index=True)
    account_user_ids_json: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(300), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    evidence_snapshot_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
    )
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    usage_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    error_category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    opportunities: Mapped[list["OpportunityRecord"]] = relationship(
        back_populates="analysis", cascade="all, delete-orphan"
    )


class OpportunityRecord(Base):
    __tablename__ = "opportunities"
    __table_args__ = (
        CheckConstraint(
            "review_status IN ('pending_review','approved','rejected')",
            name="ck_opportunity_review_status",
        ),
        CheckConstraint(
            "evidence_level IN ('warming_candidate','validated_candidate','legacy_ungraded')",
            name="ck_opportunity_evidence_level",
        ),
        CheckConstraint(
            "(review_status='pending_review' AND reviewed_at IS NULL AND rejection_reason IS NULL) "
            "OR (review_status='approved' AND reviewed_at IS NOT NULL AND rejection_reason IS NULL) "
            "OR (review_status='rejected' AND reviewed_at IS NOT NULL AND rejection_reason IS NOT NULL)",
            name="ck_opportunity_review_facts",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    analysis_id: Mapped[str] = mapped_column(
        ForeignKey("analyses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    review_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending_review", server_default="pending_review"
    )
    evidence_level: Mapped[str] = mapped_column(
        String(32), nullable=False, default="legacy_ungraded", server_default="legacy_ungraded"
    )
    supporting_accounts_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    supporting_products_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    supporting_notes_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_action: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    analysis: Mapped[AnalysisRecord] = relationship(back_populates="opportunities")
