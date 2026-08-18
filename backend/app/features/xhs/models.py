"""SQLite records for evidence-bound Xiaohongshu account collection facts."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.db import Base


_RAW_DIGEST_CHECK = (
    "json_valid(raw_evidence) = 1 AND json_type(raw_evidence) = 'object' "
    "AND raw_evidence_digest(raw_evidence) IS NOT NULL "
    "AND length(raw_digest) = 64 AND raw_digest NOT GLOB '*[^0-9a-f]*' "
    "AND raw_evidence_digest(raw_evidence) = raw_digest"
)
_SOURCE_URL_CHECK = "length(source_url) <= 2000 AND source_url GLOB 'https://*'"


class XhsAccountProfileRecord(Base):
    __tablename__ = "xhs_account_profiles"
    __table_args__ = (
        CheckConstraint("length(user_id) BETWEEN 1 AND 500", name="ck_xhs_profile_user_id"),
        CheckConstraint(_SOURCE_URL_CHECK, name="ck_xhs_profile_source_url"),
        CheckConstraint(_RAW_DIGEST_CHECK, name="ck_xhs_profile_raw_digest"),
        Index("ix_xhs_account_profiles_collection_job_id", "collection_job_id"),
        Index("ix_xhs_account_profiles_collection_artifact_id", "collection_artifact_id"),
    )

    user_id: Mapped[str] = mapped_column(String(500), primary_key=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    nickname: Mapped[str | None] = mapped_column(Text, nullable=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    public_stats_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    raw_evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    raw_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    collection_job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=False
    )
    collection_artifact_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("job_artifacts.id", ondelete="RESTRICT"), nullable=False
    )
    collected_at: Mapped[datetime] = mapped_column(nullable=False)

    notes: Mapped[list["XhsAccountNoteRecord"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan", passive_deletes=True
    )


class XhsAccountNoteRecord(Base):
    __tablename__ = "xhs_account_notes"
    __table_args__ = (
        UniqueConstraint("note_id", "user_id", name="uq_xhs_note_account_identity"),
        CheckConstraint("length(note_id) BETWEEN 1 AND 500", name="ck_xhs_note_id"),
        CheckConstraint(_SOURCE_URL_CHECK, name="ck_xhs_note_source_url"),
        CheckConstraint(_RAW_DIGEST_CHECK, name="ck_xhs_note_raw_digest"),
        Index("ix_xhs_account_notes_user_id", "user_id"),
        Index("ix_xhs_account_notes_collection_job_id", "collection_job_id"),
        Index("ix_xhs_account_notes_collection_artifact_id", "collection_artifact_id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    note_id: Mapped[str] = mapped_column(String(500), nullable=False)
    user_id: Mapped[str] = mapped_column(
        String(500),
        ForeignKey("xhs_account_profiles.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[str | None] = mapped_column(String(100), nullable=True)
    public_interactions_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    raw_evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    raw_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    collection_job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=False
    )
    collection_artifact_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("job_artifacts.id", ondelete="RESTRICT"), nullable=False
    )
    collected_at: Mapped[datetime] = mapped_column(nullable=False)

    profile: Mapped[XhsAccountProfileRecord] = relationship(back_populates="notes")
