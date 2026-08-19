"""SQLite records for evidence-bound Xiaohongshu account collection facts."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
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
        back_populates="profile", passive_deletes=True
    )


class XhsAccountNoteRecord(Base):
    __tablename__ = "xhs_account_notes"
    __table_args__ = (
        UniqueConstraint(
            "note_id",
            "user_id",
            "collection_job_id",
            name="uq_xhs_note_account_collection_identity",
        ),
        CheckConstraint(
            "id BETWEEN 1 AND 9223372036854775807",
            name="ck_xhs_note_canonical_id",
        ),
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
        ForeignKey("xhs_account_profiles.user_id", ondelete="RESTRICT"),
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


class XhsAccountProfileSnapshotRecord(Base):
    """One immutable, artifact-bound profile observation for one collection."""

    __tablename__ = "xhs_account_profile_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "collection_job_id",
            name="uq_xhs_profile_snapshot_collection_job",
        ),
        UniqueConstraint(
            "collection_artifact_id",
            name="uq_xhs_profile_snapshot_collection_artifact",
        ),
        CheckConstraint(
            "id BETWEEN 1 AND 9223372036854775807",
            name="ck_xhs_profile_snapshot_canonical_id",
        ),
        CheckConstraint(
            "length(user_id) BETWEEN 1 AND 500",
            name="ck_xhs_profile_snapshot_user_id",
        ),
        CheckConstraint(
            _SOURCE_URL_CHECK,
            name="ck_xhs_profile_snapshot_source_url",
        ),
        CheckConstraint(
            _RAW_DIGEST_CHECK,
            name="ck_xhs_profile_snapshot_raw_digest",
        ),
        Index(
            "ix_xhs_profile_snapshots_user_version",
            "user_id",
            "id",
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(500),
        ForeignKey("xhs_account_profiles.user_id", ondelete="RESTRICT"),
        nullable=False,
    )
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


class XhsAccountSnapshotNoteRecord(Base):
    """Immutable membership and artifact order for a canonical note row."""

    __tablename__ = "xhs_account_snapshot_notes"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "position",
            name="uq_xhs_snapshot_note_position",
        ),
        CheckConstraint(
            "note_record_id BETWEEN 1 AND 9223372036854775807",
            name="ck_xhs_snapshot_note_canonical_id",
        ),
        CheckConstraint(
            "position BETWEEN 0 AND 1000",
            name="ck_xhs_snapshot_note_position",
        ),
        Index("ix_xhs_snapshot_notes_snapshot_id", "snapshot_id"),
    )

    note_record_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("xhs_account_notes.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    snapshot_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("xhs_account_profile_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class XhsArtifactPromotionJournalRecord(Base):
    """Durable identity binding for one XHS artifact promotion attempt."""

    __tablename__ = "xhs_artifact_promotion_journal"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_xhs_artifact_journal_job"),
        UniqueConstraint("stage_path", name="uq_xhs_artifact_journal_stage"),
        UniqueConstraint("final_path", name="uq_xhs_artifact_journal_final"),
        CheckConstraint(
            "is_canonical_uuid(id) = 1 AND is_canonical_uuid(job_id) = 1",
            name="ck_xhs_artifact_journal_uuid",
        ),
        CheckConstraint(
            "length(artifact_kind) BETWEEN 1 AND 100 "
            "AND length(producer) BETWEEN 1 AND 64",
            name="ck_xhs_artifact_journal_source",
        ),
        CheckConstraint(
            "artifact_path_key(stage_path) IS NOT NULL "
            "AND length(stage_path) <= 1000 "
            "AND stage_path LIKE 'evidence/xhs/.staging/%.stage' "
            "AND stage_path NOT LIKE 'evidence/xhs/.staging/%/%' "
            "AND stage_path IN ("
            "'evidence/xhs/.staging/' || job_id || '-' || replace(id, '-', '') || '.stage', "
            "'evidence/xhs/.staging/' || job_id || '-' || replace(id, '-', '') || '-failure.stage')",
            name="ck_xhs_artifact_journal_stage_path",
        ),
        CheckConstraint(
            "artifact_path_key(final_path) IS NOT NULL "
            "AND length(final_path) <= 1000 "
            "AND final_path LIKE 'evidence/xhs/%.json' "
            "AND final_path NOT LIKE 'evidence/xhs/%/%' "
            "AND ((final_path = 'evidence/xhs/' || job_id || '.json' "
            "AND stage_path NOT LIKE '%-failure.stage') OR "
            "(final_path = 'evidence/xhs/' || job_id || '-failure.json' "
            "AND stage_path LIKE '%-failure.stage'))",
            name="ck_xhs_artifact_journal_final_path",
        ),
        CheckConstraint(
            "length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_xhs_artifact_journal_sha",
        ),
        CheckConstraint(
            "size_bytes BETWEEN 0 AND 20971520 AND ((file_dev IS NULL "
            "AND file_ino IS NULL AND file_mtime_ns IS NULL AND "
            "(state = 'allocating' OR (state = 'completed' AND resolution IN "
            "('rolled_back','inconsistent')))) OR (file_dev IS NOT NULL "
            "AND file_ino IS NOT NULL AND file_mtime_ns IS NOT NULL "
            "AND file_dev >= 0 AND file_ino >= 0 AND file_mtime_ns >= 0 "
            "AND state != 'allocating'))",
            name="ck_xhs_artifact_journal_identity",
        ),
        CheckConstraint(
            "target_state IN ('succeeded','failed','needs_human') "
            "AND state IN ('allocating','prepared','promoted','completed')",
            name="ck_xhs_artifact_journal_state",
        ),
        CheckConstraint(
            "((state != 'completed' AND resolution IS NULL "
            "AND artifact_id IS NULL AND completed_at IS NULL) OR "
            "(state = 'completed' AND resolution IN "
            "('committed','rolled_back','inconsistent') "
            "AND completed_at IS NOT NULL "
            "AND (resolution != 'committed' OR artifact_id IS NOT NULL) "
            "AND (resolution != 'rolled_back' OR artifact_id IS NULL)))",
            name="ck_xhs_artifact_journal_resolution",
        ),
        CheckConstraint(
            "datetime(created_at) IS NOT NULL AND datetime(updated_at) IS NOT NULL "
            "AND (completed_at IS NULL OR datetime(completed_at) IS NOT NULL)",
            name="ck_xhs_artifact_journal_timestamps",
        ),
        CheckConstraint(
            "((owner_token IS NULL AND recovery_lease_expires_at IS NULL) OR "
            "(is_canonical_uuid(owner_token) = 1 "
            "AND datetime(recovery_lease_expires_at) IS NOT NULL))",
            name="ck_xhs_artifact_journal_owner_lease",
        ),
        Index("ix_xhs_artifact_journal_state", "state"),
        Index("ix_xhs_artifact_journal_artifact_id", "artifact_id"),
        Index(
            "ix_xhs_artifact_journal_recovery_lease",
            "recovery_lease_expires_at",
            "state",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=False
    )
    artifact_kind: Mapped[str] = mapped_column(String(100), nullable=False)
    producer: Mapped[str] = mapped_column(String(64), nullable=False)
    stage_path: Mapped[str] = mapped_column(Text, nullable=False)
    final_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    file_dev: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_ino: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_mtime_ns: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_state: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    resolution: Mapped[str | None] = mapped_column(String(20), nullable=True)
    artifact_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("job_artifacts.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    owner_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    recovery_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
