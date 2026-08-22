"""Durable Phase-D records for finished-product content research."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.db import Base


def _uuid() -> str:
    return str(uuid4())


class FinishedProductDossierRecord(Base):
    __tablename__ = "content_product_dossiers"
    __table_args__ = (
        UniqueConstraint("product_key", "version", name="uq_content_product_dossier_version"),
        CheckConstraint("uat_status = 'passed'", name="ck_content_product_dossier_uat_passed"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    product_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    target_user: Mapped[str] = mapped_column(Text, nullable=False)
    core_need: Mapped[str] = mapped_column(Text, nullable=False)
    deliverables_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    usage_instructions: Mapped[str] = mapped_column(Text, nullable=False)
    faq_json: Mapped[list[dict[str, str]]] = mapped_column(JSON, nullable=False)
    allowed_claims_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    forbidden_claims_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    source_index_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    uat_status: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    keywords: Mapped[list["KeywordPlanRecord"]] = relationship(
        back_populates="dossier",
        cascade="all, delete-orphan",
        order_by="KeywordPlanRecord.position",
    )


class KeywordPlanRecord(Base):
    __tablename__ = "content_keyword_plan"
    __table_args__ = (
        UniqueConstraint("dossier_id", "keyword", name="uq_content_keyword_per_dossier"),
        UniqueConstraint("dossier_id", "position", name="uq_content_keyword_position"),
        CheckConstraint("position >= 1 AND position <= 20", name="ck_content_keyword_position"),
        CheckConstraint("target_count >= 1 AND target_count <= 50", name="ck_content_keyword_target_count"),
        CheckConstraint(
            "category IN ('main','positioning','visual','audience','pain','scenario','selling_point','question','comparison')",
            name="ck_content_keyword_category",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    dossier_id: Mapped[str] = mapped_column(
        ForeignKey("content_product_dossiers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    keyword: Mapped[str] = mapped_column(String(500), nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    expand: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    scope: Mapped[str] = mapped_column(String(100), nullable=False)
    target_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    dossier: Mapped[FinishedProductDossierRecord] = relationship(back_populates="keywords")
