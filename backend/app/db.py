"""SQLite database lifecycle for durable local workbench facts."""

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
import unicodedata
from uuid import UUID, uuid4

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from backend.app.features.xhs.constants import (
    ACCOUNT_COLLECTION_ARTIFACT_KIND,
    ACCOUNT_COLLECTION_ARTIFACT_PRODUCER,
    ACCOUNT_COLLECTION_JOB_TYPE,
)


XHS_ARTIFACT_PROMOTION_JOURNAL_MIGRATION = (
    "xhs_artifact_promotion_journal_v2"
)
XHS_ACCOUNT_FACT_CONTENT_BINDING_MIGRATION = (
    "xhs_account_fact_content_binding_v4"
)
XHS_ACCOUNT_SNAPSHOT_EVIDENCE_MIGRATION = (
    "xhs_account_snapshot_evidence_v5"
)
ANALYSIS_EVIDENCE_SNAPSHOT_MIGRATION = (
    "analysis_evidence_snapshot_v3"
)
CONTENT_MEDIA_RUNS_MIGRATION = "content_media_runs_v1"
_ANALYSIS_EVIDENCE_SNAPSHOT_CHECK = (
    "evidence_snapshot_json IS NULL OR "
    "(json_valid(evidence_snapshot_json) = 1 AND "
    "json_type(evidence_snapshot_json) = 'object')"
)
_ANALYSIS_EVIDENCE_SNAPSHOT_TRIGGERS = {
    "ck_analysis_status_insert": """
CREATE TRIGGER ck_analysis_status_insert
BEFORE INSERT ON analyses
WHEN NEW.status NOT IN ('succeeded', 'failed', 'needs_human')
     OR (
         NEW.status='succeeded'
         AND (
             NEW.output_json IS NULL
             OR json_valid(NEW.output_json) IS NOT 1
             OR json_type(NEW.output_json) IS NOT 'object'
         )
     )
     OR (
         NEW.status IN ('failed', 'needs_human')
         AND (
             (
                 NEW.output_json IS NOT NULL
                 AND json_type(NEW.output_json) IS NOT 'null'
             )
             OR NEW.evidence_snapshot_json IS NOT NULL
         )
     )
BEGIN
    SELECT RAISE(ABORT, 'analysis status and output are inconsistent');
END
""".strip(),
    "ck_analysis_status_transition": """
CREATE TRIGGER ck_analysis_status_transition
BEFORE UPDATE OF status ON analyses
WHEN NEW.status NOT IN ('succeeded', 'failed', 'needs_human')
     OR NOT (
         NEW.status=OLD.status
         OR (
             OLD.status='succeeded'
             AND NEW.status IN ('failed', 'needs_human')
         )
     )
BEGIN
    SELECT RAISE(ABORT, 'analysis status transition is invalid');
END
""".strip(),
    "ck_analysis_evidence_snapshot_insert": """
CREATE TRIGGER ck_analysis_evidence_snapshot_insert
BEFORE INSERT ON analyses
WHEN NEW.status='succeeded'
     AND analysis_evidence_snapshot_v1_valid(
         NEW.input_digest,
         NEW.account_user_id,
         NEW.account_user_ids_json,
         NEW.evidence_ids_json,
         NEW.evidence_snapshot_json
     ) IS NOT 1
BEGIN
    SELECT RAISE(ABORT, 'successful analysis requires exact evidence snapshot');
END
""".strip(),
    "ck_analysis_evidence_snapshot_immutable_update": """
CREATE TRIGGER ck_analysis_evidence_snapshot_immutable_update
BEFORE UPDATE OF
    analysis_type, account_user_id, account_user_ids_json, prompt_version,
    provider, model, input_digest, evidence_ids_json, evidence_snapshot_json,
    output_json, usage_json, duration_ms, attempts_json, error_category,
    error_detail, created_at
ON analyses
WHEN OLD.evidence_snapshot_json IS NOT NULL
     OR NEW.evidence_snapshot_json IS NOT OLD.evidence_snapshot_json
BEGIN
    SELECT RAISE(ABORT, 'sealed analysis and evidence snapshot are immutable');
END
""".strip(),
    "ck_analysis_evidence_snapshot_success_update": """
CREATE TRIGGER ck_analysis_evidence_snapshot_success_update
BEFORE UPDATE ON analyses
WHEN NEW.status='succeeded'
     AND analysis_evidence_snapshot_v1_valid(
         NEW.input_digest,
         NEW.account_user_id,
         NEW.account_user_ids_json,
         NEW.evidence_ids_json,
         NEW.evidence_snapshot_json
     ) IS NOT 1
BEGIN
    SELECT RAISE(ABORT, 'successful analysis requires exact evidence snapshot');
END
""".strip(),
    "ck_analysis_evidence_snapshot_non_success_insert": """
CREATE TRIGGER ck_analysis_evidence_snapshot_non_success_insert
BEFORE INSERT ON analyses
WHEN NEW.status<>'succeeded' AND NEW.evidence_snapshot_json IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'new non-success analysis cannot claim sealed evidence');
END
""".strip(),
    "ck_analysis_evidence_snapshot_immutable_delete": """
CREATE TRIGGER ck_analysis_evidence_snapshot_immutable_delete
BEFORE DELETE ON analyses
WHEN OLD.evidence_snapshot_json IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'analysis evidence snapshot is durable');
END
""".strip(),
    "ck_analysis_opportunity_success_insert": """
CREATE TRIGGER ck_analysis_opportunity_success_insert
BEFORE INSERT ON opportunities
WHEN NOT EXISTS (
    SELECT 1 FROM analyses
    WHERE id=NEW.analysis_id AND status='succeeded'
)
BEGIN
    SELECT RAISE(ABORT, 'opportunity requires a successful analysis');
END
""".strip(),
    "ck_analysis_opportunity_success_update": """
CREATE TRIGGER ck_analysis_opportunity_success_update
BEFORE UPDATE OF analysis_id ON opportunities
WHEN NOT EXISTS (
    SELECT 1 FROM analyses
    WHERE id=NEW.analysis_id AND status='succeeded'
)
BEGIN
    SELECT RAISE(ABORT, 'opportunity requires a successful analysis');
END
""".strip(),
}
_LEGACY_ANALYSIS_EVIDENCE_SNAPSHOT = {
    "schema_version": 0,
    "status": "legacy_unsealed",
}
_XHS_ACCOUNT_FACT_IMMUTABILITY_TRIGGERS = {
    "ck_xhs_profile_immutable_update": """
        CREATE TRIGGER ck_xhs_profile_immutable_update
        BEFORE UPDATE ON xhs_account_profiles
        FOR EACH ROW
        WHEN EXISTS (
            SELECT 1 FROM job_artifacts AS artifact
            WHERE artifact.id=OLD.collection_artifact_id
            AND artifact.job_id=OLD.collection_job_id
            AND json_valid(artifact.metadata_json) IS 1
            AND json_type(artifact.metadata_json) IS 'object'
            AND json_type(artifact.metadata_json, '$.artifact_id') IS 'integer'
            AND json_extract(artifact.metadata_json, '$.artifact_id') IS artifact.id
            AND json_type(artifact.metadata_json, '$.job_id') IS 'text'
            AND json_extract(artifact.metadata_json, '$.job_id') IS artifact.job_id
            AND json_type(artifact.metadata_json, '$.sha256') IS 'text'
            AND json_type(artifact.metadata_json, '$.size_bytes') IS 'integer'
        )
        BEGIN
            SELECT RAISE(ABORT, 'xhs account profile facts are immutable');
        END
    """,
    "ck_xhs_note_immutable_update": """
        CREATE TRIGGER ck_xhs_note_immutable_update
        BEFORE UPDATE ON xhs_account_notes
        FOR EACH ROW
        WHEN EXISTS (
            SELECT 1 FROM job_artifacts AS artifact
            WHERE artifact.id=OLD.collection_artifact_id
            AND artifact.job_id=OLD.collection_job_id
            AND json_valid(artifact.metadata_json) IS 1
            AND json_type(artifact.metadata_json) IS 'object'
            AND json_type(artifact.metadata_json, '$.artifact_id') IS 'integer'
            AND json_extract(artifact.metadata_json, '$.artifact_id') IS artifact.id
            AND json_type(artifact.metadata_json, '$.job_id') IS 'text'
            AND json_extract(artifact.metadata_json, '$.job_id') IS artifact.job_id
            AND json_type(artifact.metadata_json, '$.sha256') IS 'text'
            AND json_type(artifact.metadata_json, '$.size_bytes') IS 'integer'
        )
        BEGIN
            SELECT RAISE(ABORT, 'xhs account note facts are immutable');
        END
    """,
}
_XHS_ARTIFACT_PROMOTION_JOURNAL_LEGACY_MIGRATION = (
    "xhs_artifact_promotion_journal_v1"
)
_XHS_ARTIFACT_PROMOTION_LEFTOVERS = (
    "xhs_artifact_promotion_journal_v0",
    "xhs_artifact_promotion_journal_v1_rebuild",
)
_XHS_ARTIFACT_JOURNAL_V1_BINDING_WHEN = """
NOT EXISTS (
    SELECT 1 FROM jobs AS job
    WHERE job.id=NEW.job_id
    AND NEW.producer='xhs_cli_read_worker_v1'
    AND NEW.artifact_kind=CASE job.type
        WHEN 'xhs_account_collection' THEN 'xhs_account_collection_raw'
        WHEN 'xhs_note_search' THEN 'xhs_note_search_raw'
        ELSE '' END
)
OR (
    NEW.artifact_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM job_artifacts AS artifact
        WHERE artifact.id=NEW.artifact_id
        AND artifact.job_id=NEW.job_id
        AND artifact.kind=NEW.artifact_kind
        AND artifact.producer=NEW.producer
        AND artifact.path=NEW.final_path
        AND json_extract(artifact.metadata_json, '$.sha256')=NEW.sha256
        AND json_extract(artifact.metadata_json, '$.size_bytes')=NEW.size_bytes
    )
)
OR (
    NEW.state='completed' AND NEW.resolution='committed'
    AND NOT EXISTS (
        SELECT 1 FROM jobs AS committed_job
        WHERE committed_job.id=NEW.job_id
        AND committed_job.state=NEW.target_state
    )
)
""".strip()
_XHS_ARTIFACT_JOURNAL_V1_TRIGGERS = {
    "ck_xhs_artifact_journal_binding_insert": f"""
CREATE TRIGGER ck_xhs_artifact_journal_binding_insert
BEFORE INSERT ON xhs_artifact_promotion_journal
WHEN {_XHS_ARTIFACT_JOURNAL_V1_BINDING_WHEN}
BEGIN
    SELECT RAISE(ABORT, 'xhs artifact journal binding mismatch');
END
""".strip(),
    "ck_xhs_artifact_journal_binding_update": f"""
CREATE TRIGGER ck_xhs_artifact_journal_binding_update
BEFORE UPDATE ON xhs_artifact_promotion_journal
WHEN {_XHS_ARTIFACT_JOURNAL_V1_BINDING_WHEN}
BEGIN
    SELECT RAISE(ABORT, 'xhs artifact journal binding mismatch');
END
""".strip(),
    "ck_xhs_artifact_journal_no_delete": """
CREATE TRIGGER ck_xhs_artifact_journal_no_delete
BEFORE DELETE ON xhs_artifact_promotion_journal
BEGIN
    SELECT RAISE(ABORT, 'xhs artifact journal is durable');
END
""".strip(),
}
_XHS_ARTIFACT_METADATA_BINDING = """
json_valid(artifact.metadata_json) IS 1
AND json_type(artifact.metadata_json) IS 'object'
AND json_type(artifact.metadata_json, '$.artifact_id') IS 'integer'
AND json_extract(artifact.metadata_json, '$.artifact_id') IS artifact.id
AND json_type(artifact.metadata_json, '$.job_id') IS 'text'
AND json_extract(artifact.metadata_json, '$.job_id') IS journal.job_id
AND json_type(artifact.metadata_json, '$.sha256') IS 'text'
AND json_extract(artifact.metadata_json, '$.sha256') IS journal.sha256
AND json_type(artifact.metadata_json, '$.size_bytes') IS 'integer'
AND json_extract(artifact.metadata_json, '$.size_bytes') IS journal.size_bytes
AND (
    (
        job.type='xhs_account_collection'
        AND json_valid(job.input_data) IS 1
        AND json_type(job.input_data) IS 'object'
        AND json_type(job.input_data, '$.user_id') IS 'text'
        AND json_type(artifact.metadata_json, '$.user_id') IS 'text'
        AND json_extract(artifact.metadata_json, '$.user_id') IS
            json_extract(job.input_data, '$.user_id')
    )
    OR (
        job.type='xhs_note_search'
        AND json_valid(job.input_data) IS 1
        AND json_type(job.input_data) IS 'object'
        AND json_type(job.input_data, '$.keyword') IS 'text'
        AND json_type(artifact.metadata_json, '$.keyword') IS 'text'
        AND json_extract(artifact.metadata_json, '$.keyword') IS
            json_extract(job.input_data, '$.keyword')
    )
)
""".strip()
_XHS_ARTIFACT_JOURNAL_BINDING_WHEN = """
NOT EXISTS (
    SELECT 1 FROM jobs AS job
    WHERE job.id=NEW.job_id
    AND NEW.producer='xhs_cli_read_worker_v1'
    AND NEW.artifact_kind=CASE job.type
        WHEN 'xhs_account_collection' THEN 'xhs_account_collection_raw'
        WHEN 'xhs_note_search' THEN 'xhs_note_search_raw'
        ELSE '' END
)
OR (
    NEW.artifact_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM job_artifacts AS artifact
        WHERE artifact.id=NEW.artifact_id
        AND artifact.job_id=NEW.job_id
        AND artifact.kind=NEW.artifact_kind
        AND artifact.producer=NEW.producer
        AND artifact.path=NEW.final_path
        AND json_valid(artifact.metadata_json) IS 1
        AND json_type(artifact.metadata_json) IS 'object'
        AND json_type(artifact.metadata_json, '$.artifact_id') IS 'integer'
        AND json_extract(artifact.metadata_json, '$.artifact_id') IS artifact.id
        AND json_type(artifact.metadata_json, '$.job_id') IS 'text'
        AND json_extract(artifact.metadata_json, '$.job_id') IS NEW.job_id
        AND json_type(artifact.metadata_json, '$.sha256') IS 'text'
        AND json_extract(artifact.metadata_json, '$.sha256') IS NEW.sha256
        AND json_type(artifact.metadata_json, '$.size_bytes') IS 'integer'
        AND json_extract(artifact.metadata_json, '$.size_bytes') IS NEW.size_bytes
        AND EXISTS (
            SELECT 1 FROM jobs AS bound_job
            WHERE bound_job.id=NEW.job_id
            AND (
                (bound_job.type='xhs_account_collection'
                 AND json_valid(bound_job.input_data) IS 1
                 AND json_type(bound_job.input_data) IS 'object'
                 AND json_type(bound_job.input_data, '$.user_id') IS 'text'
                 AND json_type(artifact.metadata_json, '$.user_id') IS 'text'
                 AND json_extract(artifact.metadata_json, '$.user_id') IS
                     json_extract(bound_job.input_data, '$.user_id'))
                OR
                (bound_job.type='xhs_note_search'
                 AND json_valid(bound_job.input_data) IS 1
                 AND json_type(bound_job.input_data) IS 'object'
                 AND json_type(bound_job.input_data, '$.keyword') IS 'text'
                 AND json_type(artifact.metadata_json, '$.keyword') IS 'text'
                 AND json_extract(artifact.metadata_json, '$.keyword') IS
                     json_extract(bound_job.input_data, '$.keyword'))
            )
        )
    )
)
OR (
    NEW.state='completed' AND NEW.resolution='committed'
    AND NOT EXISTS (
        SELECT 1 FROM jobs AS committed_job
        WHERE committed_job.id=NEW.job_id
        AND committed_job.state=NEW.target_state
    )
)
""".strip()
_XHS_ARTIFACT_JOURNAL_TRIGGERS = {
    "ck_xhs_artifact_journal_binding_insert": f"""
CREATE TRIGGER ck_xhs_artifact_journal_binding_insert
BEFORE INSERT ON xhs_artifact_promotion_journal
WHEN {_XHS_ARTIFACT_JOURNAL_BINDING_WHEN}
BEGIN
    SELECT RAISE(ABORT, 'xhs artifact journal binding mismatch');
END
""".strip(),
    "ck_xhs_artifact_journal_binding_update": f"""
CREATE TRIGGER ck_xhs_artifact_journal_binding_update
BEFORE UPDATE ON xhs_artifact_promotion_journal
WHEN {_XHS_ARTIFACT_JOURNAL_BINDING_WHEN}
BEGIN
    SELECT RAISE(ABORT, 'xhs artifact journal binding mismatch');
END
""".strip(),
    "ck_xhs_artifact_journal_no_delete": """
CREATE TRIGGER ck_xhs_artifact_journal_no_delete
BEFORE DELETE ON xhs_artifact_promotion_journal
BEGIN
    SELECT RAISE(ABORT, 'xhs artifact journal is durable');
END
""".strip(),
    "ck_xhs_artifact_journal_job_update": """
CREATE TRIGGER ck_xhs_artifact_journal_job_update
BEFORE UPDATE OF id, type, state, input_data ON jobs
WHEN EXISTS (
    SELECT 1 FROM xhs_artifact_promotion_journal AS journal
    LEFT JOIN job_artifacts AS artifact ON artifact.id=journal.artifact_id
    WHERE journal.job_id=OLD.id
    AND (
        NEW.id IS NOT journal.job_id
        OR journal.producer IS NOT 'xhs_cli_read_worker_v1'
        OR journal.artifact_kind IS NOT CASE NEW.type
            WHEN 'xhs_account_collection' THEN 'xhs_account_collection_raw'
            WHEN 'xhs_note_search' THEN 'xhs_note_search_raw'
            ELSE '' END
        OR (journal.state='completed' AND journal.resolution='committed'
            AND NEW.state IS NOT journal.target_state)
        OR (journal.artifact_id IS NOT NULL AND (
            artifact.id IS NULL
            OR json_valid(NEW.input_data)!=1
            OR json_type(NEW.input_data) IS NOT 'object'
            OR (NEW.type='xhs_account_collection' AND (
                json_type(NEW.input_data, '$.user_id') IS NOT 'text'
                OR json_type(artifact.metadata_json, '$.user_id') IS NOT 'text'
                OR json_extract(artifact.metadata_json, '$.user_id')
                    IS NOT json_extract(NEW.input_data, '$.user_id')))
            OR (NEW.type='xhs_note_search' AND (
                json_type(NEW.input_data, '$.keyword') IS NOT 'text'
                OR json_type(artifact.metadata_json, '$.keyword') IS NOT 'text'
                OR json_extract(artifact.metadata_json, '$.keyword')
                    IS NOT json_extract(NEW.input_data, '$.keyword')))
        ))
    )
)
BEGIN
    SELECT RAISE(ABORT, 'xhs artifact journal job binding mismatch');
END
""".strip(),
    "ck_xhs_artifact_journal_job_delete": """
CREATE TRIGGER ck_xhs_artifact_journal_job_delete
BEFORE DELETE ON jobs
WHEN EXISTS (
    SELECT 1 FROM xhs_artifact_promotion_journal AS journal
    WHERE journal.job_id=OLD.id
)
BEGIN
    SELECT RAISE(ABORT, 'xhs artifact journal job is durable');
END
""".strip(),
    "ck_xhs_artifact_journal_artifact_update": """
CREATE TRIGGER ck_xhs_artifact_journal_artifact_update
BEFORE UPDATE OF id, job_id, kind, producer, path, metadata_json ON job_artifacts
WHEN EXISTS (
    SELECT 1 FROM xhs_artifact_promotion_journal AS journal
    JOIN jobs AS job ON job.id=journal.job_id
    WHERE journal.artifact_id=OLD.id
    AND (
        NEW.id IS NOT journal.artifact_id
        OR NEW.job_id IS NOT journal.job_id
        OR NEW.kind IS NOT journal.artifact_kind
        OR NEW.producer IS NOT journal.producer
        OR NEW.path IS NOT journal.final_path
        OR json_valid(NEW.metadata_json)!=1
        OR json_type(NEW.metadata_json) IS NOT 'object'
        OR json_type(NEW.metadata_json, '$.artifact_id') IS NOT 'integer'
        OR json_extract(NEW.metadata_json, '$.artifact_id') IS NOT NEW.id
        OR json_type(NEW.metadata_json, '$.job_id') IS NOT 'text'
        OR json_extract(NEW.metadata_json, '$.job_id') IS NOT journal.job_id
        OR json_type(NEW.metadata_json, '$.sha256') IS NOT 'text'
        OR json_extract(NEW.metadata_json, '$.sha256') IS NOT journal.sha256
        OR json_type(NEW.metadata_json, '$.size_bytes') IS NOT 'integer'
        OR json_extract(NEW.metadata_json, '$.size_bytes') IS NOT journal.size_bytes
        OR (job.type='xhs_account_collection' AND (
            json_type(job.input_data, '$.user_id') IS NOT 'text'
            OR json_type(NEW.metadata_json, '$.user_id') IS NOT 'text'
            OR json_extract(NEW.metadata_json, '$.user_id')
                IS NOT json_extract(job.input_data, '$.user_id')))
        OR (job.type='xhs_note_search' AND (
            json_type(job.input_data, '$.keyword') IS NOT 'text'
            OR json_type(NEW.metadata_json, '$.keyword') IS NOT 'text'
            OR json_extract(NEW.metadata_json, '$.keyword')
                IS NOT json_extract(job.input_data, '$.keyword')))
    )
)
BEGIN
    SELECT RAISE(ABORT, 'xhs artifact journal artifact binding mismatch');
END
""".strip(),
    "ck_xhs_artifact_journal_artifact_delete": """
CREATE TRIGGER ck_xhs_artifact_journal_artifact_delete
BEFORE DELETE ON job_artifacts
WHEN EXISTS (
    SELECT 1 FROM xhs_artifact_promotion_journal AS journal
    WHERE journal.artifact_id=OLD.id
)
BEGIN
    SELECT RAISE(ABORT, 'xhs artifact journal artifact is durable');
END
""".strip(),
}


class Base(DeclarativeBase):
    """Base class for all persisted workbench records."""


class SchemaMigrationError(SQLAlchemyError):
    """Raised when a migration marker contradicts the physical SQLite schema."""


_WINDOWS_RESERVED_STEMS = {
    "con", "prn", "aux", "nul", "conin$", "conout$", "clock$",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
    "com¹", "com²", "com³", "lpt¹", "lpt²", "lpt³",
}


def is_canonical_uuid_text(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def canonical_artifact_path_key(value: object) -> str | None:
    """Return one Windows-equivalent key only for a canonical managed path."""

    if not isinstance(value, str) or not value or unicodedata.normalize("NFC", value) != value:
        return None
    if value.startswith("/") or "\\" in value or ":" in value:
        return None
    parts = value.split("/")
    if not parts:
        return None
    canonical_parts: list[str] = []
    for part in parts:
        if (
            not part
            or part in {".", ".."}
            or part.strip() != part
            or part.endswith((".", " "))
            or any(
                ord(char) < 32
                or 127 <= ord(char) <= 159
                or char in '<>:"/\\|?*'
                for char in part
            )
        ):
            return None
        stem = part.split(".", 1)[0].casefold()
        if stem in _WINDOWS_RESERVED_STEMS:
            return None
        canonical_parts.append(unicodedata.normalize("NFC", part.casefold()))
    return "/".join(canonical_parts)


def windows_artifact_reference_path_key(value: object) -> str | None:
    """Normalize Windows-equivalent relative paths for cross-table guards."""

    if not isinstance(value, str) or not value:
        return None
    rendered = unicodedata.normalize("NFC", value).replace("\\", "/")
    if rendered.startswith("/") or ":" in rendered:
        return None
    parts: list[str] = []
    for raw_part in rendered.split("/"):
        if not raw_part or raw_part == ".":
            continue
        if raw_part == "..":
            if not parts:
                return None
            parts.pop()
            continue
        part = raw_part.rstrip(" .")
        if not part:
            return None
        parts.append(unicodedata.normalize("NFC", part.casefold()))
    return "/".join(parts) if parts else None


def canonical_raw_evidence_digest(value: object) -> str | None:
    """Hash one non-empty JSON object using one byte-stable canonical form."""

    try:
        parsed = json.loads(value) if isinstance(value, str) else value
        if not isinstance(parsed, dict) or not parsed:
            return None
        encoded = json.dumps(
            parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
        return None
    return hashlib.sha256(encoded).hexdigest()


class Database:
    """Own the local SQLite engine and initialize its durable schema."""

    def __init__(self, database_path: Path, *, runtime_dir: Path | None = None) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.database_path = database_path
        self.runtime_dir = runtime_dir.resolve() if runtime_dir is not None else None
        self.engine = create_engine(
            f"sqlite:///{database_path.as_posix()}",
            connect_args={"check_same_thread": False},
        )
        event.listen(self.engine, "connect", _configure_sqlite)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        try:
            self.initialize()
        except Exception:
            self.close()
            raise

    def initialize(self) -> None:
        """Create schema without creating any business records."""
        from backend.app.features.analysis.models import AnalysisRecord, OpportunityRecord
        from backend.app.features.content.models import (
            ArtifactCleanupRecord,
            ContentItemRecord,
            ContentPackageRecord,
            ContentReviewRecord,
            ContentRevisionRecord,
            ProductMaterialRecord,
            ProductRecord,
        )
        from backend.app.features.media.models import ContentMediaRunRecord
        from backend.app.features.radar.models import RankItemRecord, RankSnapshotRecord
        from backend.app.features.xhs.models import (
            XhsAccountNoteRecord,
            XhsAccountProfileRecord,
            XhsAccountProfileSnapshotRecord,
            XhsAccountSnapshotNoteRecord,
            XhsArtifactPromotionJournalRecord,
        )
        from backend.app.models.jobs import JobArtifactRecord, JobLogRecord, JobRecord

        _ = (
            AnalysisRecord,
            OpportunityRecord,
            ArtifactCleanupRecord,
            ContentItemRecord,
            ContentPackageRecord,
            ContentReviewRecord,
            ContentRevisionRecord,
            ProductMaterialRecord,
            ProductRecord,
            ContentMediaRunRecord,
            JobArtifactRecord,
            JobLogRecord,
            JobRecord,
            RankItemRecord,
            RankSnapshotRecord,
            XhsAccountNoteRecord,
            XhsAccountProfileRecord,
            XhsAccountProfileSnapshotRecord,
            XhsAccountSnapshotNoteRecord,
            XhsArtifactPromotionJournalRecord,
        )
        xhs_account_note_marker_present = self._migration_marker_exists(
            "xhs_account_note_evidence_v1"
        )
        xhs_account_note_identity_marker_present = self._migration_marker_exists(
            "xhs_account_note_identity_v2"
        )
        xhs_account_note_canonical_id_marker_present = self._migration_marker_exists(
            "xhs_account_note_canonical_id_v3"
        )
        xhs_account_fact_content_binding_marker_present = (
            self._migration_marker_exists(
                XHS_ACCOUNT_FACT_CONTENT_BINDING_MIGRATION
            )
        )
        xhs_account_snapshot_marker_present = self._migration_marker_exists(
            XHS_ACCOUNT_SNAPSHOT_EVIDENCE_MIGRATION
        )
        xhs_artifact_journal_marker_present = self._migration_marker_exists(
            XHS_ARTIFACT_PROMOTION_JOURNAL_MIGRATION
        )
        quarantine_marker_present = self._migration_marker_exists(
            "task8_artifact_quarantine_v1"
        )
        quarantine_identity_marker_present = self._migration_marker_exists(
            "task8_artifact_quarantine_identity_v1"
        )
        quarantine_reference_guard_marker_present = self._migration_marker_exists(
            "task8_artifact_quarantine_reference_guard_v1"
        )
        quarantine_source_token_marker_present = self._migration_marker_exists(
            "task8_artifact_quarantine_source_token_v1"
        )
        review_audit_marker_present = self._migration_marker_exists(
            "task8_content_review_outcome_v1"
        )
        analysis_evidence_snapshot_marker_present = self._migration_marker_exists(
            ANALYSIS_EVIDENCE_SNAPSHOT_MIGRATION
        )
        content_media_runs_marker_present = self._migration_marker_exists(
            CONTENT_MEDIA_RUNS_MIGRATION
        )
        xhs_account_snapshot_upgrade_started = False
        with self.engine.connect() as connection:
            _require_no_xhs_account_note_identity_leftovers(connection)
            _require_no_xhs_account_snapshot_leftovers(connection)
            _require_no_xhs_artifact_promotion_leftovers(connection)
            if not xhs_account_snapshot_marker_present:
                _require_xhs_account_snapshot_precreate_state(connection)
                tables = set(inspect(connection).get_table_names())
                xhs_account_snapshot_upgrade_started = (
                    _xhs_account_note_versioned_shape_valid(connection)
                    or bool(tables & {
                        "xhs_account_profile_snapshots",
                        "xhs_account_snapshot_notes",
                    })
                )
        if review_audit_marker_present:
            self._require_content_review_audit_schema()
        if analysis_evidence_snapshot_marker_present:
            self._require_analysis_evidence_snapshot_schema()
        if content_media_runs_marker_present:
            self._require_content_media_run_schema()
        if quarantine_marker_present:
            self._require_artifact_quarantine_schema(
                require_identity=quarantine_identity_marker_present,
                require_source_token=quarantine_source_token_marker_present,
            )
        if quarantine_reference_guard_marker_present and quarantine_source_token_marker_present:
            self._require_artifact_quarantine_reference_guards()
        if xhs_account_note_marker_present:
            self._require_xhs_account_note_evidence_schema()
        if xhs_account_note_identity_marker_present:
            self._require_xhs_account_note_identity_schema()
        if xhs_account_note_canonical_id_marker_present:
            self._require_xhs_account_note_canonical_id_schema()
        xhs_account_fact_content_binding_prerequisites_present = all((
            xhs_account_note_marker_present,
            xhs_account_note_identity_marker_present,
            xhs_account_note_canonical_id_marker_present,
        ))
        if xhs_artifact_journal_marker_present:
            self._require_xhs_artifact_promotion_journal_schema()
        if xhs_account_snapshot_marker_present:
            self._require_xhs_account_snapshot_evidence()
        if (
            xhs_account_fact_content_binding_marker_present
            and xhs_account_fact_content_binding_prerequisites_present
            and not xhs_account_snapshot_marker_present
            and not xhs_account_snapshot_upgrade_started
        ):
            self._require_xhs_account_fact_content_binding()
        Base.metadata.create_all(self.engine)
        self._migrate_artifact_provenance()
        self._migrate_analysis_scope()
        self._migrate_content_review_audit(
            marker_present=review_audit_marker_present
        )
        self._migrate_content_schema()
        self._migrate_content_review_audit(
            marker_present=self._migration_marker_exists(
                "task8_content_review_outcome_v1"
            )
        )
        self._migrate_artifact_quarantine(
            marker_present=quarantine_marker_present
        )
        self._migrate_artifact_quarantine_identity(
            marker_present=quarantine_identity_marker_present
        )
        self._migrate_artifact_quarantine_source_token(
            marker_present=quarantine_source_token_marker_present
        )
        self._migrate_artifact_quarantine_reference_guards(
            marker_present=quarantine_reference_guard_marker_present
        )
        self._migrate_xhs_account_note_evidence(
            marker_present=xhs_account_note_marker_present
        )
        self._migrate_xhs_account_note_identity(
            marker_present=xhs_account_note_identity_marker_present
        )
        self._migrate_xhs_account_note_canonical_ids(
            marker_present=xhs_account_note_canonical_id_marker_present
        )
        self._migrate_xhs_artifact_promotion_journal(
            marker_present=xhs_artifact_journal_marker_present
        )
        if (
            not xhs_account_snapshot_marker_present
            and not xhs_account_snapshot_upgrade_started
        ):
            self._migrate_xhs_account_fact_content_binding(
                marker_present=(
                    xhs_account_fact_content_binding_marker_present
                    and xhs_account_fact_content_binding_prerequisites_present
                )
            )
        self._migrate_xhs_account_snapshot_evidence(
            marker_present=xhs_account_snapshot_marker_present
        )
        self._migrate_analysis_evidence_snapshot(
            marker_present=analysis_evidence_snapshot_marker_present
        )
        self._migrate_content_media_runs(
            marker_present=content_media_runs_marker_present
        )
        self._recover_stranded_content_regenerations()

    def _migration_marker_exists(self, name: str) -> bool:
        inspector = inspect(self.engine)
        if "workbench_schema_migrations" not in inspector.get_table_names():
            return False
        with self.engine.connect() as connection:
            return connection.scalar(
                text(
                    "SELECT 1 FROM workbench_schema_migrations WHERE name=:name"
                ),
                {"name": name},
            ) is not None

    def _require_content_media_run_schema(self) -> None:
        with self.engine.connect() as connection:
            if not _content_media_run_schema_valid(connection):
                raise SchemaMigrationError(
                    "content media run schema validation failed."
                )

    def _migrate_content_media_runs(self, *, marker_present: bool) -> None:
        """Mark only the exact new schema; marker-present startup is validation-only."""

        if marker_present:
            self._require_content_media_run_schema()
            return
        with self.engine.begin() as connection:
            if not _content_media_run_schema_valid(connection):
                raise SchemaMigrationError(
                    "content media run schema validation failed; partial migration requires recovery."
                )
            connection.execute(text(
                "CREATE TABLE IF NOT EXISTS workbench_schema_migrations ("
                "name VARCHAR(200) PRIMARY KEY, applied_at VARCHAR(40) NOT NULL)"
            ))
            connection.execute(text(
                "INSERT INTO workbench_schema_migrations(name, applied_at) "
                "VALUES (:name, CURRENT_TIMESTAMP)"
            ), {"name": CONTENT_MEDIA_RUNS_MIGRATION})
        self._require_content_media_run_schema()

    def _recover_stranded_content_regenerations(self) -> None:
        """Fail closed exact regeneration reservations left by a stopped process."""
        with self.engine.begin() as connection:
            tables = set(inspect(connection).get_table_names())
            if not {"content_items", "content_reviews"}.issubset(tables):
                return
            connection.execute(text(
                "UPDATE content_reviews SET outcome='failed', "
                "error_category='transaction_unknown' "
                "WHERE decision='regenerate' AND outcome='pending' "
                "AND error_category IS NULL AND EXISTS ("
                "SELECT 1 FROM content_items i WHERE i.id=content_reviews.content_item_id "
                "AND i.status='draft' AND i.current_revision_id=content_reviews.revision_id)"
            ))
            connection.execute(text(
                "UPDATE content_items SET status='rejected', updated_at=CURRENT_TIMESTAMP "
                "WHERE status='draft' AND current_revision_id IS NOT NULL AND EXISTS ("
                "SELECT 1 FROM content_reviews r WHERE r.content_item_id=content_items.id "
                "AND r.revision_id=content_items.current_revision_id "
                "AND r.decision='regenerate' AND r.outcome='failed' "
                "AND r.error_category IN ('model_failure','validation_failed','trust_changed',"
                "'transaction_unknown','state_changed'))"
            ))

    def _require_artifact_quarantine_schema(
        self, *, require_identity: bool = True, require_source_token: bool = True
    ) -> None:
        with self.engine.connect() as connection:
            if (
                not _artifact_quarantine_schema_valid(
                    inspect(connection), require_identity=require_identity,
                    require_source_token=require_source_token,
                )
                or not _artifact_quarantine_triggers_valid(
                    connection, require_identity=require_identity
                )
                or not _artifact_quarantine_data_valid(
                    connection, require_identity=require_identity,
                    require_source_token=require_source_token,
                )
            ):
                raise SchemaMigrationError(
                    "Task 8 artifact cleanup schema validation failed."
                )

    def _require_xhs_account_note_evidence_schema(self) -> None:
        with self.engine.connect() as connection:
            if not _xhs_account_note_evidence_schema_valid(
                inspect(connection), connection
            ) or not _xhs_account_note_evidence_data_valid(connection):
                raise SchemaMigrationError(
                    "XHS account note evidence schema validation failed."
                )

    def _require_xhs_account_note_identity_schema(self) -> None:
        with self.engine.connect() as connection:
            if not _xhs_account_note_identity_schema_valid(connection):
                raise SchemaMigrationError(
                    "XHS account note identity schema validation failed."
                )

    def _require_xhs_account_note_canonical_id_schema(self) -> None:
        with self.engine.connect() as connection:
            if not _xhs_account_note_canonical_id_schema_valid(connection):
                raise SchemaMigrationError(
                    "XHS account note canonical id schema validation failed."
                )

    def _require_xhs_account_fact_content_binding(self) -> None:
        with self.engine.connect() as connection:
            if (
                not _xhs_account_fact_immutability_triggers_valid(connection)
                or not _xhs_account_fact_content_data_valid(
                    connection,
                    runtime_dir=self.runtime_dir,
                )
            ):
                raise SchemaMigrationError(
                    "XHS account fact content binding validation failed."
                )

    def _require_xhs_account_snapshot_evidence(self) -> None:
        """Validate the complete v5 physical contract without repairing it."""

        with self.engine.connect() as connection:
            _require_no_xhs_account_snapshot_leftovers(connection)
            if (
                not _xhs_account_snapshot_schema_valid(connection)
                or not _xhs_account_snapshot_data_valid(
                    connection,
                    runtime_dir=self.runtime_dir,
                )
            ):
                raise SchemaMigrationError(
                    "XHS account snapshot evidence content binding validation failed."
                )

    def _require_xhs_artifact_promotion_journal_schema(self) -> None:
        with self.engine.connect() as connection:
            if not _xhs_artifact_promotion_journal_schema_valid(
                inspect(connection), connection
            ) or not _xhs_artifact_promotion_journal_data_valid(connection):
                raise SchemaMigrationError(
                    "XHS artifact promotion journal schema validation failed."
                )

    def _migrate_xhs_account_note_evidence(self, *, marker_present: bool) -> None:
        """Install only an empty or fully-valid evidence schema; never guess history."""
        from backend.app.features.xhs.models import (
            XhsAccountNoteRecord,
            XhsAccountProfileRecord,
        )

        if marker_present:
            self._require_xhs_account_note_evidence_schema()
            return
        with self.engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE IF NOT EXISTS workbench_schema_migrations ("
                "name VARCHAR(200) PRIMARY KEY, applied_at VARCHAR(40) NOT NULL)"
            ))
            inspector = inspect(connection)
            if not _xhs_account_note_evidence_schema_valid(inspector, connection):
                tables = set(inspector.get_table_names())
                present = tables & {"xhs_account_profiles", "xhs_account_notes"}
                populated = any(
                    connection.scalar(text(f"SELECT COUNT(*) FROM {table}"))
                    for table in present
                )
                if populated:
                    raise SchemaMigrationError(
                        "Historical XHS account note evidence requires isolated manual migration."
                    )
                for trigger in _XHS_ACCOUNT_NOTE_EVIDENCE_TRIGGER_SQL:
                    connection.execute(text(f"DROP TRIGGER IF EXISTS {trigger}"))
                if "xhs_account_notes" in tables:
                    connection.execute(text("DROP TABLE xhs_account_notes"))
                if "xhs_account_profiles" in tables:
                    connection.execute(text("DROP TABLE xhs_account_profiles"))
                XhsAccountProfileRecord.__table__.create(connection)
                XhsAccountNoteRecord.__table__.create(connection)
            _create_xhs_account_note_evidence_triggers(connection)
            if not _xhs_account_note_evidence_schema_valid(
                inspect(connection), connection
            ) or not _xhs_account_note_evidence_data_valid(connection):
                raise SchemaMigrationError(
                    "XHS account note evidence schema validation failed."
                )
            connection.execute(text(
                "INSERT INTO workbench_schema_migrations(name, applied_at) "
                "VALUES ('xhs_account_note_evidence_v1', CURRENT_TIMESTAMP)"
            ))
        self._require_xhs_account_note_evidence_schema()

    def _migrate_xhs_account_note_identity(self, *, marker_present: bool) -> None:
        """Install permanent note-row identities without reassigning existing IDs."""
        from backend.app.features.xhs.models import XhsAccountNoteRecord

        if marker_present:
            self._require_xhs_account_note_identity_schema()
            return
        # A missing marker authorizes no write until the complete v1 evidence
        # schema and its rows have passed a read-only preflight. Recheck inside
        # the migration transaction below so a concurrent initializer cannot
        # turn this preflight into a time-of-check/time-of-use gap.
        with self.engine.connect() as connection:
            _require_xhs_account_note_identity_preconditions(connection)
        with self.engine.begin() as connection:
            _require_xhs_account_note_identity_preconditions(connection)
            connection.execute(text(
                "CREATE TABLE IF NOT EXISTS workbench_schema_migrations ("
                "name VARCHAR(200) PRIMARY KEY, applied_at VARCHAR(40) NOT NULL)"
            ))
            if connection.scalar(text(
                "SELECT 1 FROM workbench_schema_migrations "
                "WHERE name='xhs_account_note_identity_v2'"
            )) is not None:
                if not _xhs_account_note_identity_schema_valid(connection):
                    raise SchemaMigrationError(
                        "XHS account note identity schema validation failed."
                    )
                return
            if not _xhs_account_note_autoincrement_ddl_valid(connection):
                _rebuild_xhs_account_notes_with_permanent_ids(
                    connection, XhsAccountNoteRecord
                )
            _advance_xhs_account_note_identity_sequence(connection)
            if not _xhs_account_note_identity_schema_valid(connection):
                raise SchemaMigrationError(
                    "XHS account note identity schema validation failed."
                )
            connection.execute(text(
                "INSERT INTO workbench_schema_migrations(name, applied_at) "
                "VALUES ('xhs_account_note_identity_v2', CURRENT_TIMESTAMP)"
            ))
        self._require_xhs_account_note_identity_schema()

    def _migrate_xhs_account_note_canonical_ids(
        self,
        *,
        marker_present: bool,
    ) -> None:
        """Add a physical positive-rowid check without repairing poisoned rows."""
        from backend.app.features.xhs.models import XhsAccountNoteRecord

        if marker_present:
            self._require_xhs_account_note_canonical_id_schema()
            return
        with self.engine.connect() as connection:
            sequence = _require_xhs_account_note_canonical_id_preconditions(connection)
        with self.engine.begin() as connection:
            sequence = max(
                sequence,
                _require_xhs_account_note_canonical_id_preconditions(connection),
            )
            if connection.scalar(text(
                "SELECT 1 FROM workbench_schema_migrations "
                "WHERE name='xhs_account_note_canonical_id_v3'"
            )) is not None:
                if not _xhs_account_note_canonical_id_schema_valid(connection):
                    raise SchemaMigrationError(
                        "XHS account note canonical id schema validation failed."
                    )
                return
            if not _xhs_account_note_canonical_id_check_valid(connection):
                _rebuild_xhs_account_notes_with_permanent_ids(
                    connection,
                    XhsAccountNoteRecord,
                    temporary_table="xhs_account_notes_canonical_id_v2",
                )
            _advance_xhs_account_note_identity_sequence(
                connection,
                minimum=sequence,
            )
            if not _xhs_account_note_canonical_id_schema_valid(connection):
                raise SchemaMigrationError(
                    "XHS account note canonical id schema validation failed."
                )
            connection.execute(text(
                "INSERT INTO workbench_schema_migrations(name, applied_at) "
                "VALUES ('xhs_account_note_canonical_id_v3', CURRENT_TIMESTAMP)"
            ))
        self._require_xhs_account_note_canonical_id_schema()

    def _migrate_xhs_account_fact_content_binding(
        self,
        *,
        marker_present: bool,
    ) -> None:
        """Freeze normalized facts after verifying every readable formal snapshot."""

        if marker_present:
            self._require_xhs_account_fact_content_binding()
            return
        with self.engine.begin() as connection:
            if not _xhs_account_note_canonical_id_schema_valid(connection):
                raise SchemaMigrationError(
                    "XHS account fact content binding prerequisites failed."
                )
            if not _xhs_account_fact_content_data_valid(
                connection,
                runtime_dir=self.runtime_dir,
            ):
                raise SchemaMigrationError(
                    "XHS account fact content binding validation failed."
                )
            for name, definition in _XHS_ACCOUNT_FACT_IMMUTABILITY_TRIGGERS.items():
                connection.execute(text(f"DROP TRIGGER IF EXISTS {name}"))
                connection.execute(text(definition))
            if not _xhs_account_fact_immutability_triggers_valid(connection):
                raise SchemaMigrationError(
                    "XHS account fact content binding trigger validation failed."
                )
            connection.execute(text(
                "INSERT OR IGNORE INTO workbench_schema_migrations(name, applied_at) "
                "VALUES (:name, CURRENT_TIMESTAMP)"
            ), {"name": XHS_ACCOUNT_FACT_CONTENT_BINDING_MIGRATION})
        self._require_xhs_account_fact_content_binding()

    def _migrate_xhs_account_snapshot_evidence(
        self,
        *,
        marker_present: bool,
    ) -> None:
        """Install append-only snapshots while preserving every canonical note id."""

        from backend.app.features.xhs.models import (
            XhsAccountNoteRecord,
            XhsAccountProfileSnapshotRecord,
            XhsAccountSnapshotNoteRecord,
        )

        if marker_present:
            self._require_xhs_account_snapshot_evidence()
            return
        with self.engine.connect() as connection:
            _require_xhs_account_snapshot_precreate_state(connection)
        with self.engine.begin() as connection:
            _require_xhs_account_snapshot_precreate_state(connection)
            if connection.scalar(text(
                "SELECT 1 FROM workbench_schema_migrations WHERE name=:name"
            ), {"name": XHS_ACCOUNT_SNAPSHOT_EVIDENCE_MIGRATION}) is not None:
                if (
                    not _xhs_account_snapshot_schema_valid(connection)
                    or not _xhs_account_snapshot_data_valid(
                        connection,
                        runtime_dir=self.runtime_dir,
                    )
                ):
                    raise SchemaMigrationError(
                        "XHS account snapshot evidence validation failed."
                    )
                return
            if (
                _xhs_account_snapshot_schema_valid(connection)
                and _xhs_account_snapshot_data_valid(
                    connection,
                    runtime_dir=self.runtime_dir,
                )
            ):
                connection.execute(text(
                    "INSERT INTO workbench_schema_migrations(name, applied_at) "
                    "VALUES (:name, CURRENT_TIMESTAMP)"
                ), {"name": XHS_ACCOUNT_SNAPSHOT_EVIDENCE_MIGRATION})
                return
            if (
                not _xhs_account_note_canonical_id_schema_valid(connection)
                or not _xhs_account_fact_content_data_valid(
                    connection,
                    runtime_dir=self.runtime_dir,
                )
            ):
                raise SchemaMigrationError(
                    "XHS account snapshot migration prerequisites failed."
                )
            for table in (
                "xhs_account_snapshot_notes",
                "xhs_account_profile_snapshots",
            ):
                if table in inspect(connection).get_table_names() and connection.scalar(
                    text(f"SELECT COUNT(*) FROM {table}")
                ):
                    raise SchemaMigrationError(
                        "Ambiguous populated XHS account snapshot migration must fail closed."
                    )
            definitions = {
                **_XHS_ACCOUNT_SNAPSHOT_BINDING_TRIGGERS,
                **_XHS_ACCOUNT_SNAPSHOT_IMMUTABILITY_TRIGGERS,
            }
            for name in definitions:
                connection.execute(text(f"DROP TRIGGER IF EXISTS {name}"))
            connection.execute(text("DROP TABLE IF EXISTS xhs_account_snapshot_notes"))
            connection.execute(text("DROP TABLE IF EXISTS xhs_account_profile_snapshots"))
            if not _xhs_account_note_versioned_shape_valid(connection):
                _rebuild_xhs_account_notes_with_permanent_ids(
                    connection,
                    XhsAccountNoteRecord,
                    temporary_table="xhs_account_notes_snapshot_v4",
                )
            XhsAccountProfileSnapshotRecord.__table__.create(connection)
            XhsAccountSnapshotNoteRecord.__table__.create(connection)
            connection.execute(text("""
                INSERT INTO xhs_account_profile_snapshots (
                    user_id, source_url, nickname, bio, public_stats_json,
                    raw_evidence, raw_digest, collection_job_id,
                    collection_artifact_id, collected_at
                )
                SELECT user_id, source_url, nickname, bio, public_stats_json,
                    raw_evidence, raw_digest, collection_job_id,
                    collection_artifact_id, collected_at
                FROM xhs_account_profiles ORDER BY user_id
            """))
            connection.execute(text("""
                INSERT INTO xhs_account_snapshot_notes (
                    note_record_id, snapshot_id, position
                )
                SELECT note.id, snapshot.id,
                    ROW_NUMBER() OVER (
                        PARTITION BY snapshot.id ORDER BY note.id
                    ) - 1
                FROM xhs_account_notes AS note
                JOIN xhs_account_profile_snapshots AS snapshot
                  ON snapshot.user_id=note.user_id
                 AND snapshot.collection_job_id=note.collection_job_id
                 AND snapshot.collection_artifact_id=note.collection_artifact_id
                ORDER BY note.id
            """))
            _advance_xhs_account_note_identity_sequence(connection)
            _create_xhs_account_snapshot_triggers(connection)
            if (
                not _xhs_account_snapshot_schema_valid(connection)
                or not _xhs_account_snapshot_data_valid(
                    connection,
                    runtime_dir=self.runtime_dir,
                )
            ):
                raise SchemaMigrationError(
                    "XHS account snapshot evidence validation failed."
                )
            connection.execute(text(
                "INSERT INTO workbench_schema_migrations(name, applied_at) "
                "VALUES (:name, CURRENT_TIMESTAMP)"
            ), {"name": XHS_ACCOUNT_SNAPSHOT_EVIDENCE_MIGRATION})
        self._require_xhs_account_snapshot_evidence()

    def _migrate_xhs_artifact_promotion_journal(
        self,
        *,
        marker_present: bool,
    ) -> None:
        """Install the restart journal without repairing populated unknown data."""
        from backend.app.features.xhs.models import XhsArtifactPromotionJournalRecord

        if marker_present:
            self._require_xhs_artifact_promotion_journal_schema()
            return
        with self.engine.connect() as connection:
            _require_no_xhs_artifact_promotion_leftovers(connection)
        with self.engine.begin() as connection:
            _require_no_xhs_artifact_promotion_leftovers(connection)
            connection.execute(text(
                "CREATE TABLE IF NOT EXISTS workbench_schema_migrations ("
                "name VARCHAR(200) PRIMARY KEY, applied_at VARCHAR(40) NOT NULL)"
            ))
            if connection.scalar(text(
                "SELECT 1 FROM workbench_schema_migrations WHERE name=:name"
            ), {"name": XHS_ARTIFACT_PROMOTION_JOURNAL_MIGRATION}) is not None:
                if not _xhs_artifact_promotion_journal_schema_valid(
                    inspect(connection), connection
                ) or not _xhs_artifact_promotion_journal_data_valid(connection):
                    raise SchemaMigrationError(
                        "XHS artifact promotion journal schema validation failed."
                    )
                return
            inspector = inspect(connection)
            legacy_marker = connection.scalar(text(
                "SELECT 1 FROM workbench_schema_migrations WHERE name=:name"
            ), {
                "name": _XHS_ARTIFACT_PROMOTION_JOURNAL_LEGACY_MIGRATION,
            }) is not None
            if legacy_marker:
                if (
                    not _xhs_artifact_promotion_journal_v1_schema_valid(
                        inspector,
                        connection,
                    )
                    or not _xhs_artifact_promotion_journal_data_valid(connection)
                ):
                    raise SchemaMigrationError(
                        "XHS artifact promotion journal v1 validation failed."
                    )
                _rebuild_xhs_artifact_promotion_journal_v2(
                    connection,
                    XhsArtifactPromotionJournalRecord,
                )
                inspector = inspect(connection)
            if not _xhs_artifact_promotion_journal_schema_valid(
                inspector, connection
            ):
                tables = set(inspector.get_table_names())
                if "xhs_artifact_promotion_journal" in tables:
                    populated = connection.scalar(text(
                        "SELECT COUNT(*) FROM xhs_artifact_promotion_journal"
                    ))
                    if populated:
                        raise SchemaMigrationError(
                            "Historical XHS artifact promotion journal requires "
                            "isolated manual migration."
                        )
                    connection.execute(text(
                        "DROP TABLE xhs_artifact_promotion_journal"
                    ))
                XhsArtifactPromotionJournalRecord.__table__.create(connection)
            _create_xhs_artifact_promotion_journal_triggers(connection)
            if not _xhs_artifact_promotion_journal_schema_valid(
                inspect(connection), connection
            ) or not _xhs_artifact_promotion_journal_data_valid(connection):
                raise SchemaMigrationError(
                    "XHS artifact promotion journal schema validation failed."
                )
            connection.execute(text(
                "INSERT INTO workbench_schema_migrations(name, applied_at) "
                "VALUES (:name, CURRENT_TIMESTAMP)"
            ), {"name": XHS_ARTIFACT_PROMOTION_JOURNAL_MIGRATION})
        self._require_xhs_artifact_promotion_journal_schema()

    def _migrate_content_schema(self) -> None:
        inspector = inspect(self.engine)
        if "content_products" not in inspector.get_table_names():
            return
        if not _content_schema_valid(inspector):
            tables = [
                "content_packages", "content_reviews", "content_revisions", "content_items",
                "content_product_materials", "content_products",
            ]
            with self.engine.begin() as connection:
                populated = any(
                    connection.scalar(text(f"SELECT COUNT(*) FROM {table}"))
                    for table in tables if table in inspector.get_table_names()
                )
                if populated:
                    raise SchemaMigrationError(
                        "Legacy or malformed Task 8 schema contains records and requires isolated manual migration."
                    )
                connection.execute(text("PRAGMA foreign_keys=OFF"))
                for table in tables:
                    connection.execute(text(f"DROP TABLE IF EXISTS {table}"))
                connection.execute(text("PRAGMA foreign_keys=ON"))
            Base.metadata.create_all(self.engine)
            inspector = inspect(self.engine)
        if not _content_schema_valid(inspector):
            raise SchemaMigrationError("Task 8 schema validation failed.")
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT OR IGNORE INTO workbench_schema_migrations (name, applied_at) "
                    "VALUES ('task8_content_v2', CURRENT_TIMESTAMP)"
                )
            )

    def _require_content_review_audit_schema(self) -> None:
        with self.engine.connect() as connection:
            if not _content_review_audit_schema_valid(
                inspect(connection), connection
            ):
                raise SchemaMigrationError(
                    "Task 8 content review audit schema validation failed."
                )

    def _migrate_content_review_audit(self, *, marker_present: bool) -> None:
        from backend.app.features.content.models import ContentReviewRecord

        if marker_present:
            self._require_content_review_audit_schema()
            return
        with self.engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE IF NOT EXISTS workbench_schema_migrations ("
                "name VARCHAR(200) PRIMARY KEY, applied_at VARCHAR(40) NOT NULL)"
            ))
            tables = set(inspect(connection).get_table_names())
            if "content_reviews" not in tables:
                raise SchemaMigrationError(
                    "Task 8 content review audit table is missing."
                )
            columns = {
                column["name"] for column in inspect(connection).get_columns("content_reviews")
            }
            legacy_columns = {
                "id", "content_item_id", "revision_id", "decision", "actor",
                "note", "visual_checks_json", "created_at",
            }
            if not legacy_columns.issubset(columns):
                # The older whole-Task-8 migrator owns incomplete historical
                # schemas. It can safely rebuild an empty database, while a
                # populated one remains fail-closed for manual recovery.
                return
            audit_columns = {"outcome", "error_category"}
            present = columns & audit_columns
            if present and present != audit_columns:
                raise SchemaMigrationError(
                    "Partial content review audit migration requires manual recovery."
                )
            if not present:
                connection.execute(text("DROP INDEX IF EXISTS uq_review_terminal_revision"))
                connection.execute(text("DROP INDEX IF EXISTS ix_content_reviews_content_item_id"))
                connection.execute(text(
                    "ALTER TABLE content_reviews RENAME TO content_reviews_audit_old"
                ))
                ContentReviewRecord.__table__.create(connection)
                connection.execute(text(
                    "INSERT INTO content_reviews "
                    "(id,content_item_id,revision_id,decision,actor,note,"
                    "visual_checks_json,outcome,error_category,created_at) "
                    "SELECT old.id,old.content_item_id,old.revision_id,old.decision,"
                    "old.actor,old.note,old.visual_checks_json,"
                    "CASE WHEN old.decision != 'regenerate' OR ("
                    "EXISTS (SELECT 1 FROM content_revisions prior "
                    "JOIN content_revisions successor ON "
                    "successor.content_item_id=prior.content_item_id AND "
                    "successor.number=prior.number+1 WHERE "
                    "prior.id=old.revision_id AND prior.content_item_id=old.content_item_id) "
                    "AND (SELECT COUNT(*) FROM content_reviews_audit_old sibling "
                    "WHERE sibling.content_item_id=old.content_item_id AND "
                    "sibling.revision_id=old.revision_id AND "
                    "sibling.decision='regenerate')=1) "
                    "THEN 'succeeded' ELSE 'failed' END,"
                    "CASE WHEN old.decision != 'regenerate' OR ("
                    "EXISTS (SELECT 1 FROM content_revisions prior "
                    "JOIN content_revisions successor ON "
                    "successor.content_item_id=prior.content_item_id AND "
                    "successor.number=prior.number+1 WHERE "
                    "prior.id=old.revision_id AND prior.content_item_id=old.content_item_id) "
                    "AND (SELECT COUNT(*) FROM content_reviews_audit_old sibling "
                    "WHERE sibling.content_item_id=old.content_item_id AND "
                    "sibling.revision_id=old.revision_id AND "
                    "sibling.decision='regenerate')=1) "
                    "THEN NULL ELSE 'state_changed' END,old.created_at "
                    "FROM content_reviews_audit_old old"
                ))
                connection.execute(text("DROP TABLE content_reviews_audit_old"))
            if not _content_review_audit_schema_valid(
                inspect(connection), connection
            ):
                raise SchemaMigrationError(
                    "Task 8 content review audit schema validation failed."
                )
            connection.execute(text(
                "INSERT INTO workbench_schema_migrations(name,applied_at) "
                "VALUES ('task8_content_review_outcome_v1',CURRENT_TIMESTAMP)"
            ))
        self._require_content_review_audit_schema()

    def _migrate_artifact_quarantine(self, *, marker_present: bool) -> None:
        """Install and validate durable cleanup facts, then recover interrupted builds."""
        from backend.app.features.content.models import ArtifactCleanupRecord

        if marker_present:
            self._require_artifact_quarantine_schema(
                require_identity=False, require_source_token=False
            )
            self._recover_stranded_content_packages()
            return

        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS workbench_schema_migrations ("
                    "name VARCHAR(200) PRIMARY KEY, applied_at VARCHAR(40) NOT NULL)"
                )
            )
            package_columns = {
                column["name"]: column
                for column in inspect(connection).get_columns("content_packages")
            }
            if "build_token" not in package_columns:
                connection.execute(
                    text("ALTER TABLE content_packages ADD COLUMN build_token VARCHAR(36)")
                )
            tables = set(inspect(connection).get_table_names())
            if "artifact_gc_queue" not in tables:
                ArtifactCleanupRecord.__table__.create(connection)
            elif not _artifact_quarantine_table_valid(
                inspect(connection), require_identity=False, require_source_token=False
            ):
                row_count = connection.scalar(
                    text("SELECT COUNT(*) FROM artifact_gc_queue")
                )
                if row_count:
                    raise SchemaMigrationError(
                        "Legacy artifact cleanup records require isolated manual migration."
                    )
                ArtifactCleanupRecord.__table__.drop(connection)
                ArtifactCleanupRecord.__table__.create(connection)

            _create_artifact_quarantine_triggers(connection)

            inspector = inspect(connection)
            if (
                not _artifact_quarantine_schema_valid(
                    inspector, require_identity=False, require_source_token=False
                )
                or not _artifact_quarantine_triggers_valid(
                    connection, require_identity=False
                )
                or not _artifact_quarantine_data_valid(
                    connection, require_identity=False, require_source_token=False
                )
            ):
                raise SchemaMigrationError("Task 8 artifact cleanup schema validation failed.")

        self._recover_stranded_content_packages(write_marker=True)
        self._require_artifact_quarantine_schema(
            require_identity=False, require_source_token=False
        )

    def _migrate_artifact_quarantine_identity(self, *, marker_present: bool) -> None:
        """Add durable quarantine identity without trusting a partial migration."""

        if marker_present:
            self._require_artifact_quarantine_schema(
                require_identity=True, require_source_token=False
            )
            return
        identity_columns = (
            "quarantine_volume_id",
            "quarantine_file_id",
            "quarantine_size_bytes",
            "quarantine_mtime_ns",
        )
        with self.engine.begin() as connection:
            columns = {
                column["name"]
                for column in inspect(connection).get_columns("artifact_gc_queue")
            }
            present = {name for name in identity_columns if name in columns}
            if present and present != set(identity_columns):
                raise SchemaMigrationError(
                    "Partial artifact cleanup identity migration requires manual recovery."
                )
            for name in identity_columns:
                if name not in columns:
                    connection.execute(
                        text(f"ALTER TABLE artifact_gc_queue ADD COLUMN {name} INTEGER")
                    )
            _create_artifact_quarantine_identity_triggers(connection)
            if (
                not _artifact_quarantine_schema_valid(
                    inspect(connection), require_identity=True, require_source_token=False
                )
                or not _artifact_quarantine_triggers_valid(
                    connection, require_identity=True
                )
                or not _artifact_quarantine_data_valid(
                    connection, require_identity=True, require_source_token=False
                )
            ):
                raise SchemaMigrationError(
                    "Task 8 artifact cleanup identity schema validation failed."
                )
            connection.execute(
                text(
                    "INSERT INTO workbench_schema_migrations (name, applied_at) "
                    "VALUES ('task8_artifact_quarantine_identity_v1', CURRENT_TIMESTAMP)"
                )
            )
        self._require_artifact_quarantine_schema(
            require_identity=True, require_source_token=False
        )

    def _require_artifact_quarantine_reference_guards(self) -> None:
        with self.engine.connect() as connection:
            if (
                not _artifact_reference_guard_triggers_valid(connection)
                or not _artifact_reference_guard_data_valid(connection)
            ):
                raise SchemaMigrationError(
                    "Task 8 artifact quarantine reference guard validation failed."
                )

    def _migrate_artifact_quarantine_source_token(
        self, *, marker_present: bool
    ) -> None:
        """Bind every package cleanup to the immutable builder generation."""
        from backend.app.features.content.models import ArtifactCleanupRecord

        if marker_present:
            self._require_artifact_quarantine_schema(
                require_identity=True, require_source_token=True
            )
            with self.engine.connect() as connection:
                if not _artifact_cleanup_source_token_triggers_valid(connection):
                    raise SchemaMigrationError(
                        "Task 8 artifact cleanup source-token guards are invalid."
                    )
            return

        with self.engine.begin() as connection:
            columns = {
                column["name"]
                for column in inspect(connection).get_columns("artifact_gc_queue")
            }
            has_source_token = "source_build_token" in columns
            token_join = (
                "AND (cleanup.source_build_token IS NULL OR "
                "package.build_token=cleanup.source_build_token) "
                if has_source_token else ""
            )
            token_requirement = (
                "(is_canonical_uuid(package.build_token)=1 AND "
                "(cleanup.source_build_token IS NULL OR "
                "package.build_token=cleanup.source_build_token))"
                if has_source_token
                else "is_canonical_uuid(package.build_token)=1"
            )
            invalid_source = connection.scalar(
                text(
                    "SELECT 1 FROM artifact_gc_queue AS cleanup "
                    "LEFT JOIN content_packages AS package ON "
                    "package.id=cleanup.owner_id "
                    "AND windows_artifact_path_key(package.path)="
                    "windows_artifact_path_key(cleanup.relative_path) "
                    "AND package.sha256=cleanup.expected_sha256 "
                    "AND package.size_bytes=cleanup.expected_size_bytes "
                    + token_join
                    + "WHERE "
                    + (
                        "(cleanup.owner_type='material' AND "
                        "cleanup.source_build_token IS NOT NULL) OR "
                        if has_source_token else ""
                    )
                    + "(cleanup.owner_type='content_package' AND ("
                    + token_requirement
                    + " IS NOT TRUE OR package.id IS NULL)) LIMIT 1"
                )
            )
            if invalid_source is not None:
                raise SchemaMigrationError(
                    "Historical package cleanup generation cannot be proven."
                )
            if "source_build_token" not in columns:
                connection.execute(
                    text(
                        "ALTER TABLE artifact_gc_queue "
                        "ADD COLUMN source_build_token VARCHAR(36)"
                    )
                )
            connection.execute(
                text(
                    "UPDATE artifact_gc_queue AS cleanup "
                    "SET source_build_token=("
                    "SELECT package.build_token FROM content_packages AS package "
                    "WHERE package.id=cleanup.owner_id "
                    "AND windows_artifact_path_key(package.path)="
                    "windows_artifact_path_key(cleanup.relative_path) "
                    "AND package.sha256=cleanup.expected_sha256 "
                    "AND package.size_bytes=cleanup.expected_size_bytes) "
                    "WHERE cleanup.owner_type='content_package' "
                    "AND cleanup.source_build_token IS NULL"
                )
            )
            invalid = connection.scalar(
                text(
                    "SELECT 1 FROM artifact_gc_queue WHERE "
                    "(owner_type='material' AND source_build_token IS NOT NULL) OR "
                    "(owner_type='content_package' AND "
                    "is_canonical_uuid(source_build_token) != 1) LIMIT 1"
                )
            )
            if invalid is not None:
                raise SchemaMigrationError(
                    "Historical package cleanup generation cannot be proven."
                )

            for trigger_name in (
                "ck_artifact_gc_identity_insert",
                "ck_artifact_gc_identity_update",
                "ck_gc_cleanup_reference_insert",
                "ck_gc_cleanup_reference_update",
                "ck_gc_material_path_insert",
                "ck_gc_material_path_update",
                "ck_gc_package_path_insert",
                "ck_gc_package_path_update",
            ):
                connection.execute(text(f"DROP TRIGGER IF EXISTS {trigger_name}"))
            connection.execute(text("DROP INDEX IF EXISTS uq_artifact_gc_open_owner"))
            connection.execute(
                text("ALTER TABLE artifact_gc_queue RENAME TO artifact_gc_queue_source_old")
            )
            ArtifactCleanupRecord.__table__.create(connection)
            target_columns = [column.name for column in ArtifactCleanupRecord.__table__.columns]
            column_list = ", ".join(target_columns)
            connection.execute(
                text(
                    f"INSERT INTO artifact_gc_queue ({column_list}) "
                    f"SELECT {column_list} FROM artifact_gc_queue_source_old"
                )
            )
            connection.execute(text("DROP TABLE artifact_gc_queue_source_old"))
            _create_artifact_quarantine_triggers(connection)
            _create_artifact_quarantine_identity_triggers(connection)
            _create_artifact_cleanup_source_token_triggers(connection)
            _create_artifact_reference_guard_triggers(connection)
            connection.execute(
                text(
                    "INSERT INTO workbench_schema_migrations (name, applied_at) "
                    "VALUES ('task8_artifact_quarantine_source_token_v1', "
                    "CURRENT_TIMESTAMP)"
                )
            )
        self._require_artifact_quarantine_schema(
            require_identity=True, require_source_token=True
        )
        with self.engine.connect() as connection:
            if not _artifact_cleanup_source_token_triggers_valid(connection):
                raise SchemaMigrationError(
                    "Task 8 artifact cleanup source-token guards are invalid."
                )

    def _migrate_artifact_quarantine_reference_guards(
        self, *, marker_present: bool
    ) -> None:
        if marker_present:
            self._require_artifact_quarantine_reference_guards()
            return
        with self.engine.begin() as connection:
            if not _artifact_reference_guard_data_valid(connection):
                raise SchemaMigrationError(
                    "Historical quarantine reference conflict requires isolated manual migration."
                )
            _create_artifact_reference_guard_triggers(connection)
            if not _artifact_reference_guard_triggers_valid(connection):
                raise SchemaMigrationError(
                    "Task 8 artifact quarantine reference guard validation failed."
                )
            connection.execute(
                text(
                    "INSERT INTO workbench_schema_migrations (name, applied_at) "
                    "VALUES ('task8_artifact_quarantine_reference_guard_v1', "
                    "CURRENT_TIMESTAMP)"
                )
            )
        self._require_artifact_quarantine_reference_guards()

    def _recover_stranded_content_packages(self, *, write_marker: bool = False) -> None:
        from backend.app.features.content.models import ArtifactCleanupRecord

        with self.engine.begin() as connection:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            stranded = connection.execute(
                text(
                    "SELECT id, path, sha256, size_bytes, build_token "
                    "FROM content_packages "
                    "WHERE status='building'"
                )
            ).mappings().all()
            for package in stranded:
                path_key = canonical_artifact_path_key(package["path"])
                if (
                    not is_canonical_uuid_text(package["id"])
                    or path_key is None
                ):
                    raise SchemaMigrationError(
                        "Interrupted package identity requires isolated manual migration."
                    )
                connection.execute(
                    sqlite_insert(ArtifactCleanupRecord).on_conflict_do_nothing(
                        index_elements=["owner_type", "owner_id", "path_key"],
                        index_where=text(
                            "state IN ('pending','claimed','quarantined','needs_human')"
                        ),
                    ),
                    {
                        "id": str(uuid4()),
                        "owner_type": "content_package",
                        "owner_id": package["id"],
                        "source_build_token": package["build_token"],
                        "relative_path": package["path"],
                        "path_key": path_key,
                        "expected_sha256": package["sha256"],
                        "expected_size_bytes": package["size_bytes"],
                        "state": "pending",
                        "reason": "worker_restart_required",
                        "not_before": now,
                        "lease_token": None,
                        "lease_expires_at": None,
                        "quarantine_path": None,
                        "attempt_count": 0,
                        "last_error_category": None,
                        "created_at": now,
                        "updated_at": now,
                        "completed_at": None,
                    },
                )
                advanced = connection.execute(
                    text(
                        "UPDATE artifact_gc_queue SET not_before=:now, updated_at=:now "
                        "WHERE owner_type='content_package' AND owner_id=:owner_id "
                        "AND relative_path=:relative_path AND path_key=:path_key "
                        "AND expected_sha256=:expected_sha256 "
                        "AND expected_size_bytes=:expected_size_bytes "
                        "AND source_build_token=:source_build_token "
                        "AND state='pending'"
                    ),
                    {
                        "now": now,
                        "owner_id": package["id"],
                        "relative_path": package["path"],
                        "path_key": path_key,
                        "expected_sha256": package["sha256"],
                        "expected_size_bytes": package["size_bytes"],
                        "source_build_token": package["build_token"],
                    },
                ).rowcount
                if advanced != 1:
                    raise SchemaMigrationError(
                        "Interrupted package cleanup identity requires isolated manual migration."
                    )
            connection.execute(
                text(
                    "UPDATE content_packages SET status='failed', "
                    "error_detail='worker_restart_required' WHERE status='building'"
                )
            )
            if write_marker:
                connection.execute(
                    text(
                        "INSERT INTO workbench_schema_migrations (name, applied_at) "
                        "VALUES ('task8_artifact_quarantine_v1', CURRENT_TIMESTAMP)"
                    )
                )

    def _migrate_artifact_provenance(self) -> None:
        columns = {
            column["name"]: column
            for column in inspect(self.engine).get_columns("job_artifacts")
        }
        if "producer" not in columns:
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE job_artifacts ADD COLUMN producer VARCHAR(64) "
                        "NOT NULL DEFAULT 'external'"
                    )
                )
            columns = {
                column["name"]: column
                for column in inspect(self.engine).get_columns("job_artifacts")
            }
        producer = columns.get("producer")
        if (
            producer is None
            or producer.get("nullable") is not False
            or "external" not in str(producer.get("default") or "")
        ):
            raise SchemaMigrationError("job_artifacts.producer schema is invalid")

    def _require_analysis_evidence_snapshot_schema(self) -> None:
        with self.engine.connect() as connection:
            if not _analysis_evidence_snapshot_schema_valid(connection):
                raise SchemaMigrationError(
                    "Analysis evidence snapshot schema validation failed."
                )

    def _require_analysis_evidence_snapshot(self) -> None:
        with self.engine.connect() as connection:
            if (
                not _analysis_evidence_snapshot_schema_valid(connection)
                or not _analysis_evidence_snapshot_data_valid(connection)
            ):
                raise SchemaMigrationError(
                    "Analysis evidence snapshot validation failed."
                )

    def _migrate_analysis_evidence_snapshot(
        self,
        *,
        marker_present: bool,
    ) -> None:
        if marker_present:
            self._require_analysis_evidence_snapshot()
            return
        with self.engine.begin() as connection:
            if connection.scalar(text(
                "SELECT 1 FROM workbench_schema_migrations WHERE name=:name"
            ), {"name": ANALYSIS_EVIDENCE_SNAPSHOT_MIGRATION}) is not None:
                if (
                    not _analysis_evidence_snapshot_schema_valid(connection)
                    or not _analysis_evidence_snapshot_data_valid(connection)
                ):
                    raise SchemaMigrationError(
                        "Analysis evidence snapshot validation failed."
                    )
                return
            columns = {
                item["name"]: item
                for item in inspect(connection).get_columns("analyses")
            }
            added_column = "evidence_snapshot_json" not in columns
            if added_column:
                connection.execute(text(
                    "ALTER TABLE analyses ADD COLUMN evidence_snapshot_json JSON "
                    "CONSTRAINT ck_analysis_evidence_snapshot_json CHECK ("
                    f"{_ANALYSIS_EVIDENCE_SNAPSHOT_CHECK})"
                ))
                connection.execute(text(
                    "UPDATE analyses SET evidence_snapshot_json=:snapshot "
                    "WHERE status='succeeded'"
                ), {
                    "snapshot": json.dumps(
                        _LEGACY_ANALYSIS_EVIDENCE_SNAPSHOT,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                })
            if not _analysis_evidence_snapshot_column_valid(connection):
                raise SchemaMigrationError(
                    "Analysis evidence snapshot column is invalid."
                )
            triggers_valid = _analysis_evidence_snapshot_triggers_valid(connection)
            data_valid = _analysis_evidence_snapshot_data_valid(connection)
            if not data_valid:
                raise SchemaMigrationError(
                    "Analysis evidence snapshot data is invalid."
                )
            if not triggers_valid:
                for name, definition in _ANALYSIS_EVIDENCE_SNAPSHOT_TRIGGERS.items():
                    connection.execute(text(f"DROP TRIGGER IF EXISTS {name}"))
                    connection.execute(text(definition))
            if (
                not _analysis_evidence_snapshot_schema_valid(connection)
                or not _analysis_evidence_snapshot_data_valid(connection)
            ):
                raise SchemaMigrationError(
                    "Analysis evidence snapshot validation failed."
                )
            connection.execute(text(
                "INSERT INTO workbench_schema_migrations(name, applied_at) "
                "VALUES (:name, CURRENT_TIMESTAMP)"
            ), {"name": ANALYSIS_EVIDENCE_SNAPSHOT_MIGRATION})
        self._require_analysis_evidence_snapshot()

    def _migrate_analysis_scope(self) -> None:
        """Upgrade the pre-scope Task 7 schema and quarantine its success claims."""
        columns = {
            column["name"]: column
            for column in inspect(self.engine).get_columns("analyses")
        }
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS workbench_schema_migrations ("
                    "name VARCHAR(200) PRIMARY KEY, applied_at VARCHAR(40) NOT NULL)"
                )
            )
            already_applied = connection.scalar(
                text(
                    "SELECT 1 FROM workbench_schema_migrations "
                    "WHERE name='task7_trusted_grounding_v2'"
                )
            )
            if already_applied:
                self._validate_analysis_scope_schema(columns)
                self._validate_analysis_scope_data(connection)
                return
            if "account_user_ids_json" not in columns:
                connection.execute(
                    text(
                        "ALTER TABLE analyses ADD COLUMN account_user_ids_json JSON "
                        "NOT NULL DEFAULT '[]'"
                    )
                )
            columns = {
                column["name"]: column
                for column in inspect(connection).get_columns("analyses")
            }
            self._validate_analysis_scope_schema(columns)
            rows = connection.execute(
                text("SELECT id, analysis_type, account_user_id, status FROM analyses")
            ).mappings()
            for row in rows:
                account_user_id = row["account_user_id"]
                account_ids = (
                    [account_user_id.strip()]
                    if row["analysis_type"] != "account_report"
                    and isinstance(account_user_id, str)
                    and account_user_id.strip()
                    else []
                )
                connection.execute(
                    text(
                        "UPDATE analyses SET account_user_ids_json=:account_ids "
                        "WHERE id=:analysis_id"
                    ),
                    {
                        "account_ids": json.dumps(account_ids, ensure_ascii=False),
                        "analysis_id": row["id"],
                    },
                )
            connection.execute(
                text(
                    "DELETE FROM opportunities WHERE analysis_id IN "
                    "(SELECT id FROM analyses WHERE status='succeeded')"
                )
            )
            connection.execute(
                text(
                    "UPDATE analyses SET status='needs_human', output_json=NULL, "
                    "error_category='grounding_reverification_required', "
                    "error_detail='Legacy success requires trusted evidence re-verification.' "
                    "WHERE status='succeeded'"
                )
            )
            self._validate_analysis_scope_data(connection)
            connection.execute(
                text(
                    "INSERT INTO workbench_schema_migrations (name, applied_at) "
                    "VALUES ('task7_trusted_grounding_v2', CURRENT_TIMESTAMP)"
                )
            )
        refreshed = {
            column["name"]: column
            for column in inspect(self.engine).get_columns("analyses")
        }
        self._validate_analysis_scope_schema(refreshed)
        with self.engine.connect() as connection:
            self._validate_analysis_scope_data(connection)

    @staticmethod
    def _validate_analysis_scope_schema(
        columns: dict[str, dict[str, object]],
    ) -> None:
        required_columns = {
            "id",
            "analysis_type",
            "account_user_id",
            "account_user_ids_json",
            "status",
            "prompt_version",
            "provider",
            "model",
            "input_digest",
            "evidence_ids_json",
            "output_json",
            "usage_json",
            "duration_ms",
            "attempts_json",
            "error_category",
            "error_detail",
            "created_at",
        }
        if not required_columns.issubset(columns):
            raise SchemaMigrationError("analyses schema is missing required columns")
        account_scope = columns.get("account_user_ids_json")
        if (
            account_scope is None
            or account_scope.get("nullable") is not False
            or not _is_exact_empty_json_array_default(
                account_scope.get("default")
            )
            or "JSON" not in str(account_scope.get("type") or "").upper()
        ):
            raise SchemaMigrationError("analyses.account_user_ids_json schema is invalid")

    @staticmethod
    def _validate_analysis_scope_data(connection: Connection) -> None:
        rows = connection.execute(
            text("SELECT id, account_user_ids_json FROM analyses")
        ).mappings()
        for row in rows:
            raw_scope = row["account_user_ids_json"]
            try:
                scope = json.loads(raw_scope) if isinstance(raw_scope, str) else None
            except (json.JSONDecodeError, RecursionError):
                scope = None
            if (
                not isinstance(scope, list)
                or len(scope) > 500
                or any(
                    not isinstance(account_user_id, str)
                    or not account_user_id.strip()
                    or len(account_user_id) > 500
                    for account_user_id in scope
                )
                or len({account_user_id.strip() for account_user_id in scope})
                != len(scope)
            ):
                raise SchemaMigrationError(
                    f"Analysis {row['id']} has invalid account scope data"
                )

    def session(self) -> Session:
        return self.sessions()

    def close(self) -> None:
        """Release pooled SQLite handles before an app instance is replaced."""
        self.engine.dispose()


def _configure_sqlite(connection: object, _: object) -> None:
    connection.create_function(  # type: ignore[union-attr]
        "artifact_path_key", 1, canonical_artifact_path_key, deterministic=True
    )
    connection.create_function(  # type: ignore[union-attr]
        "is_canonical_uuid", 1, lambda value: int(is_canonical_uuid_text(value)),
        deterministic=True,
    )
    connection.create_function(  # type: ignore[union-attr]
        "windows_artifact_path_key", 1, windows_artifact_reference_path_key,
        deterministic=True,
    )
    connection.create_function(  # type: ignore[union-attr]
        "raw_evidence_digest", 1, canonical_raw_evidence_digest, deterministic=True
    )
    connection.create_function(  # type: ignore[union-attr]
        "analysis_evidence_snapshot_v1_valid",
        5,
        _sqlite_analysis_evidence_snapshot_v1_valid,
        deterministic=True,
    )
    cursor = connection.cursor()  # type: ignore[union-attr]
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _is_exact_empty_json_array_default(value: object) -> bool:
    if not isinstance(value, str):
        return False
    expression = value.strip()
    while expression.startswith("(") and expression.endswith(")"):
        expression = expression[1:-1].strip()
    return expression == "'[]'"


def _analysis_evidence_snapshot_column_valid(connection: Connection) -> bool:
    try:
        inspector = inspect(connection)
        columns = {
            item["name"]: item for item in inspector.get_columns("analyses")
        }
        snapshot = columns.get("evidence_snapshot_json")
        checks = {
            item.get("name"): _compact_sql(item.get("sqltext"))
            for item in inspector.get_check_constraints("analyses")
        }
        return (
            snapshot is not None
            and snapshot.get("nullable") is True
            and str(snapshot.get("type") or "").upper() == "JSON"
            and checks.get("ck_analysis_evidence_snapshot_json")
            == _compact_sql(_ANALYSIS_EVIDENCE_SNAPSHOT_CHECK)
        )
    except (KeyError, TypeError, AttributeError, SQLAlchemyError):
        return False


def _analysis_evidence_snapshot_triggers_valid(connection: Connection) -> bool:
    names = ",".join(
        f"'{name}'" for name in _ANALYSIS_EVIDENCE_SNAPSHOT_TRIGGERS
    )
    try:
        actual = {
            name: _compact_sql(sql)
            for name, sql in connection.execute(text(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
                f"AND name IN ({names})"
            ))
        }
        return actual == {
            name: _compact_sql(definition)
            for name, definition in _ANALYSIS_EVIDENCE_SNAPSHOT_TRIGGERS.items()
        }
    except SQLAlchemyError:
        return False


def _analysis_evidence_snapshot_schema_valid(connection: Connection) -> bool:
    return (
        _analysis_evidence_snapshot_column_valid(connection)
        and _analysis_evidence_snapshot_triggers_valid(connection)
    )


def _decode_analysis_json(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, RecursionError):
        return None


def _analysis_evidence_snapshot_v1_valid(
    input_digest_value: object,
    account_user_id_value: object,
    account_user_ids_value: object,
    evidence_ids_value: object,
    snapshot_value: object,
) -> bool:
    try:
        stored_scope = _decode_analysis_json(account_user_ids_value)
        scope = (
            [account_user_id_value]
            if isinstance(account_user_id_value, str) and account_user_id_value
            else stored_scope
        )
        allowed_ids = _decode_analysis_json(evidence_ids_value)
        snapshot = _decode_analysis_json(snapshot_value)
        if (
            not isinstance(stored_scope, list)
            or not isinstance(scope, list)
            or not isinstance(allowed_ids, list)
            or any(
                not isinstance(account_id, str)
                or not account_id
                or len(account_id) > 500
                for account_id in scope
            )
            or len(scope) != len(set(scope))
            or any(
                not isinstance(evidence_id, str)
                or re.fullmatch(
                    r"(?:account-note|artifact|rank-item):[1-9][0-9]*",
                    evidence_id,
                    re.ASCII,
                ) is None
                or int(evidence_id.rpartition(":")[2])
                > 9_223_372_036_854_775_807
                for evidence_id in allowed_ids
            )
            or len(allowed_ids) != len(set(allowed_ids))
            or not isinstance(snapshot, dict)
            or set(snapshot) != {
                "schema_version",
                "trust_fingerprint",
                "account_scope",
                "allowed_ids",
                "facts",
                "trust",
                "artifact_bindings",
                "input_digest",
            }
        ):
            return False
        facts = snapshot.get("facts")
        trust = snapshot.get("trust")
        bindings = snapshot.get("artifact_bindings")
        fingerprint = snapshot.get("trust_fingerprint")
        input_digest = snapshot.get("input_digest")
        if (
            type(snapshot.get("schema_version")) is not int
            or snapshot.get("schema_version") != 1
            or snapshot.get("account_scope") != scope
            or snapshot.get("allowed_ids") != allowed_ids
            or not isinstance(facts, list)
            or not isinstance(trust, list)
            or not isinstance(bindings, list)
            or len(facts) != len(allowed_ids)
            or len(trust) != len(allowed_ids)
            or [
                item.get("evidence_id") if isinstance(item, dict) else None
                for item in facts
            ] != allowed_ids
            or [
                item.get("evidence_id") if isinstance(item, dict) else None
                for item in trust
            ] != allowed_ids
            or not isinstance(fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", fingerprint, re.ASCII) is None
            or not isinstance(input_digest, str)
            or input_digest != input_digest_value
            or re.fullmatch(r"[0-9a-f]{64}", input_digest, re.ASCII) is None
        ):
            return False
        fingerprint_value = json.dumps(
            {
                "account_scope": scope,
                "allowed_ids": allowed_ids,
                "facts": facts,
                "trust": trust,
                "artifact_bindings": bindings,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if hashlib.sha256(fingerprint_value.encode("utf-8")).hexdigest() != fingerprint:
            return False
        binding_ids: list[str] = []
        for binding in bindings:
            if not isinstance(binding, dict) or set(binding) != {
                "evidence_id",
                "artifact_id",
                "artifact_job_id",
                "relative_path",
                "sha256",
                "size_bytes",
                "file_identity",
            }:
                return False
            evidence_id = binding.get("evidence_id")
            artifact_id = binding.get("artifact_id")
            artifact_job_id = binding.get("artifact_job_id")
            relative_path = binding.get("relative_path")
            digest = binding.get("sha256")
            size_bytes = binding.get("size_bytes")
            identity = binding.get("file_identity")
            if (
                not isinstance(evidence_id, str)
                or evidence_id not in allowed_ids
                or isinstance(artifact_id, bool)
                or not isinstance(artifact_id, int)
                or not 1 <= artifact_id <= 9_223_372_036_854_775_807
                or not isinstance(artifact_job_id, str)
                or not artifact_job_id
                or not isinstance(relative_path, str)
                or not relative_path
                or "\\" in relative_path
                or relative_path.startswith("/")
                or any(part in {"", ".", ".."} for part in relative_path.split("/"))
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest, re.ASCII) is None
                or isinstance(size_bytes, bool)
                or not isinstance(size_bytes, int)
                or not 0 <= size_bytes <= 20 * 1024 * 1024
                or not isinstance(identity, list)
                or len(identity) != 4
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 0
                    for value in identity
                )
                or identity[2] != size_bytes
            ):
                return False
            binding_ids.append(evidence_id)
        if len(binding_ids) != len(set(binding_ids)):
            return False
        expected_file_evidence = {
            item["evidence_id"]
            for item in facts
            if isinstance(item, dict)
            and item.get("kind") in {"account_note", "shop_collection_result"}
        }
        return set(binding_ids) == expected_file_evidence
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError):
        return False


def _sqlite_analysis_evidence_snapshot_v1_valid(
    input_digest_value: object,
    account_user_id_value: object,
    account_user_ids_value: object,
    evidence_ids_value: object,
    snapshot_value: object,
) -> int:
    return int(_analysis_evidence_snapshot_v1_valid(
        input_digest_value,
        account_user_id_value,
        account_user_ids_value,
        evidence_ids_value,
        snapshot_value,
    ))


def _analysis_evidence_snapshot_data_valid(connection: Connection) -> bool:
    try:
        rows = connection.execute(text(
            "SELECT status, input_digest, account_user_id, account_user_ids_json, "
            "evidence_ids_json, evidence_snapshot_json, output_json FROM analyses"
        )).mappings().all()
    except SQLAlchemyError:
        return False
    for row in rows:
        status = row["status"]
        snapshot = _decode_analysis_json(row["evidence_snapshot_json"])
        output = _decode_analysis_json(row["output_json"])
        if status not in {"succeeded", "failed", "needs_human"}:
            return False
        if snapshot is None:
            if status == "succeeded" or output is not None:
                return False
            continue
        if snapshot == _LEGACY_ANALYSIS_EVIDENCE_SNAPSHOT:
            continue
        if not _analysis_evidence_snapshot_v1_valid(
            row["input_digest"],
            row["account_user_id"],
            row["account_user_ids_json"],
            row["evidence_ids_json"],
            row["evidence_snapshot_json"],
        ):
            return False
        if status == "succeeded" and not isinstance(output, dict):
            return False
    return True


def _content_review_audit_schema_valid(
    inspector: object, connection: Connection,
) -> bool:
    try:
        if "content_reviews" not in set(inspector.get_table_names()):
            return False
        columns = {
            item["name"]: item for item in inspector.get_columns("content_reviews")
        }
        if not {"outcome", "error_category"}.issubset(columns):
            return False
        if columns["outcome"].get("nullable") is not False:
            return False
        if columns["error_category"].get("nullable") is not True:
            return False
        default = _compact_sql(columns["outcome"].get("default"))
        if default not in {"'succeeded'", "succeeded"}:
            return False
        checks = {
            item.get("name"): _compact_sql(item.get("sqltext"))
            for item in inspector.get_check_constraints("content_reviews")
        }
        if checks.get("ck_review_outcome") != "outcomein('pending','succeeded','failed')":
            return False
        if checks.get("ck_review_error_category") != (
            "(outcome='failed'anderror_categoryisnotnullanderror_categoryin('model_failure','validation_failed',"
            "'trust_changed','transaction_unknown','state_changed'))or(outcomein"
            "('pending','succeeded')anderror_categoryisnull)"
        ):
            return False
        invalid = connection.scalar(text(
            "SELECT 1 FROM content_reviews WHERE "
            "outcome NOT IN ('pending','succeeded','failed') OR "
            "(outcome='failed' AND (error_category IS NULL OR error_category NOT IN "
            "('model_failure','validation_failed','trust_changed','transaction_unknown','state_changed'))) OR "
            "(outcome IN ('pending','succeeded') AND error_category IS NOT NULL) LIMIT 1"
        ))
        return invalid is None
    except (KeyError, TypeError, AttributeError, SQLAlchemyError):
        return False


def _content_schema_valid(inspector: object) -> bool:
    required_columns = {
        "content_products": {"id", "opportunity_id", "name", "target_user", "created_at"},
        "content_product_materials": {"id", "product_id", "logical_name", "logical_key", "version", "path", "sha256", "size_bytes", "media_type", "kind", "created_at"},
        "content_items": {"id", "product_id", "opportunity_id", "template_key", "status", "evidence_ids_json", "material_ids_json", "image_material_ids_json", "cover_material_id", "research_facts_json", "current_revision_id", "created_at", "updated_at"},
        "content_revisions": {"id", "content_item_id", "number", "title", "body", "claims_json", "source_evidence_ids_json", "image_plan_json", "model_provider", "model_name", "prompt_version", "usage_json", "attempts_json", "created_at"},
        "content_reviews": {"id", "content_item_id", "revision_id", "decision", "actor", "note", "visual_checks_json", "outcome", "error_category", "created_at"},
        "content_packages": {"id", "content_item_id", "revision_id", "status", "path", "sha256", "size_bytes", "created_at", "error_detail"},
    }
    try:
        tables = set(inspector.get_table_names())
        if not set(required_columns).issubset(tables):
            return False
        for table, required in required_columns.items():
            columns = {item["name"]: item for item in inspector.get_columns(table)}
            if not required.issubset(columns):
                return False
            for name in required - {"current_revision_id", "error_detail", "error_category"}:
                if columns[name].get("nullable") is not False:
                    return False

        required_checks = {
            "content_product_materials": {
                "ck_material_version_positive": "version>0",
                "ck_material_size_positive": "size_bytes>0",
                "ck_material_sha_format": "length(sha256)=64andsha256notglob'*[^0-9a-f]*'",
                "ck_material_kind": "kindin('source','output_image')",
            },
            "content_items": {
                "ck_content_item_status": "statusin('research','draft','review','rejected','approved','exported')",
            },
            "content_revisions": {"ck_revision_number_positive": "number>0"},
            "content_reviews": {
                "ck_review_decision": "decisionin('approve','reject','regenerate')",
                "ck_review_outcome": "outcomein('pending','succeeded','failed')",
                "ck_review_error_category": (
                    "(outcome='failed'anderror_categoryisnotnullanderror_categoryin('model_failure','validation_failed',"
                    "'trust_changed','transaction_unknown','state_changed'))or(outcomein"
                    "('pending','succeeded')anderror_categoryisnull)"
                ),
            },
            "content_packages": {
                "ck_package_status": "statusin('building','ready','failed')",
                "ck_package_size_nonnegative": "size_bytes>=0",
                "ck_package_sha_format": "length(sha256)=64andsha256notglob'*[^0-9a-f]*'",
            },
        }
        for table, expected in required_checks.items():
            actual = {item.get("name"): _compact_sql(item.get("sqltext")) for item in inspector.get_check_constraints(table)}
            if actual != expected:
                return False

        required_fks = {
            "content_products": {(('opportunity_id',), "opportunities", ('id',), "RESTRICT")},
            "content_product_materials": {(('product_id',), "content_products", ('id',), "CASCADE")},
            "content_items": {
                (('product_id',), "content_products", ('id',), "RESTRICT"),
                (('opportunity_id',), "opportunities", ('id',), "RESTRICT"),
                (('current_revision_id', 'id'), "content_revisions", ('id', 'content_item_id'), None),
            },
            "content_revisions": {(('content_item_id',), "content_items", ('id',), "CASCADE")},
            "content_reviews": {
                (('revision_id', 'content_item_id'), "content_revisions", ('id', 'content_item_id'), None),
                (('content_item_id',), "content_items", ('id',), "CASCADE"),
            },
            "content_packages": {
                (('revision_id', 'content_item_id'), "content_revisions", ('id', 'content_item_id'), None),
                (('content_item_id',), "content_items", ('id',), "RESTRICT"),
            },
        }
        for table, expected in required_fks.items():
            actual = {
                (
                    tuple(item.get("constrained_columns") or ()), item.get("referred_table"),
                    tuple(item.get("referred_columns") or ()),
                    (item.get("options") or {}).get("ondelete"),
                )
                for item in inspector.get_foreign_keys(table)
            }
            if actual != expected:
                return False

        required_unique = {
            "content_products": set(),
            "content_product_materials": {("product_id", "logical_key", "version")},
            "content_items": set(),
            "content_revisions": {("content_item_id", "number"), ("id", "content_item_id")},
            "content_reviews": set(),
            "content_packages": {("revision_id",)},
        }
        for table, expected in required_unique.items():
            actual = {tuple(item.get("column_names") or ()) for item in inspector.get_unique_constraints(table)}
            if actual != expected:
                return False

        required_indexes = {
            "content_products": {"ix_content_products_opportunity_id": (("opportunity_id",), False, "")},
            "content_product_materials": {"ix_content_product_materials_product_id": (("product_id",), False, "")},
            "content_items": {
                "ix_content_items_opportunity_id": (("opportunity_id",), False, ""),
                "ix_content_items_product_id": (("product_id",), False, ""),
                "ix_content_items_status": (("status",), False, ""),
            },
            "content_revisions": {"ix_content_revisions_content_item_id": (("content_item_id",), False, "")},
            "content_reviews": {
                "ix_content_reviews_content_item_id": (("content_item_id",), False, ""),
                "uq_review_terminal_revision": (("revision_id",), True, "decisionin('approve','reject')"),
            },
            "content_packages": {"ix_content_packages_content_item_id": (("content_item_id",), False, "")},
        }
        for table, expected in required_indexes.items():
            actual = {
                item.get("name"): (
                    tuple(item.get("column_names") or ()), bool(item.get("unique")),
                    _compact_sql((item.get("dialect_options") or {}).get("sqlite_where")),
                )
                for item in inspector.get_indexes(table)
            }
            if actual != expected:
                return False
    except (KeyError, TypeError, AttributeError, SQLAlchemyError):
        return False
    return True


def _content_media_run_schema_valid(connection: Connection) -> bool:
    """Validate the small physical contract that later workers may safely resume."""

    try:
        inspector = inspect(connection)
        if "content_media_runs" not in inspector.get_table_names():
            return False
        columns = {
            item["name"]: item for item in inspector.get_columns("content_media_runs")
        }
        required = {
            "id", "job_id", "owner_product_id", "content_item_id", "revision_id",
            "plan_entry_id", "capability", "status", "state_version", "provider",
            "model", "prompt_version", "input_digest", "allowed_evidence_ids_json",
            "allowed_material_ids_json", "output_material_id", "analysis_artifact_id",
            "usage_json", "duration_ms", "attempts_json", "error_category",
            "error_detail", "lease_token", "lease_expires_at", "created_at",
            "updated_at", "completed_at",
        }
        if set(columns) != required:
            return False
        nullable = {
            name for name, item in columns.items() if item.get("nullable") is True
        }
        if nullable != {
            "plan_entry_id", "output_material_id", "analysis_artifact_id",
            "duration_ms", "error_category", "error_detail", "lease_token",
            "lease_expires_at", "completed_at",
        }:
            return False
        checks = {
            item.get("name") for item in inspector.get_check_constraints("content_media_runs")
        }
        if checks != {
            "ck_content_media_id", "ck_content_media_capability",
            "ck_content_media_status", "ck_content_media_state_version",
            "ck_content_media_provider_model", "ck_content_media_input_digest",
            "ck_content_media_json", "ck_content_media_plan_entry",
            "ck_content_media_duration", "ck_content_media_lease",
            "ck_content_media_outcome",
        }:
            return False
        unique = {
            (item.get("name"), tuple(item.get("column_names") or ()))
            for item in inspector.get_unique_constraints("content_media_runs")
        }
        if unique != {("uq_content_media_job", ("job_id",))}:
            return False
        indexes = {
            item.get("name"): (
                tuple(item.get("column_names") or ()), bool(item.get("unique")),
                _compact_sql((item.get("dialect_options") or {}).get("sqlite_where")),
            )
            for item in inspector.get_indexes("content_media_runs")
        }
        if indexes != {
            "ix_content_media_item_created": (
                ("content_item_id", "created_at"), False, "",
            ),
            "uq_content_media_open_generation": (
                ("content_item_id", "revision_id", "plan_entry_id"), True,
                "capability='generate'andstatusin('queued','running')",
            ),
        }:
            return False
        fks = {
            (
                tuple(item.get("constrained_columns") or ()),
                item.get("referred_table"),
                tuple(item.get("referred_columns") or ()),
                (item.get("options") or {}).get("ondelete"),
            )
            for item in inspector.get_foreign_keys("content_media_runs")
        }
        expected_fks = {
            (("job_id",), "jobs", ("id",), "RESTRICT"),
            (("owner_product_id",), "content_products", ("id",), "RESTRICT"),
            (("content_item_id",), "content_items", ("id",), "RESTRICT"),
            (("revision_id", "content_item_id"), "content_revisions", ("id", "content_item_id"), "RESTRICT"),
            (("output_material_id",), "content_product_materials", ("id",), "RESTRICT"),
            (("analysis_artifact_id",), "job_artifacts", ("id",), "RESTRICT"),
        }
        if fks != expected_fks:
            return False
        invalid_binding = connection.scalar(text(
            "SELECT 1 FROM content_media_runs r "
            "LEFT JOIN content_items i ON i.id=r.content_item_id "
            "LEFT JOIN content_revisions v ON v.id=r.revision_id AND v.content_item_id=r.content_item_id "
            "LEFT JOIN jobs j ON j.id=r.job_id "
            "LEFT JOIN content_product_materials m ON m.id=r.output_material_id "
            "LEFT JOIN job_artifacts a ON a.id=r.analysis_artifact_id "
            "WHERE i.id IS NULL OR i.product_id!=r.owner_product_id OR v.id IS NULL OR j.id IS NULL OR "
            "j.type!=CASE r.capability WHEN 'generate' THEN 'content_image_generation' ELSE 'content_image_analysis' END OR "
            "j.state!=r.status OR (m.id IS NOT NULL AND m.product_id!=r.owner_product_id) OR "
            "(a.id IS NOT NULL AND a.job_id!=r.job_id) LIMIT 1"
        ))
        if invalid_binding is not None:
            return False
        rows = connection.execute(text(
            "SELECT allowed_evidence_ids_json,allowed_material_ids_json,usage_json,attempts_json "
            "FROM content_media_runs"
        )).all()
        for raw_evidence, raw_materials, raw_usage, raw_attempts in rows:
            evidence = json.loads(raw_evidence) if isinstance(raw_evidence, str) else raw_evidence
            materials = json.loads(raw_materials) if isinstance(raw_materials, str) else raw_materials
            usage = json.loads(raw_usage) if isinstance(raw_usage, str) else raw_usage
            attempts = json.loads(raw_attempts) if isinstance(raw_attempts, str) else raw_attempts
            if (
                not isinstance(evidence, list) or len(evidence) > 500
                or not isinstance(materials, list) or len(materials) > 500
                or any(not isinstance(value, str) or not value.strip() or len(value) > 500 for value in evidence + materials)
                or len(evidence) != len(set(evidence)) or len(materials) != len(set(materials))
                or not isinstance(usage, dict) or len(usage) > 50
                or any(not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, int) or value < 0 for key, value in usage.items())
                or not isinstance(attempts, list) or len(attempts) > 20
                or any(not isinstance(value, dict) for value in attempts)
            ):
                return False
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, SQLAlchemyError):
        return False
    return True


def _compact_sql(value: object) -> str:
    """Normalize SQL layout without changing quoted literal or identifier bytes."""

    rendered = "" if value is None else str(value)
    compact: list[str] = []
    index = 0
    while index < len(rendered):
        char = rendered[index]
        if char.isspace():
            index += 1
            continue
        if char in {"'", '"', "`"}:
            delimiter = char
            compact.append(char)
            index += 1
            while index < len(rendered):
                char = rendered[index]
                compact.append(char)
                index += 1
                if char == delimiter:
                    if index < len(rendered) and rendered[index] == delimiter:
                        compact.append(rendered[index])
                        index += 1
                        continue
                    break
            continue
        if char == "[":
            compact.append(char)
            index += 1
            while index < len(rendered):
                char = rendered[index]
                compact.append(char)
                index += 1
                if char == "]":
                    if index < len(rendered) and rendered[index] == "]":
                        compact.append(rendered[index])
                        index += 1
                        continue
                    break
            continue
        compact.append(char.lower())
        index += 1
    return "".join(compact)


_XHS_TRUSTED_ARTIFACT_WHERE = f"""
    artifact.id=NEW.collection_artifact_id
    AND artifact.job_id=NEW.collection_job_id
    AND job.type='{ACCOUNT_COLLECTION_JOB_TYPE}'
    AND artifact.kind='{ACCOUNT_COLLECTION_ARTIFACT_KIND}'
    AND artifact.producer='{ACCOUNT_COLLECTION_ARTIFACT_PRODUCER}'
"""


_XHS_ACCOUNT_NOTE_EVIDENCE_TRIGGER_SQL = {
    "ck_xhs_profile_artifact_job_insert": f"""
        CREATE TRIGGER ck_xhs_profile_artifact_job_insert
        BEFORE INSERT ON xhs_account_profiles
        WHEN NOT EXISTS (
            SELECT 1 FROM job_artifacts AS artifact JOIN jobs AS job ON job.id=artifact.job_id
            WHERE {_XHS_TRUSTED_ARTIFACT_WHERE}
        )
        BEGIN
            SELECT RAISE(ABORT, 'XHS profile artifact must belong to its collection job');
        END
    """,
    "ck_xhs_profile_artifact_job_update": f"""
        CREATE TRIGGER ck_xhs_profile_artifact_job_update
        BEFORE UPDATE OF collection_job_id, collection_artifact_id ON xhs_account_profiles
        WHEN NOT EXISTS (
            SELECT 1 FROM job_artifacts AS artifact JOIN jobs AS job ON job.id=artifact.job_id
            WHERE {_XHS_TRUSTED_ARTIFACT_WHERE}
        )
        BEGIN
            SELECT RAISE(ABORT, 'XHS profile artifact must belong to its collection job');
        END
    """,
    "ck_xhs_note_artifact_job_insert": f"""
        CREATE TRIGGER ck_xhs_note_artifact_job_insert
        BEFORE INSERT ON xhs_account_notes
        WHEN NOT EXISTS (
            SELECT 1 FROM job_artifacts AS artifact JOIN jobs AS job ON job.id=artifact.job_id
            WHERE {_XHS_TRUSTED_ARTIFACT_WHERE}
        )
        BEGIN
            SELECT RAISE(ABORT, 'XHS note artifact must belong to its collection job');
        END
    """,
    "ck_xhs_note_artifact_job_update": f"""
        CREATE TRIGGER ck_xhs_note_artifact_job_update
        BEFORE UPDATE OF collection_job_id, collection_artifact_id ON xhs_account_notes
        WHEN NOT EXISTS (
            SELECT 1 FROM job_artifacts AS artifact JOIN jobs AS job ON job.id=artifact.job_id
            WHERE {_XHS_TRUSTED_ARTIFACT_WHERE}
        )
        BEGIN
            SELECT RAISE(ABORT, 'XHS note artifact must belong to its collection job');
        END
    """,
}


def _create_xhs_account_note_evidence_triggers(connection: Connection) -> None:
    for name, sql in _XHS_ACCOUNT_NOTE_EVIDENCE_TRIGGER_SQL.items():
        connection.execute(text(f"DROP TRIGGER IF EXISTS {name}"))
        connection.execute(text(sql))


_XHS_ACCOUNT_SNAPSHOT_BINDING_TRIGGERS = {
    "ck_xhs_profile_snapshot_artifact_job_insert": f"""
        CREATE TRIGGER ck_xhs_profile_snapshot_artifact_job_insert
        BEFORE INSERT ON xhs_account_profile_snapshots
        WHEN NOT EXISTS (
            SELECT 1 FROM job_artifacts AS artifact
            JOIN jobs AS job ON job.id=artifact.job_id
            WHERE {_XHS_TRUSTED_ARTIFACT_WHERE}
        ) OR NOT EXISTS (
            SELECT 1 FROM xhs_account_profiles AS profile
            WHERE profile.user_id=NEW.user_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'XHS profile snapshot binding is invalid');
        END
    """,
    "ck_xhs_snapshot_note_binding_insert": """
        CREATE TRIGGER ck_xhs_snapshot_note_binding_insert
        BEFORE INSERT ON xhs_account_snapshot_notes
        WHEN NOT EXISTS (
            SELECT 1 FROM xhs_account_profile_snapshots AS snapshot
            JOIN xhs_account_notes AS note ON note.id=NEW.note_record_id
            WHERE snapshot.id=NEW.snapshot_id
            AND note.user_id=snapshot.user_id
            AND note.collection_job_id=snapshot.collection_job_id
            AND note.collection_artifact_id=snapshot.collection_artifact_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'XHS snapshot note binding is invalid');
        END
    """,
}


_XHS_ACCOUNT_SNAPSHOT_IMMUTABILITY_TRIGGERS = {
    "ck_xhs_profile_immutable_update": """
        CREATE TRIGGER ck_xhs_profile_immutable_update
        BEFORE UPDATE ON xhs_account_profiles
        BEGIN
            SELECT RAISE(ABORT, 'xhs account profile facts are immutable');
        END
    """,
    "ck_xhs_note_immutable_update": """
        CREATE TRIGGER ck_xhs_note_immutable_update
        BEFORE UPDATE ON xhs_account_notes
        BEGIN
            SELECT RAISE(ABORT, 'xhs account note facts are immutable');
        END
    """,
    "ck_xhs_profile_immutable_delete": """
        CREATE TRIGGER ck_xhs_profile_immutable_delete
        BEFORE DELETE ON xhs_account_profiles
        BEGIN
            SELECT RAISE(ABORT, 'xhs account profile facts are append only');
        END
    """,
    "ck_xhs_note_immutable_delete": """
        CREATE TRIGGER ck_xhs_note_immutable_delete
        BEFORE DELETE ON xhs_account_notes
        BEGIN
            SELECT RAISE(ABORT, 'xhs account note facts are append only');
        END
    """,
    "ck_xhs_profile_snapshot_immutable_update": """
        CREATE TRIGGER ck_xhs_profile_snapshot_immutable_update
        BEFORE UPDATE ON xhs_account_profile_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'xhs account profile snapshots are immutable');
        END
    """,
    "ck_xhs_profile_snapshot_immutable_delete": """
        CREATE TRIGGER ck_xhs_profile_snapshot_immutable_delete
        BEFORE DELETE ON xhs_account_profile_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'xhs account profile snapshots are append only');
        END
    """,
    "ck_xhs_snapshot_note_immutable_update": """
        CREATE TRIGGER ck_xhs_snapshot_note_immutable_update
        BEFORE UPDATE ON xhs_account_snapshot_notes
        BEGIN
            SELECT RAISE(ABORT, 'xhs account snapshot membership is immutable');
        END
    """,
    "ck_xhs_snapshot_note_immutable_delete": """
        CREATE TRIGGER ck_xhs_snapshot_note_immutable_delete
        BEFORE DELETE ON xhs_account_snapshot_notes
        BEGIN
            SELECT RAISE(ABORT, 'xhs account snapshot membership is append only');
        END
    """,
}


def _create_xhs_account_snapshot_triggers(connection: Connection) -> None:
    definitions = {
        **_XHS_ACCOUNT_SNAPSHOT_BINDING_TRIGGERS,
        **_XHS_ACCOUNT_SNAPSHOT_IMMUTABILITY_TRIGGERS,
    }
    for name, sql in definitions.items():
        connection.execute(text(f"DROP TRIGGER IF EXISTS {name}"))
        connection.execute(text(sql))


_XHS_ACCOUNT_SNAPSHOT_TEMPORARY_TABLES = frozenset({
    "xhs_account_notes_snapshot_v4",
    "xhs_account_profile_snapshots_v5_rebuild",
    "xhs_account_snapshot_notes_v5_rebuild",
})


def _require_no_xhs_account_snapshot_leftovers(connection: Connection) -> None:
    known = {
        _sqlite_ascii_identifier_key(name)
        for name in _XHS_ACCOUNT_SNAPSHOT_TEMPORARY_TABLES
    }
    actual = {
        name
        for name in inspect(connection).get_table_names()
        if _sqlite_ascii_identifier_key(name) in known
    }
    if actual:
        raise SchemaMigrationError(
            "Interrupted XHS account snapshot migration requires isolated manual migration."
        )


def _xhs_account_note_versioned_shape_valid(connection: Connection) -> bool:
    try:
        inspector = inspect(connection)
        uniques = {
            tuple(item.get("column_names") or ())
            for item in inspector.get_unique_constraints("xhs_account_notes")
        }
        foreign_keys = {
            (
                tuple(item.get("constrained_columns") or ()),
                item.get("referred_table"),
                tuple(item.get("referred_columns") or ()),
                (item.get("options") or {}).get("ondelete"),
            )
            for item in inspector.get_foreign_keys("xhs_account_notes")
        }
        return (
            uniques == {("note_id", "user_id", "collection_job_id")}
            and (
                ("user_id",),
                "xhs_account_profiles",
                ("user_id",),
                "RESTRICT",
            ) in foreign_keys
            and _xhs_account_note_autoincrement_ddl_valid(connection)
            and _xhs_account_note_canonical_id_check_valid(connection)
        )
    except (KeyError, TypeError, AttributeError, SQLAlchemyError):
        return False


def _xhs_account_snapshot_schema_valid(connection: Connection) -> bool:
    inspector = inspect(connection)
    expected_columns = {
        "xhs_account_profile_snapshots": {
            "id": "INTEGER",
            "user_id": "VARCHAR(500)",
            "source_url": "TEXT",
            "nickname": "TEXT",
            "bio": "TEXT",
            "public_stats_json": "JSON",
            "raw_evidence": "JSON",
            "raw_digest": "VARCHAR(64)",
            "collection_job_id": "VARCHAR(36)",
            "collection_artifact_id": "INTEGER",
            "collected_at": "DATETIME",
        },
        "xhs_account_snapshot_notes": {
            "note_record_id": "INTEGER",
            "snapshot_id": "INTEGER",
            "position": "INTEGER",
        },
    }
    expected_checks = {
        "xhs_account_profile_snapshots": {
            "ck_xhs_profile_snapshot_canonical_id": _XHS_CANONICAL_NOTE_ID_CHECK,
            "ck_xhs_profile_snapshot_user_id": "length(user_id)between1and500",
            "ck_xhs_profile_snapshot_source_url": (
                "length(source_url)<=2000andsource_urlglob'https://*'"
            ),
            "ck_xhs_profile_snapshot_raw_digest": (
                "json_valid(raw_evidence)=1andjson_type(raw_evidence)='object'and"
                "raw_evidence_digest(raw_evidence)isnotnulland"
                "length(raw_digest)=64andraw_digestnotglob'*[^0-9a-f]*'and"
                "raw_evidence_digest(raw_evidence)=raw_digest"
            ),
        },
        "xhs_account_snapshot_notes": {
            "ck_xhs_snapshot_note_canonical_id": _XHS_CANONICAL_NOTE_ID_CHECK.replace(
                "idbetween", "note_record_idbetween"
            ),
            "ck_xhs_snapshot_note_position": "positionbetween0and1000",
        },
    }
    expected_fks = {
        "xhs_account_profile_snapshots": {
            (("user_id",), "xhs_account_profiles", ("user_id",), "RESTRICT"),
            (("collection_job_id",), "jobs", ("id",), "RESTRICT"),
            (("collection_artifact_id",), "job_artifacts", ("id",), "RESTRICT"),
        },
        "xhs_account_snapshot_notes": {
            (("note_record_id",), "xhs_account_notes", ("id",), "RESTRICT"),
            (("snapshot_id",), "xhs_account_profile_snapshots", ("id",), "RESTRICT"),
        },
    }
    expected_indexes = {
        "xhs_account_profile_snapshots": {
            "ix_xhs_profile_snapshots_user_version": (("user_id", "id"), False, ""),
        },
        "xhs_account_snapshot_notes": {
            "ix_xhs_snapshot_notes_snapshot_id": (("snapshot_id",), False, ""),
        },
    }
    expected_uniques = {
        "xhs_account_profile_snapshots": {
            ("collection_job_id",),
            ("collection_artifact_id",),
        },
        "xhs_account_snapshot_notes": {("snapshot_id", "position")},
    }
    expected_pks = {
        "xhs_account_profile_snapshots": ("id",),
        "xhs_account_snapshot_notes": ("note_record_id",),
    }
    try:
        if not _xhs_account_note_versioned_shape_valid(connection):
            return False
        if not set(expected_columns).issubset(inspector.get_table_names()):
            return False
        for table, expected in expected_columns.items():
            columns = {item["name"]: item for item in inspector.get_columns(table)}
            if set(columns) != set(expected):
                return False
            nullable = (
                {"nickname", "bio"}
                if table == "xhs_account_profile_snapshots"
                else set()
            )
            if any(
                columns[name].get("nullable") is not (name in nullable)
                for name in columns
            ):
                return False
            if {
                name: str(column.get("type") or "").upper()
                for name, column in columns.items()
            } != expected:
                return False
            checks = {
                item.get("name"): _compact_sql(item.get("sqltext"))
                for item in inspector.get_check_constraints(table)
            }
            if checks != expected_checks[table]:
                return False
            foreign_keys = {
                (
                    tuple(item.get("constrained_columns") or ()),
                    item.get("referred_table"),
                    tuple(item.get("referred_columns") or ()),
                    (item.get("options") or {}).get("ondelete"),
                )
                for item in inspector.get_foreign_keys(table)
            }
            if foreign_keys != expected_fks[table]:
                return False
            indexes = {
                item.get("name"): (
                    tuple(item.get("column_names") or ()),
                    bool(item.get("unique")),
                    _compact_sql(
                        (item.get("dialect_options") or {}).get("sqlite_where")
                    ),
                )
                for item in inspector.get_indexes(table)
            }
            if indexes != expected_indexes[table]:
                return False
            uniques = {
                tuple(item.get("column_names") or ())
                for item in inspector.get_unique_constraints(table)
            }
            if uniques != expected_uniques[table]:
                return False
            if tuple(
                inspector.get_pk_constraint(table).get("constrained_columns") or ()
            ) != expected_pks[table]:
                return False
        definitions = {
            **_XHS_ACCOUNT_SNAPSHOT_BINDING_TRIGGERS,
            **_XHS_ACCOUNT_SNAPSHOT_IMMUTABILITY_TRIGGERS,
        }
        names = ",".join(f"'{name}'" for name in definitions)
        actual_triggers = {
            name: _compact_sql(sql)
            for name, sql in connection.execute(text(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
                f"AND name IN ({names})"
            ))
        }
        return actual_triggers == {
            name: _compact_sql(sql) for name, sql in definitions.items()
        }
    except (KeyError, TypeError, AttributeError, SQLAlchemyError):
        return False


def _xhs_account_snapshot_data_valid(
    connection: Connection,
    *,
    runtime_dir: Path | None,
) -> bool:
    try:
        if not _xhs_account_note_evidence_data_valid(connection):
            return False
        invalid_snapshot = connection.scalar(text("""
            SELECT 1 FROM xhs_account_profile_snapshots AS snapshot
            LEFT JOIN xhs_account_profiles AS profile
              ON profile.user_id=snapshot.user_id
            LEFT JOIN job_artifacts AS artifact
              ON artifact.id=snapshot.collection_artifact_id
             AND artifact.job_id=snapshot.collection_job_id
            LEFT JOIN jobs AS job ON job.id=snapshot.collection_job_id
            WHERE profile.user_id IS NULL OR artifact.id IS NULL
               OR job.type!=:job_type
               OR artifact.kind!=:artifact_kind OR artifact.producer!=:producer
               OR json_valid(snapshot.raw_evidence)!=1
               OR json_type(snapshot.raw_evidence)!='object'
               OR raw_evidence_digest(snapshot.raw_evidence) IS NULL
               OR raw_evidence_digest(snapshot.raw_evidence) IS NOT snapshot.raw_digest
               OR datetime(snapshot.collected_at) IS NULL
            LIMIT 1
        """), {
            "job_type": ACCOUNT_COLLECTION_JOB_TYPE,
            "artifact_kind": ACCOUNT_COLLECTION_ARTIFACT_KIND,
            "producer": ACCOUNT_COLLECTION_ARTIFACT_PRODUCER,
        })
        if invalid_snapshot is not None:
            return False
        if connection.scalar(text("""
            SELECT 1 FROM xhs_account_profile_snapshots
            WHERE typeof(id)!='integer' OR id<1
               OR id>9223372036854775807
            LIMIT 1
        """)) is not None:
            return False
        if connection.scalar(text("""
            SELECT 1 FROM xhs_account_profiles AS profile
            WHERE NOT EXISTS (
                SELECT 1 FROM xhs_account_profile_snapshots AS snapshot
                WHERE snapshot.user_id=profile.user_id
            ) LIMIT 1
        """)) is not None:
            return False
        if connection.scalar(text("""
            SELECT 1 FROM xhs_account_notes AS note
            LEFT JOIN xhs_account_snapshot_notes AS member
              ON member.note_record_id=note.id
            LEFT JOIN xhs_account_profile_snapshots AS snapshot
              ON snapshot.id=member.snapshot_id
            WHERE member.note_record_id IS NULL OR snapshot.id IS NULL
               OR snapshot.user_id!=note.user_id
               OR snapshot.collection_job_id!=note.collection_job_id
               OR snapshot.collection_artifact_id!=note.collection_artifact_id
            LIMIT 1
        """)) is not None:
            return False
        if connection.scalar(text("""
            SELECT 1 FROM xhs_account_snapshot_notes AS member
            LEFT JOIN xhs_account_notes AS note
              ON note.id=member.note_record_id
            LEFT JOIN xhs_account_profile_snapshots AS snapshot
              ON snapshot.id=member.snapshot_id
            WHERE note.id IS NULL OR snapshot.id IS NULL
               OR typeof(member.note_record_id)!='integer'
               OR member.note_record_id<1
               OR member.note_record_id>9223372036854775807
               OR typeof(member.snapshot_id)!='integer'
               OR member.snapshot_id<1
               OR member.snapshot_id>9223372036854775807
               OR typeof(member.position)!='integer'
               OR member.position<0 OR member.position>1000
            LIMIT 1
        """)) is not None:
            return False
        if connection.scalar(text("""
            SELECT 1 FROM xhs_account_profile_snapshots AS snapshot
            LEFT JOIN xhs_account_snapshot_notes AS member
              ON member.snapshot_id=snapshot.id
            GROUP BY snapshot.id
            HAVING COUNT(member.note_record_id)>0 AND (
                MIN(member.position)!=0
                OR MAX(member.position)!=COUNT(member.note_record_id)-1
                OR COUNT(DISTINCT member.position)!=COUNT(member.note_record_id)
            ) LIMIT 1
        """)) is not None:
            return False
        if connection.scalar(text("""
            SELECT 1 FROM xhs_account_profiles AS profile
            JOIN xhs_account_profile_snapshots AS snapshot
              ON snapshot.user_id=profile.user_id
             AND snapshot.collection_job_id=profile.collection_job_id
             AND snapshot.collection_artifact_id=profile.collection_artifact_id
            WHERE profile.source_url IS NOT snapshot.source_url
               OR profile.nickname IS NOT snapshot.nickname
               OR profile.bio IS NOT snapshot.bio
               OR json(profile.public_stats_json)!=json(snapshot.public_stats_json)
               OR json(profile.raw_evidence)!=json(snapshot.raw_evidence)
               OR profile.raw_digest IS NOT snapshot.raw_digest
               OR profile.collected_at IS NOT snapshot.collected_at
            LIMIT 1
        """)) is not None:
            return False
        return _xhs_account_fact_content_data_valid(
            connection,
            runtime_dir=runtime_dir,
            versioned=True,
        )
    except SQLAlchemyError:
        return False


def _require_xhs_account_snapshot_precreate_state(connection: Connection) -> None:
    _require_no_xhs_account_snapshot_leftovers(connection)
    tables = set(inspect(connection).get_table_names())
    present = tables & {
        "xhs_account_profile_snapshots",
        "xhs_account_snapshot_notes",
    }
    if not present:
        return
    populated = any(
        connection.scalar(text(f"SELECT COUNT(*) FROM {table}"))
        for table in present
    )
    if populated and not (
        len(present) == 2
        and _xhs_account_snapshot_schema_valid(connection)
        and _xhs_account_snapshot_data_valid(connection, runtime_dir=None)
    ):
        raise SchemaMigrationError(
            "Ambiguous populated XHS account snapshot migration must fail closed."
        )


_ACCOUNT_NOTE_EVIDENCE_PREFIX = "account-note:"
_ACCOUNT_NOTE_EVIDENCE_ID = re.compile(r"^account-note:([1-9][0-9]*)$", re.ASCII)
_SQLITE_MAX_ROW_ID = 9_223_372_036_854_775_807
_SQLITE_MAX_ROW_ID_TEXT = str(_SQLITE_MAX_ROW_ID)
_XHS_CANONICAL_NOTE_ID_CHECK = "idbetween1and9223372036854775807"
_XHS_ACCOUNT_NOTE_IDENTITY_TEMPORARY_TABLES = frozenset(
    {
        "xhs_account_notes_identity_v1",
        "xhs_account_notes_canonical_id_v2",
    }
)
_SQLITE_ASCII_IDENTIFIER_CASE_MAP = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "abcdefghijklmnopqrstuvwxyz",
)


def _sqlite_ascii_identifier_key(value: str) -> str:
    """Match SQLite object names without folding non-ASCII code points."""

    return value.translate(_SQLITE_ASCII_IDENTIFIER_CASE_MAP)


def _xhs_account_note_identity_leftovers(
    connection: Connection,
) -> frozenset[str]:
    known_names = frozenset(
        _sqlite_ascii_identifier_key(name)
        for name in _XHS_ACCOUNT_NOTE_IDENTITY_TEMPORARY_TABLES
    )
    return frozenset(
        name
        for name in inspect(connection).get_table_names()
        if _sqlite_ascii_identifier_key(name) in known_names
    )


def _require_no_xhs_account_note_identity_leftovers(
    connection: Connection,
) -> None:
    if _xhs_account_note_identity_leftovers(connection):
        raise SchemaMigrationError(
            "Interrupted XHS account note identity or canonical id migration "
            "requires isolated manual migration."
        )


def _sqlite_schema_tokens(value: object) -> list[tuple[str, str]] | None:
    """Tokenize SQLite DDL while keeping code separate from quotes/comments."""

    if not isinstance(value, str):
        return None
    tokens: list[tuple[str, str]] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char.isspace():
            index += 1
            continue
        if value.startswith("--", index):
            newline = value.find("\n", index + 2)
            index = len(value) if newline < 0 else newline + 1
            continue
        if value.startswith("/*", index):
            end = value.find("*/", index + 2)
            if end < 0:
                return None
            index = end + 2
            continue
        if char == "'":
            index += 1
            while index < len(value):
                if value[index] != "'":
                    index += 1
                    continue
                index += 1
                if index < len(value) and value[index] == "'":
                    index += 1
                    continue
                tokens.append(("literal", ""))
                break
            else:
                return None
            continue
        if char in {'"', "`", "["}:
            delimiter = "]" if char == "[" else char
            index += 1
            rendered: list[str] = []
            while index < len(value):
                if value[index] != delimiter:
                    rendered.append(value[index])
                    index += 1
                    continue
                index += 1
                if index < len(value) and value[index] == delimiter:
                    rendered.append(delimiter)
                    index += 1
                    continue
                tokens.append(("identifier", "".join(rendered).casefold()))
                break
            else:
                return None
            continue
        if char in {"(", ")", ",", ".", ";"}:
            tokens.append(("symbol", char))
            index += 1
            continue
        start = index
        while index < len(value):
            if (
                value[index].isspace()
                or value[index] in "'\"`[](),.;"
                or value.startswith("--", index)
                or value.startswith("/*", index)
            ):
                break
            index += 1
        if start == index:
            tokens.append(("symbol", char))
            index += 1
        else:
            tokens.append(("word", value[start:index].casefold()))
    return tokens


def _sqlite_table_column_definitions(
    tokens: list[tuple[str, str]],
) -> list[list[tuple[str, str]]] | None:
    if len(tokens) < 4 or tokens[:2] != [("word", "create"), ("word", "table")]:
        return None
    try:
        body_start = tokens.index(("symbol", "("), 2)
    except ValueError:
        return None
    definitions: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    depth = 1
    for token in tokens[body_start + 1:]:
        if token == ("symbol", "("):
            depth += 1
            current.append(token)
            continue
        if token == ("symbol", ")"):
            depth -= 1
            if depth == 0:
                if current:
                    definitions.append(current)
                return definitions
            if depth < 0:
                return None
            current.append(token)
            continue
        if token == ("symbol", ",") and depth == 1:
            if not current:
                return None
            definitions.append(current)
            current = []
            continue
        current.append(token)
    return None


def _xhs_account_note_autoincrement_ddl_valid(connection: Connection) -> bool:
    try:
        table_sql = connection.scalar(text(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='xhs_account_notes'"
        ))
    except SQLAlchemyError:
        return False
    tokens = _sqlite_schema_tokens(table_sql)
    definitions = _sqlite_table_column_definitions(tokens or [])
    if definitions is None:
        return False
    for definition in definitions:
        if (
            len(definition) < 4
            or definition[0][0] not in {"word", "identifier"}
            or definition[0][1] != "id"
            or definition[1] != ("word", "integer")
        ):
            continue
        top_level_words: list[str] = []
        depth = 0
        for kind, token_value in definition[2:]:
            if (kind, token_value) == ("symbol", "("):
                depth += 1
            elif (kind, token_value) == ("symbol", ")"):
                depth -= 1
                if depth < 0:
                    return False
            elif depth == 0 and kind == "word":
                top_level_words.append(token_value)
        for index, word in enumerate(top_level_words[:-1]):
            if word == "primary" and top_level_words[index + 1] == "key":
                return "autoincrement" in top_level_words[index + 2:]
        return False
    return False


def _historical_account_note_id(value: str) -> int | None:
    if not value.startswith(_ACCOUNT_NOTE_EVIDENCE_PREFIX):
        return None
    match = _ACCOUNT_NOTE_EVIDENCE_ID.fullmatch(value)
    if match is None:
        raise SchemaMigrationError(
            "XHS account note reference history is invalid."
        )
    rendered = match.group(1)
    if len(rendered) > len(_SQLITE_MAX_ROW_ID_TEXT) or (
        len(rendered) == len(_SQLITE_MAX_ROW_ID_TEXT)
        and rendered > _SQLITE_MAX_ROW_ID_TEXT
    ):
        raise SchemaMigrationError(
            "XHS account note reference history is invalid."
        )
    return int(rendered)


def _xhs_account_note_reference_floor(connection: Connection) -> int:
    floor = connection.scalar(
        text("SELECT COALESCE(MAX(id), 0) FROM xhs_account_notes")
    )
    if isinstance(floor, bool) or not isinstance(floor, int) or floor < 0:
        raise SchemaMigrationError("XHS account note identity sequence is invalid.")
    tables = set(inspect(connection).get_table_names())
    for table in ("analyses", "opportunities"):
        if table not in tables:
            continue
        columns = {item["name"] for item in inspect(connection).get_columns(table)}
        if "evidence_ids_json" not in columns:
            continue
        for raw_value, in connection.execute(
            text(f"SELECT evidence_ids_json FROM {table}")
        ):
            try:
                values = (
                    json.loads(raw_value)
                    if isinstance(raw_value, str)
                    else raw_value
                )
            except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
                raise SchemaMigrationError(
                    "XHS account note reference history is invalid."
                ) from None
            if not isinstance(values, list) or any(
                not isinstance(value, str) for value in values
            ):
                raise SchemaMigrationError(
                    "XHS account note reference history is invalid."
                )
            for value in values:
                referenced_id = _historical_account_note_id(value)
                if referenced_id is not None:
                    floor = max(floor, referenced_id)
    return floor


def _require_xhs_account_note_identity_preconditions(
    connection: Connection,
) -> None:
    try:
        if (
            not _xhs_account_note_evidence_schema_valid(
                inspect(connection), connection
            )
            or not _xhs_account_note_evidence_data_valid(connection)
        ):
            raise SchemaMigrationError(
                "XHS account note identity requires a valid evidence schema."
            )
        _require_no_xhs_account_note_identity_leftovers(connection)
        _xhs_account_note_reference_floor(connection)
        if _xhs_account_note_autoincrement_ddl_valid(connection):
            rows = connection.execute(text(
                "SELECT seq FROM sqlite_sequence WHERE name='xhs_account_notes'"
            )).all()
            if len(rows) > 1:
                raise SchemaMigrationError(
                    "XHS account note identity sequence is ambiguous."
                )
            if rows and (
                isinstance(rows[0][0], bool)
                or not isinstance(rows[0][0], int)
                or not 0 <= rows[0][0] <= _SQLITE_MAX_ROW_ID
            ):
                raise SchemaMigrationError(
                    "XHS account note identity sequence is invalid."
                )
    except SchemaMigrationError:
        raise
    except (KeyError, TypeError, AttributeError, SQLAlchemyError, OverflowError):
        raise SchemaMigrationError(
            "XHS account note identity preflight failed."
        ) from None


def _xhs_account_note_canonical_id_check_valid(connection: Connection) -> bool:
    try:
        checks = {
            item.get("name"): _compact_sql(item.get("sqltext"))
            for item in inspect(connection).get_check_constraints(
                "xhs_account_notes"
            )
        }
        return (
            checks.get("ck_xhs_note_canonical_id")
            == _XHS_CANONICAL_NOTE_ID_CHECK
        )
    except (KeyError, TypeError, AttributeError, SQLAlchemyError):
        return False


def _xhs_account_note_rows_have_canonical_ids(connection: Connection) -> bool:
    try:
        return connection.scalar(text(
            "SELECT COUNT(*) FROM xhs_account_notes "
            "WHERE typeof(id) != 'integer' OR id < 1 "
            "OR id > 9223372036854775807"
        )) == 0
    except SQLAlchemyError:
        return False


def _xhs_account_note_canonical_id_schema_valid(connection: Connection) -> bool:
    try:
        return (
            _xhs_account_note_identity_schema_valid(connection)
            and _xhs_account_note_canonical_id_check_valid(connection)
            and _xhs_account_note_rows_have_canonical_ids(connection)
            and not _xhs_account_note_identity_leftovers(connection)
        )
    except (KeyError, TypeError, AttributeError, SQLAlchemyError):
        return False


def _require_xhs_account_note_canonical_id_preconditions(
    connection: Connection,
) -> int:
    try:
        _require_no_xhs_account_note_identity_leftovers(connection)
        if not _xhs_account_note_rows_have_canonical_ids(connection):
            raise SchemaMigrationError(
                "XHS account note canonical id data is invalid."
            )
        if not _xhs_account_note_identity_schema_valid(connection):
            raise SchemaMigrationError(
                "XHS account note canonical id requires a valid identity schema."
            )
        rows = connection.execute(text(
            "SELECT seq FROM sqlite_sequence WHERE name='xhs_account_notes'"
        )).all()
        if len(rows) != 1:
            raise SchemaMigrationError(
                "XHS account note canonical id sequence is invalid."
            )
        sequence = rows[0][0]
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or not 0 <= sequence <= _SQLITE_MAX_ROW_ID
        ):
            raise SchemaMigrationError(
                "XHS account note canonical id sequence is invalid."
            )
        return sequence
    except SchemaMigrationError:
        raise
    except (KeyError, TypeError, AttributeError, SQLAlchemyError, OverflowError):
        raise SchemaMigrationError(
            "XHS account note canonical id preflight failed."
        ) from None


def _advance_xhs_account_note_identity_sequence(
    connection: Connection,
    *,
    minimum: int = 0,
) -> None:
    if not _xhs_account_note_autoincrement_ddl_valid(connection):
        raise SchemaMigrationError("XHS account note identity DDL is invalid.")
    if (
        isinstance(minimum, bool)
        or not isinstance(minimum, int)
        or not 0 <= minimum <= _SQLITE_MAX_ROW_ID
    ):
        raise SchemaMigrationError("XHS account note identity sequence is invalid.")
    floor = max(_xhs_account_note_reference_floor(connection), minimum)
    rows = connection.execute(text(
        "SELECT rowid, seq FROM sqlite_sequence WHERE name='xhs_account_notes'"
    )).all()
    if len(rows) > 1:
        raise SchemaMigrationError("XHS account note identity sequence is ambiguous.")
    if not rows:
        connection.execute(
            text(
                "INSERT INTO sqlite_sequence(name, seq) "
                "VALUES ('xhs_account_notes', :seq)"
            ),
            {"seq": floor},
        )
        return
    sequence = rows[0][1]
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise SchemaMigrationError("XHS account note identity sequence is invalid.")
    if sequence < floor:
        connection.execute(
            text("UPDATE sqlite_sequence SET seq=:seq WHERE rowid=:rowid"),
            {"seq": floor, "rowid": rows[0][0]},
        )


def _xhs_account_note_identity_schema_valid(connection: Connection) -> bool:
    try:
        if (
            _xhs_account_note_identity_leftovers(connection)
            or not _xhs_account_note_evidence_schema_valid(
                inspect(connection), connection
            )
            or not _xhs_account_note_evidence_data_valid(connection)
            or not _xhs_account_note_autoincrement_ddl_valid(connection)
            or connection.scalar(text(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                "AND name='sqlite_sequence'"
            )) != 1
        ):
            return False
        rows = connection.execute(text(
            "SELECT seq FROM sqlite_sequence WHERE name='xhs_account_notes'"
        )).all()
        if len(rows) != 1:
            return False
        sequence = rows[0][0]
        return (
            not isinstance(sequence, bool)
            and isinstance(sequence, int)
            and sequence >= _xhs_account_note_reference_floor(connection)
        )
    except (KeyError, TypeError, AttributeError, SQLAlchemyError, SchemaMigrationError):
        return False


def _rebuild_xhs_account_notes_with_permanent_ids(
    connection: Connection,
    note_record: object,
    *,
    temporary_table: str = "xhs_account_notes_identity_v1",
) -> None:
    _require_no_xhs_account_note_identity_leftovers(connection)
    for name in (
        "ck_xhs_note_artifact_job_insert",
        "ck_xhs_note_artifact_job_update",
    ):
        connection.execute(text(f"DROP TRIGGER IF EXISTS {name}"))
    explicit_indexes = connection.execute(text(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='xhs_account_notes' AND sql IS NOT NULL"
    )).all()
    for name, in explicit_indexes:
        connection.execute(text(f'DROP INDEX "{name}"'))
    connection.execute(text(
        f"ALTER TABLE xhs_account_notes RENAME TO {temporary_table}"
    ))
    note_record.__table__.create(connection)
    columns = [column.name for column in note_record.__table__.columns]
    rendered_columns = ", ".join(f'"{column}"' for column in columns)
    connection.execute(text(
        f"INSERT INTO xhs_account_notes ({rendered_columns}) "
        f"SELECT {rendered_columns} FROM {temporary_table}"
    ))
    connection.execute(text(f"DROP TABLE {temporary_table}"))
    _create_xhs_account_note_evidence_triggers(connection)


def _require_no_xhs_artifact_promotion_leftovers(connection: Connection) -> None:
    tables = {
        str(name).casefold()
        for name in inspect(connection).get_table_names()
    }
    leftovers = tables & {
        name.casefold() for name in _XHS_ARTIFACT_PROMOTION_LEFTOVERS
    }
    if leftovers:
        raise SchemaMigrationError(
            "Incomplete XHS artifact promotion journal migration requires "
            "isolated manual recovery."
        )


def _rebuild_xhs_artifact_promotion_journal_v2(
    connection: Connection,
    journal_record: object,
) -> None:
    """Mechanically add allocation identity and recovery lease fields to v1."""

    temporary_table = "xhs_artifact_promotion_journal_v1_rebuild"
    _require_no_xhs_artifact_promotion_leftovers(connection)
    for name in _XHS_ARTIFACT_JOURNAL_V1_TRIGGERS:
        connection.execute(text(f"DROP TRIGGER {name}"))
    explicit_indexes = connection.execute(text(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='xhs_artifact_promotion_journal' AND sql IS NOT NULL"
    )).all()
    for name, in explicit_indexes:
        connection.execute(text(f'DROP INDEX "{name}"'))
    connection.execute(text(
        "ALTER TABLE xhs_artifact_promotion_journal "
        f"RENAME TO {temporary_table}"
    ))
    journal_record.__table__.create(connection)
    legacy_columns = (
        "id", "job_id", "artifact_kind", "producer", "stage_path",
        "final_path", "sha256", "size_bytes", "file_dev", "file_ino",
        "file_mtime_ns", "target_state", "state", "resolution",
        "artifact_id", "created_at", "updated_at", "completed_at",
    )
    rendered = ", ".join(f'"{column}"' for column in legacy_columns)
    connection.execute(text(
        "INSERT INTO xhs_artifact_promotion_journal "
        f"({rendered}, owner_token, recovery_lease_expires_at) "
        f"SELECT {rendered}, NULL, NULL FROM {temporary_table}"
    ))
    connection.execute(text(f"DROP TABLE {temporary_table}"))
    _create_xhs_artifact_promotion_journal_triggers(connection)


def _create_xhs_artifact_promotion_journal_triggers(
    connection: Connection,
) -> None:
    for name, definition in _XHS_ARTIFACT_JOURNAL_TRIGGERS.items():
        connection.execute(text(f"DROP TRIGGER IF EXISTS {name}"))
        connection.execute(text(definition))


def _xhs_artifact_promotion_journal_v1_schema_valid(
    inspector: object,
    connection: Connection,
) -> bool:
    """Recognize the one published v1 layout before its mechanical v2 rebuild."""

    table = "xhs_artifact_promotion_journal"
    expected_types = {
        "id": "VARCHAR(36)",
        "job_id": "VARCHAR(36)",
        "artifact_kind": "VARCHAR(100)",
        "producer": "VARCHAR(64)",
        "stage_path": "TEXT",
        "final_path": "TEXT",
        "sha256": "VARCHAR(64)",
        "size_bytes": "INTEGER",
        "file_dev": "INTEGER",
        "file_ino": "INTEGER",
        "file_mtime_ns": "INTEGER",
        "target_state": "VARCHAR(32)",
        "state": "VARCHAR(20)",
        "resolution": "VARCHAR(20)",
        "artifact_id": "INTEGER",
        "created_at": "DATETIME",
        "updated_at": "DATETIME",
        "completed_at": "DATETIME",
    }
    nullable = {"resolution", "artifact_id", "completed_at"}
    expected_checks = {
        "ck_xhs_artifact_journal_uuid": (
            "is_canonical_uuid(id)=1andis_canonical_uuid(job_id)=1"
        ),
        "ck_xhs_artifact_journal_source": (
            "length(artifact_kind)between1and100andlength(producer)between1and64"
        ),
        "ck_xhs_artifact_journal_stage_path": (
            "artifact_path_key(stage_path)isnotnullandlength(stage_path)<=1000and"
            "stage_pathlike'evidence/xhs/.staging/%.stage'and"
            "stage_pathnotlike'evidence/xhs/.staging/%/%'andstage_pathin("
            "'evidence/xhs/.staging/'||job_id||'-'||replace(id,'-','')||'.stage',"
            "'evidence/xhs/.staging/'||job_id||'-'||replace(id,'-','')||'-failure.stage')"
        ),
        "ck_xhs_artifact_journal_final_path": (
            "artifact_path_key(final_path)isnotnullandlength(final_path)<=1000and"
            "final_pathlike'evidence/xhs/%.json'and"
            "final_pathnotlike'evidence/xhs/%/%'and((final_path="
            "'evidence/xhs/'||job_id||'.json'andstage_pathnotlike'%-failure.stage')or("
            "final_path='evidence/xhs/'||job_id||'-failure.json'and"
            "stage_pathlike'%-failure.stage'))"
        ),
        "ck_xhs_artifact_journal_sha": (
            "length(sha256)=64andsha256notglob'*[^0-9a-f]*'"
        ),
        "ck_xhs_artifact_journal_identity": (
            "size_bytesbetween0and20971520andfile_dev>=0andfile_ino>=0and"
            "file_mtime_ns>=0"
        ),
        "ck_xhs_artifact_journal_state": (
            "target_statein('succeeded','failed','needs_human')and"
            "statein('prepared','promoted','completed')"
        ),
        "ck_xhs_artifact_journal_resolution": (
            "((state!='completed'andresolutionisnullandartifact_idisnulland"
            "completed_atisnull)or(state='completed'andresolutionin"
            "('committed','rolled_back','inconsistent')andcompleted_atisnotnulland"
            "(resolution!='committed'orartifact_idisnotnull)and"
            "(resolution!='rolled_back'orartifact_idisnull)))"
        ),
        "ck_xhs_artifact_journal_timestamps": (
            "datetime(created_at)isnotnullanddatetime(updated_at)isnotnulland"
            "(completed_atisnullordatetime(completed_at)isnotnull)"
        ),
    }
    try:
        columns = {
            column["name"]: column for column in inspector.get_columns(table)
        }
        if set(columns) != set(expected_types) or {
            name: str(column.get("type") or "").upper()
            for name, column in columns.items()
        } != expected_types:
            return False
        if any(
            columns[name].get("nullable") is not (name in nullable)
            for name in columns
        ):
            return False
        if tuple(
            inspector.get_pk_constraint(table).get("constrained_columns") or ()
        ) != ("id",):
            return False
        if {
            tuple(item.get("column_names") or ())
            for item in inspector.get_unique_constraints(table)
        } != {("job_id",), ("stage_path",), ("final_path",)}:
            return False
        if {
            (
                tuple(item.get("constrained_columns") or ()),
                item.get("referred_table"),
                tuple(item.get("referred_columns") or ()),
                (item.get("options") or {}).get("ondelete"),
            )
            for item in inspector.get_foreign_keys(table)
        } != {
            (("job_id",), "jobs", ("id",), "RESTRICT"),
            (("artifact_id",), "job_artifacts", ("id",), "RESTRICT"),
        }:
            return False
        if {
            item.get("name"): (
                tuple(item.get("column_names") or ()),
                bool(item.get("unique")),
            )
            for item in inspector.get_indexes(table)
        } != {
            "ix_xhs_artifact_journal_state": (("state",), False),
            "ix_xhs_artifact_journal_artifact_id": (("artifact_id",), False),
        }:
            return False
        if {
            item.get("name"): _compact_sql(item.get("sqltext"))
            for item in inspector.get_check_constraints(table)
        } != expected_checks:
            return False
        triggers = {
            row[0]: _compact_sql(row[1])
            for row in connection.execute(text(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'ck_xhs_artifact_journal_%'"
            ))
        }
        return triggers == {
            name: _compact_sql(definition)
            for name, definition in _XHS_ARTIFACT_JOURNAL_V1_TRIGGERS.items()
        }
    except (KeyError, TypeError, AttributeError, SQLAlchemyError):
        return False


def _xhs_artifact_promotion_journal_schema_valid(
    inspector: object,
    connection: Connection,
) -> bool:
    """Validate the journal's complete physical schema, not only its marker."""

    table = "xhs_artifact_promotion_journal"
    expected_types = {
        "id": "VARCHAR(36)",
        "job_id": "VARCHAR(36)",
        "artifact_kind": "VARCHAR(100)",
        "producer": "VARCHAR(64)",
        "stage_path": "TEXT",
        "final_path": "TEXT",
        "sha256": "VARCHAR(64)",
        "size_bytes": "INTEGER",
        "file_dev": "INTEGER",
        "file_ino": "INTEGER",
        "file_mtime_ns": "INTEGER",
        "target_state": "VARCHAR(32)",
        "state": "VARCHAR(20)",
        "resolution": "VARCHAR(20)",
        "artifact_id": "INTEGER",
        "created_at": "DATETIME",
        "updated_at": "DATETIME",
        "completed_at": "DATETIME",
        "owner_token": "VARCHAR(36)",
        "recovery_lease_expires_at": "DATETIME",
    }
    nullable = {
        "file_dev",
        "file_ino",
        "file_mtime_ns",
        "resolution",
        "artifact_id",
        "completed_at",
        "owner_token",
        "recovery_lease_expires_at",
    }
    expected_checks = {
        "ck_xhs_artifact_journal_uuid": (
            "is_canonical_uuid(id)=1andis_canonical_uuid(job_id)=1"
        ),
        "ck_xhs_artifact_journal_source": (
            "length(artifact_kind)between1and100andlength(producer)between1and64"
        ),
        "ck_xhs_artifact_journal_stage_path": (
            "artifact_path_key(stage_path)isnotnullandlength(stage_path)<=1000and"
            "stage_pathlike'evidence/xhs/.staging/%.stage'and"
            "stage_pathnotlike'evidence/xhs/.staging/%/%'andstage_pathin("
            "'evidence/xhs/.staging/'||job_id||'-'||replace(id,'-','')||'.stage',"
            "'evidence/xhs/.staging/'||job_id||'-'||replace(id,'-','')||'-failure.stage')"
        ),
        "ck_xhs_artifact_journal_final_path": (
            "artifact_path_key(final_path)isnotnullandlength(final_path)<=1000and"
            "final_pathlike'evidence/xhs/%.json'and"
            "final_pathnotlike'evidence/xhs/%/%'and((final_path="
            "'evidence/xhs/'||job_id||'.json'andstage_pathnotlike'%-failure.stage')or("
            "final_path='evidence/xhs/'||job_id||'-failure.json'and"
            "stage_pathlike'%-failure.stage'))"
        ),
        "ck_xhs_artifact_journal_sha": (
            "length(sha256)=64andsha256notglob'*[^0-9a-f]*'"
        ),
        "ck_xhs_artifact_journal_identity": (
            "size_bytesbetween0and20971520and((file_devisnulland"
            "file_inoisnullandfile_mtime_nsisnulland(state='allocating'or("
            "state='completed'andresolutionin('rolled_back','inconsistent'))))"
            "or(file_devisnotnullandfile_inoisnotnulland"
            "file_mtime_nsisnotnullandfile_dev>=0andfile_ino>=0and"
            "file_mtime_ns>=0andstate!='allocating'))"
        ),
        "ck_xhs_artifact_journal_state": (
            "target_statein('succeeded','failed','needs_human')and"
            "statein('allocating','prepared','promoted','completed')"
        ),
        "ck_xhs_artifact_journal_resolution": (
            "((state!='completed'andresolutionisnullandartifact_idisnulland"
            "completed_atisnull)or(state='completed'andresolutionin"
            "('committed','rolled_back','inconsistent')andcompleted_atisnotnulland"
            "(resolution!='committed'orartifact_idisnotnull)and"
            "(resolution!='rolled_back'orartifact_idisnull)))"
        ),
        "ck_xhs_artifact_journal_timestamps": (
            "datetime(created_at)isnotnullanddatetime(updated_at)isnotnulland"
            "(completed_atisnullordatetime(completed_at)isnotnull)"
        ),
        "ck_xhs_artifact_journal_owner_lease": (
            "((owner_tokenisnullandrecovery_lease_expires_atisnull)or("
            "is_canonical_uuid(owner_token)=1and"
            "datetime(recovery_lease_expires_at)isnotnull))"
        ),
    }
    try:
        if table not in set(inspector.get_table_names()):
            return False
        columns = {
            column["name"]: column
            for column in inspector.get_columns(table)
        }
        if set(columns) != set(expected_types):
            return False
        if {
            name: str(column.get("type") or "").upper()
            for name, column in columns.items()
        } != expected_types:
            return False
        if any(
            columns[name].get("nullable") is not (name in nullable)
            for name in columns
        ):
            return False
        if tuple(
            inspector.get_pk_constraint(table).get("constrained_columns") or ()
        ) != ("id",):
            return False
        if {
            tuple(item.get("column_names") or ())
            for item in inspector.get_unique_constraints(table)
        } != {("job_id",), ("stage_path",), ("final_path",)}:
            return False
        if {
            (
                tuple(item.get("constrained_columns") or ()),
                item.get("referred_table"),
                tuple(item.get("referred_columns") or ()),
                (item.get("options") or {}).get("ondelete"),
            )
            for item in inspector.get_foreign_keys(table)
        } != {
            (("job_id",), "jobs", ("id",), "RESTRICT"),
            (("artifact_id",), "job_artifacts", ("id",), "RESTRICT"),
        }:
            return False
        if {
            item.get("name"): (
                tuple(item.get("column_names") or ()),
                bool(item.get("unique")),
            )
            for item in inspector.get_indexes(table)
        } != {
            "ix_xhs_artifact_journal_state": (("state",), False),
            "ix_xhs_artifact_journal_artifact_id": (("artifact_id",), False),
            "ix_xhs_artifact_journal_recovery_lease": (
                ("recovery_lease_expires_at", "state"),
                False,
            ),
        }:
            return False
        if {
            item.get("name"): _compact_sql(item.get("sqltext"))
            for item in inspector.get_check_constraints(table)
        } != expected_checks:
            return False
        triggers = {
            row[0]: _compact_sql(row[1])
            for row in connection.execute(text(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'ck_xhs_artifact_journal_%'"
            ))
        }
        return triggers == {
            name: _compact_sql(definition)
            for name, definition in _XHS_ARTIFACT_JOURNAL_TRIGGERS.items()
        }
    except (KeyError, TypeError, AttributeError, SQLAlchemyError):
        return False


def _xhs_artifact_promotion_journal_data_valid(connection: Connection) -> bool:
    """Reject rows whose job, artifact, path or durable state bindings disagree."""

    try:
        violation = connection.scalar(text(f"""
            SELECT 1 FROM xhs_artifact_promotion_journal AS journal
            LEFT JOIN jobs AS job ON job.id=journal.job_id
            LEFT JOIN job_artifacts AS artifact ON artifact.id=journal.artifact_id
            WHERE job.id IS NULL
            OR job.type NOT IN ('xhs_account_collection','xhs_note_search')
            OR journal.producer IS NOT 'xhs_cli_read_worker_v1'
            OR journal.artifact_kind IS NOT CASE job.type
                WHEN 'xhs_account_collection' THEN 'xhs_account_collection_raw'
                ELSE 'xhs_note_search_raw' END
            OR journal.stage_path NOT LIKE
                ('evidence/xhs/.staging/' || journal.job_id || '-%.stage')
            OR journal.final_path NOT IN
                ('evidence/xhs/' || journal.job_id || '.json',
                 'evidence/xhs/' || journal.job_id || '-failure.json')
            OR (journal.artifact_id IS NOT NULL AND (
                artifact.id IS NULL OR NOT ({_XHS_ARTIFACT_METADATA_BINDING})
                OR artifact.job_id IS NOT journal.job_id
                OR artifact.kind IS NOT journal.artifact_kind
                OR artifact.producer IS NOT journal.producer
                OR artifact.path IS NOT journal.final_path
            ))
            OR (journal.state='completed' AND journal.resolution='committed' AND (
                artifact.id IS NULL OR job.state IS NOT journal.target_state
            ))
            OR (journal.state!='completed' AND journal.artifact_id IS NOT NULL)
            OR ((journal.file_dev IS NULL) + (journal.file_ino IS NULL)
                + (journal.file_mtime_ns IS NULL)) NOT IN (0, 3)
            OR (journal.state='allocating' AND journal.file_dev IS NOT NULL)
            OR (journal.state IN ('prepared','promoted')
                AND journal.file_dev IS NULL)
            OR (journal.state='completed' AND journal.resolution='committed'
                AND journal.file_dev IS NULL)
            OR (journal.file_dev IS NOT NULL AND (
                journal.file_dev < 0 OR journal.file_ino < 0
                OR journal.file_mtime_ns < 0
            ))
            LIMIT 1
        """))
        if violation is not None:
            return False
        columns = {
            column["name"]
            for column in inspect(connection).get_columns(
                "xhs_artifact_promotion_journal"
            )
        }
        if {
            "owner_token",
            "recovery_lease_expires_at",
        }.issubset(columns):
            return connection.scalar(text("""
                SELECT 1 FROM xhs_artifact_promotion_journal
                WHERE (owner_token IS NULL) IS NOT
                    (recovery_lease_expires_at IS NULL)
                OR (owner_token IS NOT NULL AND (
                    is_canonical_uuid(owner_token) IS NOT 1
                    OR datetime(recovery_lease_expires_at) IS NULL
                ))
                LIMIT 1
            """)) is None
        return True
    except SQLAlchemyError:
        return False


def _xhs_account_note_evidence_schema_valid(
    inspector: object, connection: Connection
) -> bool:
    """Verify every durable XHS fact constraint instead of trusting its marker."""

    expected_columns = {
        "xhs_account_profiles": {
            "user_id": "VARCHAR(500)",
            "source_url": "TEXT",
            "nickname": "TEXT",
            "bio": "TEXT",
            "public_stats_json": "JSON",
            "raw_evidence": "JSON",
            "raw_digest": "VARCHAR(64)",
            "collection_job_id": "VARCHAR(36)",
            "collection_artifact_id": "INTEGER",
            "collected_at": "DATETIME",
        },
        "xhs_account_notes": {
            "id": "INTEGER",
            "note_id": "VARCHAR(500)",
            "user_id": "VARCHAR(500)",
            "source_url": "TEXT",
            "title": "TEXT",
            "summary": "TEXT",
            "published_at": "VARCHAR(100)",
            "public_interactions_json": "JSON",
            "raw_evidence": "JSON",
            "raw_digest": "VARCHAR(64)",
            "collection_job_id": "VARCHAR(36)",
            "collection_artifact_id": "INTEGER",
            "collected_at": "DATETIME",
        },
    }
    expected_checks = {
        "xhs_account_profiles": {
            "ck_xhs_profile_user_id": "length(user_id)between1and500",
            "ck_xhs_profile_source_url": "length(source_url)<=2000andsource_urlglob'https://*'",
            "ck_xhs_profile_raw_digest": (
                "json_valid(raw_evidence)=1andjson_type(raw_evidence)='object'and"
                "raw_evidence_digest(raw_evidence)isnotnulland"
                "length(raw_digest)=64andraw_digestnotglob'*[^0-9a-f]*'and"
                "raw_evidence_digest(raw_evidence)=raw_digest"
            ),
        },
        "xhs_account_notes": {
            "ck_xhs_note_id": "length(note_id)between1and500",
            "ck_xhs_note_source_url": "length(source_url)<=2000andsource_urlglob'https://*'",
            "ck_xhs_note_raw_digest": (
                "json_valid(raw_evidence)=1andjson_type(raw_evidence)='object'and"
                "raw_evidence_digest(raw_evidence)isnotnulland"
                "length(raw_digest)=64andraw_digestnotglob'*[^0-9a-f]*'and"
                "raw_evidence_digest(raw_evidence)=raw_digest"
            ),
        },
    }
    expected_fks = {
        "xhs_account_profiles": {
            (("collection_job_id",), "jobs", ("id",), "RESTRICT"),
            (("collection_artifact_id",), "job_artifacts", ("id",), "RESTRICT"),
        },
        "xhs_account_notes": {
            (("user_id",), "xhs_account_profiles", ("user_id",), "CASCADE"),
            (("collection_job_id",), "jobs", ("id",), "RESTRICT"),
            (("collection_artifact_id",), "job_artifacts", ("id",), "RESTRICT"),
        },
    }
    expected_indexes = {
        "xhs_account_profiles": {
            "ix_xhs_account_profiles_collection_job_id": (("collection_job_id",), False, ""),
            "ix_xhs_account_profiles_collection_artifact_id": (("collection_artifact_id",), False, ""),
        },
        "xhs_account_notes": {
            "ix_xhs_account_notes_user_id": (("user_id",), False, ""),
            "ix_xhs_account_notes_collection_job_id": (("collection_job_id",), False, ""),
            "ix_xhs_account_notes_collection_artifact_id": (("collection_artifact_id",), False, ""),
        },
    }
    try:
        if not set(expected_columns).issubset(set(inspector.get_table_names())):
            return False
        for table, expected in expected_columns.items():
            columns = {item["name"]: item for item in inspector.get_columns(table)}
            if set(columns) != set(expected):
                return False
            nullable_columns = {
                "xhs_account_profiles": {"nickname", "bio"},
                "xhs_account_notes": {"title", "summary", "published_at"},
            }[table]
            if any(
                columns[name].get("nullable") is not False
                for name in set(expected) - nullable_columns
            ) or any(
                columns[name].get("nullable") is not True for name in nullable_columns
            ):
                return False
            if {
                name: str(column.get("type") or "").upper()
                for name, column in columns.items()
            } != expected:
                return False
            checks = {
                item.get("name"): _compact_sql(item.get("sqltext"))
                for item in inspector.get_check_constraints(table)
            }
            allowed_checks = [expected_checks[table]]
            if table == "xhs_account_notes":
                allowed_checks.append(
                    {
                        **expected_checks[table],
                        "ck_xhs_note_canonical_id": _XHS_CANONICAL_NOTE_ID_CHECK,
                    }
                )
            if checks not in allowed_checks:
                return False
            foreign_keys = {
                (
                    tuple(item.get("constrained_columns") or ()),
                    item.get("referred_table"),
                    tuple(item.get("referred_columns") or ()),
                    (item.get("options") or {}).get("ondelete"),
                )
                for item in inspector.get_foreign_keys(table)
            }
            allowed_foreign_keys = [expected_fks[table]]
            if table == "xhs_account_notes":
                allowed_foreign_keys.append({
                    (("user_id",), "xhs_account_profiles", ("user_id",), "RESTRICT"),
                    (("collection_job_id",), "jobs", ("id",), "RESTRICT"),
                    (("collection_artifact_id",), "job_artifacts", ("id",), "RESTRICT"),
                })
            if foreign_keys not in allowed_foreign_keys:
                return False
            indexes = {
                item.get("name"): (
                    tuple(item.get("column_names") or ()),
                    bool(item.get("unique")),
                    _compact_sql((item.get("dialect_options") or {}).get("sqlite_where")),
                )
                for item in inspector.get_indexes(table)
            }
            if indexes != expected_indexes[table]:
                return False
        if tuple(inspector.get_pk_constraint("xhs_account_profiles").get("constrained_columns") or ()) != ("user_id",):
            return False
        if tuple(inspector.get_pk_constraint("xhs_account_notes").get("constrained_columns") or ()) != ("id",):
            return False
        if {
            tuple(item.get("column_names") or ())
            for item in inspector.get_unique_constraints("xhs_account_profiles")
        } != set():
            return False
        if {
            tuple(item.get("column_names") or ())
            for item in inspector.get_unique_constraints("xhs_account_notes")
        } not in (
            {("note_id", "user_id")},
            {("note_id", "user_id", "collection_job_id")},
        ):
            return False
        evidence_trigger_names = ",".join(
            f"'{name}'" for name in _XHS_ACCOUNT_NOTE_EVIDENCE_TRIGGER_SQL
        )
        triggers = {
            row[0]: _compact_sql(row[1])
            for row in connection.execute(text(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name IN ('xhs_account_profiles','xhs_account_notes') "
                f"AND name IN ({evidence_trigger_names})"
            ))
        }
        if triggers != {
            name: _compact_sql(sql)
            for name, sql in _XHS_ACCOUNT_NOTE_EVIDENCE_TRIGGER_SQL.items()
        }:
            return False
    except (KeyError, TypeError, AttributeError, SQLAlchemyError):
        return False
    return True


def _xhs_account_note_evidence_data_valid(connection: Connection) -> bool:
    try:
        for table in ("xhs_account_profiles", "xhs_account_notes"):
            invalid = connection.scalar(text(
                f"SELECT 1 FROM {table} AS fact "
                "LEFT JOIN job_artifacts AS artifact "
                "ON artifact.id=fact.collection_artifact_id "
                "AND artifact.job_id=fact.collection_job_id "
                "LEFT JOIN jobs AS job ON job.id=artifact.job_id "
                "WHERE artifact.id IS NULL OR job.type!=:job_type "
                "OR artifact.kind!=:artifact_kind OR artifact.producer!=:producer "
                "OR json_valid(fact.raw_evidence)!=1 "
                "OR json_type(fact.raw_evidence)!='object' "
                "OR raw_evidence_digest(fact.raw_evidence) IS NULL "
                "OR raw_evidence_digest(fact.raw_evidence) IS NOT fact.raw_digest "
                "OR datetime(fact.collected_at) IS NULL LIMIT 1"
            ), {
                "job_type": ACCOUNT_COLLECTION_JOB_TYPE,
                "artifact_kind": ACCOUNT_COLLECTION_ARTIFACT_KIND,
                "producer": ACCOUNT_COLLECTION_ARTIFACT_PRODUCER,
            })
            if invalid is not None:
                return False
        return connection.scalar(text(
            "SELECT 1 FROM xhs_account_notes AS note "
            "LEFT JOIN xhs_account_profiles AS profile ON profile.user_id=note.user_id "
            "WHERE profile.user_id IS NULL LIMIT 1"
        )) is None
    except SQLAlchemyError:
        return False


def _xhs_account_fact_immutability_triggers_valid(
    connection: Connection,
) -> bool:
    names = ",".join(
        f"'{name}'" for name in _XHS_ACCOUNT_FACT_IMMUTABILITY_TRIGGERS
    )
    try:
        rows = connection.execute(text(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
            f"AND name IN ({names})"
        )).all()
        actual = {name: _compact_sql(sql) for name, sql in rows}
        expected = {
            name: _compact_sql(definition)
            for name, definition in _XHS_ACCOUNT_FACT_IMMUTABILITY_TRIGGERS.items()
        }
        return actual == expected
    except SQLAlchemyError:
        return False


def _xhs_account_fact_content_data_valid(
    connection: Connection,
    *,
    runtime_dir: Path | None,
    versioned: bool = False,
) -> bool:
    """Compare normalized values with their exact readable journal-owned bytes."""

    try:
        profile_table = (
            "xhs_account_profile_snapshots"
            if versioned
            else "xhs_account_profiles"
        )
        profiles = connection.execute(text(
            "SELECT user_id, source_url, nickname, bio, public_stats_json, "
            "raw_evidence, raw_digest, collection_job_id, "
            "collection_artifact_id, collected_at "
            f"FROM {profile_table} ORDER BY user_id, collection_artifact_id"
        )).mappings().all()
    except SQLAlchemyError:
        return False
    if not profiles or runtime_dir is None:
        return True

    from backend.app.adapters.contracts import CollectionResult
    from backend.app.features.xhs.schemas import (
        AccountEvidenceBinding,
        AccountEvidencePersistenceError,
        NOTE_PUBLIC_COUNTER_FIELDS,
        PROFILE_PUBLIC_COUNTER_FIELDS,
        normalize_exact_account_result,
        public_counter_json_matches,
    )
    from backend.app.features.xhs.staging_cleanup import (
        TrustedXhsArtifactStore,
        UnsafeXhsArtifactStore,
        XhsArtifactIdentity,
    )

    try:
        with TrustedXhsArtifactStore(
            runtime_dir,
            max_bytes=20 * 1024 * 1024,
        ) as store:
            for profile in profiles:
                provenance = connection.execute(text(
                    "SELECT journal.stage_path, journal.final_path, "
                    "journal.sha256, journal.size_bytes, journal.file_dev, "
                    "journal.file_ino, journal.file_mtime_ns, "
                    "artifact.id AS artifact_id, artifact.job_id, artifact.kind, "
                    "artifact.producer, artifact.path, artifact.metadata_json, "
                    "job.type AS job_type, job.state AS job_state, "
                    "job.input_data "
                    "FROM xhs_artifact_promotion_journal AS journal "
                    "JOIN job_artifacts AS artifact ON artifact.id=journal.artifact_id "
                    "JOIN jobs AS job ON job.id=journal.job_id "
                    "WHERE journal.job_id=:job_id "
                    "AND journal.artifact_id=:artifact_id "
                    "AND journal.state='completed' "
                    "AND journal.resolution='committed'"
                ), {
                    "job_id": profile["collection_job_id"],
                    "artifact_id": profile["collection_artifact_id"],
                }).mappings().all()
                if len(provenance) != 1:
                    job_state = connection.scalar(text(
                        "SELECT state FROM jobs WHERE id=:job_id"
                    ), {"job_id": profile["collection_job_id"]})
                    # Recovery-invalidated snapshots are already unavailable
                    # to reads; their missing formal bytes remain owned by the
                    # artifact recovery state machine, not this content scan.
                    if job_state != "succeeded":
                        continue
                    return False
                bound = provenance[0]
                if (
                    bound["job_type"] != ACCOUNT_COLLECTION_JOB_TYPE
                    or bound["job_state"] != "succeeded"
                    or bound["kind"] != ACCOUNT_COLLECTION_ARTIFACT_KIND
                    or bound["producer"] != ACCOUNT_COLLECTION_ARTIFACT_PRODUCER
                    or bound["path"] != bound["final_path"]
                    or bound["file_dev"] is None
                    or bound["file_ino"] is None
                    or bound["file_mtime_ns"] is None
                ):
                    return False
                identity = XhsArtifactIdentity(
                    int(bound["file_dev"]),
                    int(bound["file_ino"]),
                    int(bound["size_bytes"]),
                    int(bound["file_mtime_ns"]),
                )
                encoded: bytes | None = None
                try:
                    encoded = store.read_final(
                        Path(bound["final_path"]).name,
                        identity,
                    )
                except (OSError, TypeError, ValueError):
                    try:
                        encoded = store.read_stage(
                            Path(bound["stage_path"]).name,
                            identity,
                        )
                    except (OSError, TypeError, ValueError):
                        # Artifact recovery owns missing/ambiguous formal bytes.
                        continue
                if (
                    len(encoded) != bound["size_bytes"]
                    or hashlib.sha256(encoded).hexdigest() != bound["sha256"]
                ):
                    continue
                try:
                    payload = json.loads(encoded.decode("utf-8"))
                    if (
                        not isinstance(payload, dict)
                        or set(payload) != {
                            "schema_version",
                            "job_id",
                            "job_type",
                            "collected_at",
                            "result",
                        }
                        or payload["schema_version"] != 1
                        or payload["job_id"] != bound["job_id"]
                        or payload["job_type"] != ACCOUNT_COLLECTION_JOB_TYPE
                    ):
                        continue
                    job_input = json.loads(bound["input_data"])
                    metadata = json.loads(bound["metadata_json"])
                    if (
                        not isinstance(job_input, dict)
                        or not isinstance(metadata, dict)
                        or job_input.get("user_id") != profile["user_id"]
                        or metadata.get("user_id") != profile["user_id"]
                        or metadata.get("artifact_id") != bound["artifact_id"]
                        or metadata.get("job_id") != bound["job_id"]
                        or metadata.get("sha256") != bound["sha256"]
                        or metadata.get("size_bytes") != bound["size_bytes"]
                    ):
                        return False
                    collected_at = datetime.fromisoformat(payload["collected_at"])
                    normalized = normalize_exact_account_result(
                        result=CollectionResult.model_validate(payload["result"]),
                        binding=AccountEvidenceBinding(
                            collection_job_id=bound["job_id"],
                            collection_artifact_id=bound["artifact_id"],
                            collected_at=collected_at,
                        ),
                    )
                except (
                    AccountEvidencePersistenceError,
                    KeyError,
                    TypeError,
                    ValueError,
                    UnicodeError,
                    json.JSONDecodeError,
                ):
                    continue
                actual_profile = {
                    "user_id": profile["user_id"],
                    "source_url": profile["source_url"],
                    "nickname": profile["nickname"],
                    "bio": profile["bio"],
                    "public_stats_json": json.loads(profile["public_stats_json"]),
                    "raw_evidence": json.loads(profile["raw_evidence"]),
                    "raw_digest": profile["raw_digest"],
                    "collection_job_id": profile["collection_job_id"],
                    "collection_artifact_id": profile["collection_artifact_id"],
                    "collected_at": datetime.fromisoformat(profile["collected_at"]),
                }
                if versioned:
                    notes = connection.execute(text(
                        "SELECT note.note_id, note.user_id, note.source_url, "
                        "note.title, note.summary, note.published_at, "
                        "note.public_interactions_json, note.raw_evidence, "
                        "note.raw_digest, note.collection_job_id, "
                        "note.collection_artifact_id, note.collected_at "
                        "FROM xhs_account_notes AS note "
                        "JOIN xhs_account_snapshot_notes AS member "
                        "ON member.note_record_id=note.id "
                        "JOIN xhs_account_profile_snapshots AS snapshot "
                        "ON snapshot.id=member.snapshot_id "
                        "WHERE snapshot.collection_job_id=:job_id "
                        "AND snapshot.collection_artifact_id=:artifact_id "
                        "ORDER BY member.position"
                    ), {
                        "job_id": profile["collection_job_id"],
                        "artifact_id": profile["collection_artifact_id"],
                    }).mappings().all()
                else:
                    notes = connection.execute(text(
                        "SELECT note_id, user_id, source_url, title, summary, "
                        "published_at, public_interactions_json, raw_evidence, "
                        "raw_digest, collection_job_id, collection_artifact_id, "
                        "collected_at FROM xhs_account_notes "
                        "WHERE user_id=:user_id ORDER BY id"
                    ), {"user_id": profile["user_id"]}).mappings().all()
                actual_notes = [
                    {
                        "note_id": note["note_id"],
                        "user_id": note["user_id"],
                        "source_url": note["source_url"],
                        "title": note["title"],
                        "summary": note["summary"],
                        "published_at": note["published_at"],
                        "public_interactions_json": json.loads(
                            note["public_interactions_json"]
                        ),
                        "raw_evidence": json.loads(note["raw_evidence"]),
                        "raw_digest": note["raw_digest"],
                        "collection_job_id": note["collection_job_id"],
                        "collection_artifact_id": note["collection_artifact_id"],
                        "collected_at": datetime.fromisoformat(note["collected_at"]),
                    }
                    for note in notes
                ]
                expected_profile = normalized.profile.model_dump()
                expected_notes = [note.model_dump() for note in normalized.notes]
                counters_match = public_counter_json_matches(
                    actual_profile["public_stats_json"],
                    expected_profile["public_stats_json"],
                    allowed_fields=PROFILE_PUBLIC_COUNTER_FIELDS,
                ) and len(actual_notes) == len(expected_notes) and all(
                    public_counter_json_matches(
                        actual["public_interactions_json"],
                        formal["public_interactions_json"],
                        allowed_fields=NOTE_PUBLIC_COUNTER_FIELDS,
                    )
                    for actual, formal in zip(
                        actual_notes,
                        expected_notes,
                        strict=True,
                    )
                )
                if (
                    not counters_match
                    or actual_profile != expected_profile
                    or actual_notes != expected_notes
                ):
                    return False
        return True
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeError,
        SQLAlchemyError,
        UnsafeXhsArtifactStore,
    ):
        return False


def _artifact_quarantine_schema_valid(
    inspector: object, *, require_identity: bool = True,
    require_source_token: bool = True,
) -> bool:
    """Validate the physical cleanup contract instead of trusting a marker."""

    if not _artifact_quarantine_table_valid(
        inspector, require_identity=require_identity,
        require_source_token=require_source_token,
    ):
        return False
    try:
        package_columns = {
            column["name"]: column
            for column in inspector.get_columns("content_packages")
        }
        build_token = package_columns.get("build_token")
        return bool(
            build_token is not None
            and build_token.get("nullable") is True
            and str(build_token.get("type") or "").upper() == "VARCHAR(36)"
        )
    except (KeyError, TypeError, AttributeError, SQLAlchemyError):
        return False


def _artifact_quarantine_table_valid(
    inspector: object, *, require_identity: bool = True,
    require_source_token: bool = True,
) -> bool:
    """Validate every physical queue column, CHECK and open-row identity index."""

    base_columns = {
        "id",
        "owner_type",
        "owner_id",
        "relative_path",
        "path_key",
        "expected_sha256",
        "expected_size_bytes",
        "state",
        "reason",
        "not_before",
        "lease_token",
        "lease_expires_at",
        "quarantine_path",
        "attempt_count",
        "last_error_category",
        "created_at",
        "updated_at",
        "completed_at",
    }
    identity_columns = {
        "quarantine_volume_id",
        "quarantine_file_id",
        "quarantine_size_bytes",
        "quarantine_mtime_ns",
    }
    source_columns = {"source_build_token"}
    required_not_null = {
        "id",
        "owner_type",
        "owner_id",
        "relative_path",
        "path_key",
        "expected_sha256",
        "expected_size_bytes",
        "state",
        "reason",
        "not_before",
        "attempt_count",
        "created_at",
        "updated_at",
    }
    expected_types = {
        "id": "VARCHAR(36)",
        "owner_type": "VARCHAR(32)",
        "owner_id": "VARCHAR(36)",
        "source_build_token": "VARCHAR(36)",
        "relative_path": "TEXT",
        "path_key": "TEXT",
        "expected_sha256": "VARCHAR(64)",
        "expected_size_bytes": "INTEGER",
        "state": "VARCHAR(20)",
        "reason": "VARCHAR(64)",
        "not_before": "DATETIME",
        "lease_token": "VARCHAR(36)",
        "lease_expires_at": "DATETIME",
        "quarantine_path": "TEXT",
        "attempt_count": "INTEGER",
        "last_error_category": "VARCHAR(64)",
        "created_at": "DATETIME",
        "updated_at": "DATETIME",
        "completed_at": "DATETIME",
        "quarantine_volume_id": "INTEGER",
        "quarantine_file_id": "INTEGER",
        "quarantine_size_bytes": "INTEGER",
        "quarantine_mtime_ns": "INTEGER",
    }
    legacy_checks = {
        "ck_artifact_gc_owner_type": "owner_typein('material','content_package')",
        "ck_artifact_gc_state": "statein('pending','claimed','quarantined','deleted','needs_human','cancelled')",
        "ck_artifact_gc_size": "expected_size_bytes>=0",
        "ck_artifact_gc_sha_format": "length(expected_sha256)=64andexpected_sha256notglob'*[^0-9a-f]*'",
        "ck_artifact_gc_attempts": "attempt_count>=0",
        "ck_artifact_gc_uuid_identity": "is_canonical_uuid(id)=1andis_canonical_uuid(owner_id)=1and(lease_tokenisnulloris_canonical_uuid(lease_token)=1)",
        "ck_artifact_gc_lease_state": "((state='claimed'andlease_tokenisnotnullandlease_expires_atisnotnull)or(state!='claimed'andlease_tokenisnullandlease_expires_atisnull))",
        "ck_artifact_gc_timestamps": "datetime(not_before)isnotnullanddatetime(created_at)isnotnullanddatetime(updated_at)isnotnulland(lease_expires_atisnullordatetime(lease_expires_at)isnotnull)and(completed_atisnullordatetime(completed_at)isnotnull)",
        "ck_artifact_gc_relative_path": "artifact_path_key(relative_path)isnotnullandpath_key=artifact_path_key(relative_path)",
        "ck_artifact_gc_quarantine_path": "quarantine_pathisnullorartifact_path_key(quarantine_path)isnotnull",
    }
    hardened_checks = {
        **legacy_checks,
        "ck_artifact_gc_size": "expected_size_bytes>=0andexpected_size_bytes<=262144000",
        "ck_artifact_gc_relative_path": "artifact_path_key(relative_path)isnotnullandlength(relative_path)<=1000andpath_key=artifact_path_key(relative_path)",
        "ck_artifact_gc_quarantine_path": "quarantine_pathisnullor(length(quarantine_path)<=1000andartifact_path_key(quarantine_path)isnotnull)",
        "ck_artifact_gc_quarantine_identity": "((quarantine_pathisnullandquarantine_volume_idisnullandquarantine_file_idisnullandquarantine_size_bytesisnullandquarantine_mtime_nsisnull)or(quarantine_pathisnotnullandquarantine_volume_idisnotnullandquarantine_file_idisnotnullandquarantine_size_bytesisnotnullandquarantine_mtime_nsisnotnullandquarantine_volume_id>=0andquarantine_file_id>=0andquarantine_size_bytes>=0andquarantine_size_bytes<=262144000andquarantine_mtime_ns>=0))and(state!='quarantined'orquarantine_pathisnotnull)",
    }
    try:
        tables = set(inspector.get_table_names())
        if "artifact_gc_queue" not in tables or "content_packages" not in tables:
            return False
        columns = {
            column["name"]: column
            for column in inspector.get_columns("artifact_gc_queue")
        }
        column_names = set(columns)
        has_identity = identity_columns <= column_names
        has_source_token = source_columns <= column_names
        allowed = base_columns | (identity_columns if has_identity else set()) | (
            source_columns if has_source_token else set()
        )
        if column_names != allowed:
            return False
        if require_identity and not has_identity:
            return False
        if require_source_token and not has_source_token:
            return False
        if any(columns[name].get("nullable") is not False for name in required_not_null):
            return False
        if any(
            columns[name].get("nullable") is not True
            for name in set(columns) - required_not_null
        ):
            return False
        if {
            name: str(column.get("type") or "").upper()
            for name, column in columns.items()
        } != {name: expected_types[name] for name in columns}:
            return False
        checks = {
            item.get("name"): _compact_sql(item.get("sqltext"))
            for item in inspector.get_check_constraints("artifact_gc_queue")
        }
        source_check = (
            "is_canonical_uuid(id)=1andis_canonical_uuid(owner_id)=1and"
            "(lease_tokenisnulloris_canonical_uuid(lease_token)=1)and"
            "((owner_type='material'andsource_build_tokenisnull)or"
            "(owner_type='content_package'andis_canonical_uuid(source_build_token)=1))"
        )
        expected_checks = dict(hardened_checks if has_identity else legacy_checks)
        if has_source_token:
            expected_checks["ck_artifact_gc_uuid_identity"] = source_check
        if checks != expected_checks:
            return False
        indexes = {
            item.get("name"): (
                tuple(item.get("column_names") or ()),
                bool(item.get("unique")),
                _compact_sql((item.get("dialect_options") or {}).get("sqlite_where")),
            )
            for item in inspector.get_indexes("artifact_gc_queue")
        }
        if indexes != {
            "uq_artifact_gc_open_owner": (
                ("owner_type", "owner_id", "path_key"),
                True,
                "statein('pending','claimed','quarantined','needs_human')",
            )
        }:
            return False
    except (KeyError, TypeError, AttributeError, SQLAlchemyError):
        return False
    return True


_BUILD_TOKEN_INSERT_TRIGGER = """
CREATE TRIGGER ck_content_packages_build_token_insert
BEFORE INSERT ON content_packages
WHEN NEW.build_token IS NOT NULL AND is_canonical_uuid(NEW.build_token) != 1
BEGIN
    SELECT RAISE(ABORT, 'content_packages.build_token must be a canonical UUID');
END
"""

_BUILD_TOKEN_UPDATE_TRIGGER = """
CREATE TRIGGER ck_content_packages_build_token_update
BEFORE UPDATE OF build_token ON content_packages
WHEN NEW.build_token IS NOT NULL AND is_canonical_uuid(NEW.build_token) != 1
BEGIN
    SELECT RAISE(ABORT, 'content_packages.build_token must be a canonical UUID');
END
"""

_CLEANUP_SOURCE_TOKEN_INSERT_TRIGGER = """
CREATE TRIGGER ck_artifact_gc_source_token_insert
BEFORE INSERT ON artifact_gc_queue
WHEN (NEW.owner_type = 'material' AND NEW.source_build_token IS NOT NULL)
OR (NEW.owner_type = 'content_package' AND (
    is_canonical_uuid(NEW.source_build_token) != 1
    OR NOT EXISTS (
        SELECT 1 FROM content_packages AS package
        WHERE package.id = NEW.owner_id
          AND package.build_token = NEW.source_build_token
          AND windows_artifact_path_key(package.path)
              = windows_artifact_path_key(NEW.relative_path)
          AND package.sha256 = NEW.expected_sha256
          AND package.size_bytes = NEW.expected_size_bytes
    )
))
BEGIN
    SELECT RAISE(ABORT, 'artifact cleanup source generation is invalid');
END
"""

_CLEANUP_SOURCE_TOKEN_UPDATE_TRIGGER = """
CREATE TRIGGER ck_artifact_gc_source_token_update
BEFORE UPDATE OF source_build_token ON artifact_gc_queue
WHEN OLD.source_build_token IS NOT NEW.source_build_token
BEGIN
    SELECT RAISE(ABORT, 'artifact cleanup source generation is immutable');
END
"""

_CLEANUP_IDENTITY_WHEN = """
length(NEW.relative_path) > 1000
OR NEW.expected_size_bytes < 0 OR NEW.expected_size_bytes > 262144000
OR (NEW.quarantine_path IS NULL AND (
    NEW.quarantine_volume_id IS NOT NULL OR NEW.quarantine_file_id IS NOT NULL
    OR NEW.quarantine_size_bytes IS NOT NULL OR NEW.quarantine_mtime_ns IS NOT NULL
))
OR (NEW.quarantine_path IS NOT NULL AND (
    length(NEW.quarantine_path) > 1000
    OR NEW.quarantine_volume_id IS NULL OR NEW.quarantine_file_id IS NULL
    OR NEW.quarantine_size_bytes IS NULL OR NEW.quarantine_mtime_ns IS NULL
    OR NEW.quarantine_volume_id < 0 OR NEW.quarantine_file_id < 0
    OR NEW.quarantine_size_bytes < 0 OR NEW.quarantine_size_bytes > 262144000
    OR NEW.quarantine_mtime_ns < 0
))
OR (NEW.state = 'quarantined' AND NEW.quarantine_path IS NULL)
"""

_CLEANUP_IDENTITY_INSERT_TRIGGER = f"""
CREATE TRIGGER ck_artifact_gc_identity_insert
BEFORE INSERT ON artifact_gc_queue
WHEN {_CLEANUP_IDENTITY_WHEN}
BEGIN
    SELECT RAISE(ABORT, 'artifact cleanup identity is invalid');
END
"""

_CLEANUP_IDENTITY_UPDATE_TRIGGER = f"""
CREATE TRIGGER ck_artifact_gc_identity_update
BEFORE UPDATE ON artifact_gc_queue
WHEN {_CLEANUP_IDENTITY_WHEN}
BEGIN
    SELECT RAISE(ABORT, 'artifact cleanup identity is invalid');
END
"""

_QUARANTINE_REFERENCE_WHEN = """
windows_artifact_path_key(NEW.path) IS NULL
OR EXISTS (
    SELECT 1 FROM artifact_gc_queue AS cleanup
    WHERE cleanup.quarantine_path IS NOT NULL
      AND cleanup.state IN ('claimed','quarantined','deleted','needs_human')
      AND windows_artifact_path_key(cleanup.quarantine_path)
          = windows_artifact_path_key(NEW.path)
)
"""


def _reference_guard_trigger(name: str, table: str, operation: str) -> str:
    return f"""
CREATE TRIGGER {name}
BEFORE {operation} ON {table}
WHEN {_QUARANTINE_REFERENCE_WHEN}
BEGIN
    SELECT RAISE(ABORT, 'artifact path is invalid or references a quarantine path');
END
"""


_MATERIAL_PATH_INSERT_TRIGGER = _reference_guard_trigger(
    "ck_gc_material_path_insert", "content_product_materials", "INSERT"
)
_PACKAGE_PATH_INSERT_TRIGGER = _reference_guard_trigger(
    "ck_gc_package_path_insert", "content_packages", "INSERT"
)

_ARTIFACT_REFERENCE_ROWS = """
SELECT 'material' AS reference_type, id, NULL AS status, path, sha256, size_bytes,
       NULL AS build_token
FROM content_product_materials
UNION ALL
SELECT 'content_package' AS reference_type, id, status, path, sha256, size_bytes,
       build_token
FROM content_packages
"""


def _cleanup_reference_exists(
    cleanup: str,
    *,
    target_path: str,
    require_expected_identity: bool,
    exclude_material_owner: bool,
    exclude_exact_failed_package_owner: bool,
) -> str:
    conditions = [
        "windows_artifact_path_key(artifact_reference.path) "
        f"= windows_artifact_path_key({target_path})"
    ]
    if require_expected_identity:
        conditions.extend(
            [
                f"artifact_reference.sha256 = {cleanup}.expected_sha256",
                f"artifact_reference.size_bytes = {cleanup}.expected_size_bytes",
            ]
        )
    if exclude_material_owner:
        conditions.append(
            "NOT (artifact_reference.reference_type = 'material' "
            f"AND {cleanup}.owner_type = 'material' "
            f"AND artifact_reference.id = {cleanup}.owner_id)"
        )
    if exclude_exact_failed_package_owner:
        conditions.append(
            "NOT (artifact_reference.reference_type = 'content_package' "
            f"AND {cleanup}.owner_type = 'content_package' "
            f"AND artifact_reference.id = {cleanup}.owner_id "
            "AND artifact_reference.status = 'failed' "
            f"AND artifact_reference.build_token = {cleanup}.source_build_token "
            "AND windows_artifact_path_key(artifact_reference.path) "
            f"= windows_artifact_path_key({cleanup}.relative_path) "
            f"AND artifact_reference.sha256 = {cleanup}.expected_sha256 "
            f"AND artifact_reference.size_bytes = {cleanup}.expected_size_bytes)"
        )
    where = "\n      AND ".join(conditions)
    return f"""
EXISTS (
    SELECT 1 FROM ({_ARTIFACT_REFERENCE_ROWS}) AS artifact_reference
    WHERE {where}
)
"""


def _cleanup_reference_conflict_when(cleanup: str) -> str:
    quarantine_reference_exists = _cleanup_reference_exists(
        cleanup,
        target_path=f"{cleanup}.quarantine_path",
        require_expected_identity=False,
        exclude_material_owner=False,
        exclude_exact_failed_package_owner=False,
    )
    quarantine_live_reference_exists = _cleanup_reference_exists(
        cleanup,
        target_path=f"{cleanup}.quarantine_path",
        require_expected_identity=True,
        exclude_material_owner=True,
        exclude_exact_failed_package_owner=True,
    )
    original_external_live_reference_exists = _cleanup_reference_exists(
        cleanup,
        target_path=f"{cleanup}.relative_path",
        require_expected_identity=True,
        exclude_material_owner=True,
        exclude_exact_failed_package_owner=True,
    )
    live_reference_exception = f"""
{cleanup}.state = 'needs_human'
AND COALESCE({cleanup}.last_error_category, '') = 'live_reference'
AND {cleanup}.quarantine_path IS NOT NULL
AND {cleanup}.quarantine_volume_id IS NOT NULL
AND {cleanup}.quarantine_file_id IS NOT NULL
AND {cleanup}.quarantine_size_bytes IS NOT NULL
AND {cleanup}.quarantine_mtime_ns IS NOT NULL
AND {quarantine_live_reference_exists}
"""
    moved_live_reference_fact = f"""
{cleanup}.state = 'needs_human'
AND COALESCE({cleanup}.last_error_category, '') = 'live_reference'
AND {cleanup}.quarantine_path IS NOT NULL
AND {cleanup}.quarantine_volume_id IS NOT NULL
AND {cleanup}.quarantine_file_id IS NOT NULL
AND {cleanup}.quarantine_size_bytes IS NOT NULL
AND {cleanup}.quarantine_mtime_ns IS NOT NULL
AND (
    {quarantine_live_reference_exists}
    OR {original_external_live_reference_exists}
)
"""
    return f"""
(
    {cleanup}.quarantine_path IS NOT NULL
    AND {cleanup}.state IN ('claimed','quarantined','deleted','needs_human')
    AND {quarantine_reference_exists}
    AND NOT ({live_reference_exception})
)
OR (
    {cleanup}.quarantine_path IS NOT NULL
    AND COALESCE({cleanup}.last_error_category, '') = 'live_reference'
    AND NOT ({moved_live_reference_fact})
)
"""


_CLEANUP_CAPTURES_REFERENCE_WHEN = _cleanup_reference_conflict_when("NEW")


def _reference_guard_update_trigger(
    name: str, table: str, mutable_columns: str
) -> str:
    cleanup_conflict = _cleanup_reference_conflict_when("cleanup")
    return f"""
CREATE TRIGGER {name}
AFTER UPDATE OF {mutable_columns} ON {table}
WHEN (OLD.path IS NOT NEW.path AND ({_QUARANTINE_REFERENCE_WHEN}))
OR EXISTS (
    SELECT 1 FROM artifact_gc_queue AS cleanup
    WHERE {cleanup_conflict}
)
BEGIN
    SELECT RAISE(ABORT, 'quarantine path conflicts with an existing artifact reference');
END
"""


_MATERIAL_PATH_UPDATE_TRIGGER = _reference_guard_update_trigger(
    "ck_gc_material_path_update",
    "content_product_materials",
    "id, path, sha256, size_bytes",
)
_PACKAGE_PATH_UPDATE_TRIGGER = _reference_guard_update_trigger(
    "ck_gc_package_path_update",
    "content_packages",
    "id, status, path, sha256, size_bytes",
)

_CLEANUP_REFERENCE_INSERT_TRIGGER = f"""
CREATE TRIGGER ck_gc_cleanup_reference_insert
BEFORE INSERT ON artifact_gc_queue
WHEN {_CLEANUP_CAPTURES_REFERENCE_WHEN}
BEGIN
    SELECT RAISE(ABORT, 'quarantine path conflicts with an existing artifact reference');
END
"""

_CLEANUP_REFERENCE_UPDATE_TRIGGER = f"""
CREATE TRIGGER ck_gc_cleanup_reference_update
BEFORE UPDATE OF owner_type, owner_id, relative_path, path_key, expected_sha256, expected_size_bytes, quarantine_path, state, last_error_category,
quarantine_volume_id, quarantine_file_id, quarantine_size_bytes,
quarantine_mtime_ns ON artifact_gc_queue
WHEN {_CLEANUP_CAPTURES_REFERENCE_WHEN}
BEGIN
    SELECT RAISE(ABORT, 'quarantine path conflicts with an existing artifact reference');
END
"""

_REFERENCE_GUARD_TRIGGERS = {
    "ck_gc_material_path_insert": _MATERIAL_PATH_INSERT_TRIGGER,
    "ck_gc_material_path_update": _MATERIAL_PATH_UPDATE_TRIGGER,
    "ck_gc_package_path_insert": _PACKAGE_PATH_INSERT_TRIGGER,
    "ck_gc_package_path_update": _PACKAGE_PATH_UPDATE_TRIGGER,
    "ck_gc_cleanup_reference_insert": _CLEANUP_REFERENCE_INSERT_TRIGGER,
    "ck_gc_cleanup_reference_update": _CLEANUP_REFERENCE_UPDATE_TRIGGER,
}


def _create_artifact_quarantine_triggers(connection: Connection) -> None:
    connection.execute(text("DROP TRIGGER IF EXISTS ck_content_packages_build_token_insert"))
    connection.execute(text("DROP TRIGGER IF EXISTS ck_content_packages_build_token_update"))
    connection.execute(text(_BUILD_TOKEN_INSERT_TRIGGER))
    connection.execute(text(_BUILD_TOKEN_UPDATE_TRIGGER))


def _create_artifact_quarantine_identity_triggers(connection: Connection) -> None:
    connection.execute(text("DROP TRIGGER IF EXISTS ck_artifact_gc_identity_insert"))
    connection.execute(text("DROP TRIGGER IF EXISTS ck_artifact_gc_identity_update"))
    connection.execute(text(_CLEANUP_IDENTITY_INSERT_TRIGGER))
    connection.execute(text(_CLEANUP_IDENTITY_UPDATE_TRIGGER))


def _create_artifact_cleanup_source_token_triggers(connection: Connection) -> None:
    connection.execute(text("DROP TRIGGER IF EXISTS ck_artifact_gc_source_token_insert"))
    connection.execute(text("DROP TRIGGER IF EXISTS ck_artifact_gc_source_token_update"))
    connection.execute(text(_CLEANUP_SOURCE_TOKEN_INSERT_TRIGGER))
    connection.execute(text(_CLEANUP_SOURCE_TOKEN_UPDATE_TRIGGER))


def _artifact_cleanup_source_token_triggers_valid(connection: Connection) -> bool:
    names = (
        "ck_artifact_gc_source_token_insert",
        "ck_artifact_gc_source_token_update",
    )
    rows = connection.execute(
        text(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
            "AND name IN ('ck_artifact_gc_source_token_insert',"
            "'ck_artifact_gc_source_token_update')"
        )
    ).mappings().all()
    actual = {row["name"]: _compact_sql(row["sql"]) for row in rows}
    expected = {
        names[0]: _compact_sql(_CLEANUP_SOURCE_TOKEN_INSERT_TRIGGER),
        names[1]: _compact_sql(_CLEANUP_SOURCE_TOKEN_UPDATE_TRIGGER),
    }
    return actual == expected


def _create_artifact_reference_guard_triggers(connection: Connection) -> None:
    for name, definition in _REFERENCE_GUARD_TRIGGERS.items():
        connection.execute(text(f"DROP TRIGGER IF EXISTS {name}"))
        connection.execute(text(definition))


def _artifact_reference_guard_triggers_valid(connection: Connection) -> bool:
    names = ",".join(f"'{name}'" for name in _REFERENCE_GUARD_TRIGGERS)
    rows = connection.execute(
        text(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
            f"AND name IN ({names})"
        )
    ).mappings().all()
    actual = {row["name"]: _compact_sql(row["sql"]) for row in rows}
    expected = {
        name: _compact_sql(definition)
        for name, definition in _REFERENCE_GUARD_TRIGGERS.items()
    }
    return actual == expected


def _artifact_reference_guard_data_valid(connection: Connection) -> bool:
    cleanup_conflict = _cleanup_reference_conflict_when("cleanup")
    try:
        return connection.scalar(
            text(
                "SELECT 1 FROM artifact_gc_queue AS cleanup WHERE "
                f"{cleanup_conflict} "
                "LIMIT 1"
            )
        ) is None
    except SQLAlchemyError:
        return False


def _artifact_quarantine_triggers_valid(
    connection: Connection, *, require_identity: bool = True
) -> bool:
    names = [
        "ck_content_packages_build_token_insert",
        "ck_content_packages_build_token_update",
    ]
    if require_identity:
        names.extend(
            ["ck_artifact_gc_identity_insert", "ck_artifact_gc_identity_update"]
        )
    placeholders = ",".join(f"'{name}'" for name in names)
    rows = connection.execute(
        text(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
            f"AND name IN ({placeholders})"
        )
    ).mappings().all()
    actual = {row["name"]: _compact_sql(row["sql"]) for row in rows}
    expected = {
        "ck_content_packages_build_token_insert": _compact_sql(
            _BUILD_TOKEN_INSERT_TRIGGER
        ),
        "ck_content_packages_build_token_update": _compact_sql(
            _BUILD_TOKEN_UPDATE_TRIGGER
        ),
    }
    if require_identity:
        expected.update(
            {
                "ck_artifact_gc_identity_insert": _compact_sql(
                    _CLEANUP_IDENTITY_INSERT_TRIGGER
                ),
                "ck_artifact_gc_identity_update": _compact_sql(
                    _CLEANUP_IDENTITY_UPDATE_TRIGGER
                ),
            }
        )
    return actual == expected


def _artifact_quarantine_data_valid(
    connection: Connection, *, require_identity: bool = True,
    require_source_token: bool = True,
) -> bool:
    try:
        build_tokens = connection.execute(
            text("SELECT build_token FROM content_packages WHERE build_token IS NOT NULL")
        ).scalars()
        if any(not is_canonical_uuid_text(token) for token in build_tokens):
            return False
        cleanup_columns = {
            column["name"] for column in inspect(connection).get_columns("artifact_gc_queue")
        }
        has_source_token = "source_build_token" in cleanup_columns
        if require_source_token and not has_source_token:
            return False
        identity_sql = (
            ", quarantine_volume_id, quarantine_file_id, quarantine_size_bytes, "
            "quarantine_mtime_ns" if require_identity else ""
        )
        rows = connection.execute(
            text(
                "SELECT id, owner_id, relative_path, path_key, quarantine_path, "
                "expected_size_bytes, state, lease_token, lease_expires_at"
                + (", owner_type, source_build_token" if has_source_token else "")
                + f"{identity_sql} FROM artifact_gc_queue"
            )
        ).mappings()
        for row in rows:
            expected_key = canonical_artifact_path_key(row["relative_path"])
            lease_complete = (
                is_canonical_uuid_text(row["lease_token"])
                and row["lease_expires_at"] is not None
            )
            if (
                not is_canonical_uuid_text(row["id"])
                or not is_canonical_uuid_text(row["owner_id"])
                or expected_key is None
                or row["path_key"] != expected_key
                or len(row["relative_path"]) > 1000
                or not 0 <= row["expected_size_bytes"] <= 262144000
                or (
                    row["quarantine_path"] is not None
                    and canonical_artifact_path_key(row["quarantine_path"]) is None
                )
                or ((row["state"] == "claimed") != lease_complete)
                or (
                    has_source_token
                    and (
                        (row["owner_type"] == "material" and row["source_build_token"] is not None)
                        or (
                            row["owner_type"] == "content_package"
                            and not is_canonical_uuid_text(row["source_build_token"])
                        )
                    )
                )
            ):
                return False
            if require_identity:
                identity = (
                    row["quarantine_volume_id"], row["quarantine_file_id"],
                    row["quarantine_size_bytes"], row["quarantine_mtime_ns"],
                )
                if (
                    (row["quarantine_path"] is None)
                    != all(value is None for value in identity)
                    or (
                        row["quarantine_path"] is not None
                        and (
                            len(row["quarantine_path"]) > 1000
                            or any(value is None or value < 0 for value in identity)
                            or row["quarantine_size_bytes"] > 262144000
                        )
                    )
                    or (row["state"] == "quarantined" and row["quarantine_path"] is None)
                ):
                    return False
    except (KeyError, TypeError, AttributeError, SQLAlchemyError):
        return False
    return True
