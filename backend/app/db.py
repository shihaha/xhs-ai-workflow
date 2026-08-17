"""SQLite database lifecycle for durable local workbench facts."""

import json
from pathlib import Path, PurePosixPath
import re

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
        self._migrate_content_schema()

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
            stranded_packages = connection.execute(
                text(
                    "SELECT id, content_item_id, path FROM content_packages "
                    "WHERE status='building'"
                )
            ).mappings().all()
            if self.runtime_dir is not None:
                from backend.app.features.content.export import remove_contained_regular

                material_paths = set(
                    connection.execute(
                        text("SELECT path FROM content_product_materials")
                    ).scalars().all()
                )
                package_path_owners: dict[str, set[str]] = {}
                for package_id, relative_path in connection.execute(
                    text("SELECT id, path FROM content_packages")
                ):
                    if isinstance(relative_path, str):
                        package_path_owners.setdefault(relative_path, set()).add(package_id)
                for package in stranded_packages:
                    if _building_package_owns_artifact(
                        package,
                        material_paths=material_paths,
                        package_path_owners=package_path_owners,
                    ):
                        remove_contained_regular(self.runtime_dir, package["path"])
            connection.execute(
                text(
                    "UPDATE content_packages SET status='failed', "
                    "error_detail='worker_restart_required' WHERE status='building'"
                )
            )
            connection.execute(
                text(
                    "INSERT OR IGNORE INTO workbench_schema_migrations (name, applied_at) "
                    "VALUES ('task8_content_v2', CURRENT_TIMESTAMP)"
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


def _content_schema_valid(inspector: object) -> bool:
    required_columns = {
        "content_products": {"id", "opportunity_id", "name", "target_user", "created_at"},
        "content_product_materials": {"id", "product_id", "logical_name", "logical_key", "version", "path", "sha256", "size_bytes", "media_type", "kind", "created_at"},
        "content_items": {"id", "product_id", "opportunity_id", "template_key", "status", "evidence_ids_json", "material_ids_json", "image_material_ids_json", "cover_material_id", "research_facts_json", "current_revision_id", "created_at", "updated_at"},
        "content_revisions": {"id", "content_item_id", "number", "title", "body", "claims_json", "source_evidence_ids_json", "image_plan_json", "model_provider", "model_name", "prompt_version", "usage_json", "attempts_json", "created_at"},
        "content_reviews": {"id", "content_item_id", "revision_id", "decision", "actor", "note", "visual_checks_json", "created_at"},
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
            for name in required - {"current_revision_id", "error_detail"}:
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
            "content_reviews": {"ck_review_decision": "decisionin('approve','reject','regenerate')"},
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


_PACKAGE_FILENAME = re.compile(r"^(?P<id>[0-9a-f-]{36})(?:-[0-9a-f]{32})?\.zip$")


def _building_package_owns_artifact(
    package: object,
    *,
    material_paths: set[object],
    package_path_owners: dict[str, set[str]],
) -> bool:
    """Prove a stranded path is exclusively the artifact reserved by this package."""

    try:
        package_id = package["id"]  # type: ignore[index]
        content_item_id = package["content_item_id"]  # type: ignore[index]
        relative_path = package["path"]  # type: ignore[index]
        if not all(isinstance(value, str) and value for value in (package_id, content_item_id, relative_path)):
            return False
        if relative_path in material_paths:
            return False
        if package_path_owners.get(relative_path) != {package_id}:
            return False
        path = PurePosixPath(relative_path)
        if path.parts[:2] != ("content-packages", content_item_id) or len(path.parts) != 3:
            return False
        match = _PACKAGE_FILENAME.fullmatch(path.name)
        return match is not None and match.group("id") == package_id
    except (KeyError, TypeError, ValueError):
        return False
