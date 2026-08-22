"""Durable Phase-D records for finished-product content research."""

from __future__ import annotations

from datetime import datetime
from typing import Any
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

    keyword_runs: Mapped[list["KeywordPlanRunRecord"]] = relationship(
        back_populates="dossier",
        cascade="all, delete-orphan",
        order_by="KeywordPlanRunRecord.created_at",
    )
    benchmark_searches: Mapped[list["BenchmarkSearchRecord"]] = relationship(
        back_populates="dossier",
        cascade="all, delete-orphan",
        order_by="BenchmarkSearchRecord.created_at",
    )


class KeywordPlanRunRecord(Base):
    __tablename__ = "content_keyword_plan_runs"
    __table_args__ = (
        CheckConstraint("source IN ('manual','ai')", name="ck_content_keyword_run_source"),
        CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="ck_content_keyword_run_duration"),
        CheckConstraint(
            "(source = 'manual' AND provider IS NULL AND model IS NULL AND prompt_version IS NULL) "
            "OR (source = 'ai' AND provider IS NOT NULL AND model IS NOT NULL AND prompt_version IS NOT NULL)",
            name="ck_content_keyword_run_provenance",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    dossier_id: Mapped[str] = mapped_column(
        ForeignKey("content_product_dossiers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model: Mapped[str | None] = mapped_column(String(300), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    usage_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    dossier: Mapped[FinishedProductDossierRecord] = relationship(back_populates="keyword_runs")
    items: Mapped[list["KeywordPlanRecord"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="KeywordPlanRecord.position",
    )


class KeywordPlanRecord(Base):
    __tablename__ = "content_keyword_plan_items"
    __table_args__ = (
        UniqueConstraint("run_id", "keyword", name="uq_content_keyword_per_run"),
        UniqueConstraint("run_id", "position", name="uq_content_keyword_position"),
        CheckConstraint("position >= 1 AND position <= 20", name="ck_content_keyword_position"),
        CheckConstraint("target_count >= 5 AND target_count <= 10", name="ck_content_keyword_target_count"),
        CheckConstraint(
            "category IN ('main','positioning','visual','audience','pain','scenario','selling_point','question','comparison')",
            name="ck_content_keyword_category",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("content_keyword_plan_runs.id", ondelete="CASCADE"),
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

    run: Mapped[KeywordPlanRunRecord] = relationship(back_populates="items")


class BenchmarkSearchRecord(Base):
    """Bind one tutorial benchmark-search attempt to one real XHS search job."""

    __tablename__ = "content_benchmark_searches"
    __table_args__ = (
        UniqueConstraint("xhs_job_id", name="uq_content_benchmark_xhs_job"),
        UniqueConstraint(
            "keyword_item_id", "stage", "attempt",
            name="uq_content_benchmark_keyword_stage_attempt",
        ),
        CheckConstraint("stage IN ('probe','full')", name="ck_content_benchmark_stage"),
        CheckConstraint("attempt >= 1", name="ck_content_benchmark_attempt"),
        CheckConstraint("expected_count >= 1 AND expected_count <= 50", name="ck_content_benchmark_expected_count"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    dossier_id: Mapped[str] = mapped_column(
        ForeignKey("content_product_dossiers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    keyword_run_id: Mapped[str] = mapped_column(
        ForeignKey("content_keyword_plan_runs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    keyword_item_id: Mapped[str] = mapped_column(
        ForeignKey("content_keyword_plan_items.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    stage: Mapped[str] = mapped_column(String(20), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    keyword: Mapped[str] = mapped_column(String(500), nullable=False)
    expected_count: Mapped[int] = mapped_column(Integer, nullable=False)
    xhs_job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    dossier: Mapped[FinishedProductDossierRecord] = relationship(back_populates="benchmark_searches")
