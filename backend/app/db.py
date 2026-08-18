"""SQLite database lifecycle for durable local workbench facts."""

import json
from datetime import datetime, timezone
from pathlib import Path
import unicodedata
from uuid import UUID, uuid4

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


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
        from backend.app.features.radar.models import RankItemRecord, RankSnapshotRecord
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
            JobArtifactRecord,
            JobLogRecord,
            JobRecord,
            RankItemRecord,
            RankSnapshotRecord,
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
        if review_audit_marker_present:
            self._require_content_review_audit_schema()
        if quarantine_marker_present:
            self._require_artifact_quarantine_schema(
                require_identity=quarantine_identity_marker_present,
                require_source_token=quarantine_source_token_marker_present,
            )
        if quarantine_reference_guard_marker_present and quarantine_source_token_marker_present:
            self._require_artifact_quarantine_reference_guards()
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
