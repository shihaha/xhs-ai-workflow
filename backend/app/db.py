"""SQLite database lifecycle for durable local workbench facts."""

import json
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """Base class for all persisted workbench records."""


class SchemaMigrationError(SQLAlchemyError):
    """Raised when a migration marker contradicts the physical SQLite schema."""


class Database:
    """Own the local SQLite engine and initialize its durable schema."""

    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.database_path = database_path
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
        Base.metadata.create_all(self.engine)
        self._migrate_artifact_provenance()
        self._migrate_analysis_scope()

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
