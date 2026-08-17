import sqlite3
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from backend.app.db import Database, SchemaMigrationError
from backend.app.features.content import models as content_models
from backend.app.features.content import schemas as content_schemas
from backend.app.features.content.models import ContentPackageRecord
from backend.app.features.content.schemas import ExportCreate
from backend.tests.content.test_hardening import _approval, _image_item


def _cleanup_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "id": str(uuid4()),
        "owner_type": "content_package",
        "owner_id": str(uuid4()),
        "relative_path": "content-packages/item/package.zip",
        "path_key": "content-packages/item/package.zip",
        "expected_sha256": "a" * 64,
        "expected_size_bytes": 12,
        "state": "pending",
        "reason": "worker_restart_required",
        "not_before": datetime(2026, 8, 18),
        "lease_token": None,
        "lease_expires_at": None,
        "quarantine_path": None,
        "attempt_count": 0,
        "last_error_category": None,
        "created_at": datetime(2026, 8, 18),
        "updated_at": datetime(2026, 8, 18),
        "completed_at": None,
    }
    values.update(overrides)
    return values


def _insert_cleanup(database: Database, **overrides: object) -> object:
    values = _cleanup_values(**overrides)
    with database.session() as session:
        record = content_models.ArtifactCleanupRecord(**values)
        session.add(record)
        session.commit()
        session.refresh(record)
        return record


def test_fresh_schema_has_cleanup_queue_build_token_and_migration_marker(tmp_path: Path) -> None:
    database = Database(tmp_path / "fresh.sqlite3", runtime_dir=tmp_path)
    try:
        inspection = inspect(database.engine)
        assert "artifact_gc_queue" in inspection.get_table_names()
        package_columns = {
            column["name"] for column in inspection.get_columns("content_packages")
        }
        assert "build_token" in package_columns
        cleanup_columns = {
            column["name"] for column in inspection.get_columns("artifact_gc_queue")
        }
        assert "path_key" in cleanup_columns
        cleanup_index = {
            item["name"]: tuple(item["column_names"])
            for item in inspection.get_indexes("artifact_gc_queue")
        }
        assert cleanup_index["uq_artifact_gc_open_owner"] == (
            "owner_type", "owner_id", "path_key"
        )
        with database.engine.connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT COUNT(*) FROM workbench_schema_migrations "
                    "WHERE name='task8_artifact_quarantine_v1'"
                )
            ) == 1
    finally:
        database.close()


def test_cleanup_state_and_open_owner_are_physically_constrained(tmp_path: Path) -> None:
    database = Database(tmp_path / "constraints.sqlite3", runtime_dir=tmp_path)
    try:
        with pytest.raises(IntegrityError):
            _insert_cleanup(database, state="unknown")
        owner_id = str(uuid4())
        first = _insert_cleanup(database, owner_id=owner_id)
        with pytest.raises(IntegrityError):
            _insert_cleanup(database, owner_id=owner_id)
        assert first.state == "pending"
    finally:
        database.close()


def test_completed_cleanup_does_not_block_new_open_record(tmp_path: Path) -> None:
    database = Database(tmp_path / "completed.sqlite3", runtime_dir=tmp_path)
    try:
        owner_id = str(uuid4())
        completed = _insert_cleanup(database, owner_id=owner_id, state="deleted")
        pending = _insert_cleanup(database, owner_id=owner_id)
        assert completed.id != pending.id
        assert pending.state == "pending"
    finally:
        database.close()


def test_cleanup_timestamps_are_physically_constrained(tmp_path: Path) -> None:
    database = Database(tmp_path / "timestamp-constraints.sqlite3", runtime_dir=tmp_path)
    try:
        values = _cleanup_values()
        values = {
            key: value.isoformat(sep=" ") if isinstance(value, datetime) else value
            for key, value in values.items()
        }
        values["not_before"] = "not-a-timestamp"
        column_names = ",".join(values)
        placeholders = ",".join(f":{name}" for name in values)
        with pytest.raises(IntegrityError):
            with database.engine.begin() as connection:
                connection.execute(
                    text(
                        f"INSERT INTO artifact_gc_queue ({column_names}) "
                        f"VALUES ({placeholders})"
                    ),
                    values,
                )
    finally:
        database.close()


@pytest.mark.parametrize(
    "invalid_path",
    [
        "/absolute/file.zip",
        "C:/absolute/file.zip",
        "folder\\file.zip",
        "folder//file.zip",
        "folder/./file.zip",
        "folder/../file.zip",
        "folder/   /file.zip",
        "folder./file.zip",
        "folder /file.zip",
        "folder/CON.txt",
        "folder/bad\x1f.zip",
        "folder/e\u0301.zip",
    ],
)
def test_cleanup_paths_are_canonical_in_database(
    tmp_path: Path, invalid_path: str
) -> None:
    database = Database(tmp_path / f"invalid-{uuid4()}.sqlite3", runtime_dir=tmp_path)
    try:
        with pytest.raises(IntegrityError):
            _insert_cleanup(
                database,
                relative_path=invalid_path,
                path_key=invalid_path.casefold(),
            )
        with pytest.raises(IntegrityError):
            _insert_cleanup(database, quarantine_path=invalid_path)
    finally:
        database.close()


def test_windows_equivalent_paths_share_one_open_identity(tmp_path: Path) -> None:
    database = Database(tmp_path / "path-key.sqlite3", runtime_dir=tmp_path)
    try:
        owner_id = str(uuid4())
        first = _insert_cleanup(
            database,
            owner_id=owner_id,
            relative_path="Content-Packages/Item/File.ZIP",
            path_key="content-packages/item/file.zip",
        )
        with pytest.raises(IntegrityError):
            _insert_cleanup(
                database,
                owner_id=owner_id,
                relative_path="content-packages/item/file.zip",
                path_key="content-packages/item/file.zip",
            )
        assert first.path_key == "content-packages/item/file.zip"
    finally:
        database.close()


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "NOT-A-UUID"},
        {"owner_id": "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"},
        {"lease_token": str(uuid4()), "lease_expires_at": datetime(2026, 8, 19)},
        {"state": "claimed"},
        {
            "state": "claimed",
            "lease_token": "not-a-uuid",
            "lease_expires_at": datetime(2026, 8, 19),
        },
    ],
)
def test_cleanup_identity_and_lease_state_are_physically_constrained(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    database = Database(tmp_path / f"identity-{uuid4()}.sqlite3", runtime_dir=tmp_path)
    try:
        with pytest.raises(IntegrityError):
            _insert_cleanup(database, **overrides)
    finally:
        database.close()


def test_claimed_cleanup_accepts_one_complete_canonical_lease(tmp_path: Path) -> None:
    database = Database(tmp_path / "valid-lease.sqlite3", runtime_dir=tmp_path)
    try:
        record = _insert_cleanup(
            database,
            state="claimed",
            lease_token=str(uuid4()),
            lease_expires_at=datetime(2026, 8, 19),
        )
        assert record.state == "claimed"
    finally:
        database.close()


def test_build_token_is_canonical_uuid_on_direct_write(tmp_path: Path) -> None:
    service, item, image = _image_item(tmp_path)
    approved = service.review(item.id, _approval(item, image))
    package = service.export_package(
        item.id, ExportCreate(expected_revision_id=approved.current_revision.id)
    )
    try:
        with pytest.raises(IntegrityError):
            with service.database.engine.begin() as connection:
                connection.execute(
                    text("UPDATE content_packages SET build_token='NOT-A-UUID' WHERE id=:id"),
                    {"id": package.id},
                )
    finally:
        service.database.close()


def test_cleanup_read_is_strict_and_has_no_mutation_defaults() -> None:
    values = _cleanup_values()
    read = content_schemas.ArtifactCleanupRead.model_validate(values)
    assert read.state == "pending"
    with pytest.raises(ValidationError):
        content_schemas.ArtifactCleanupRead.model_validate({**values, "force_delete": True})
    incomplete = dict(values)
    incomplete.pop("lease_token")
    with pytest.raises(ValidationError):
        content_schemas.ArtifactCleanupRead.model_validate(incomplete)
    with pytest.raises(ValidationError):
        content_schemas.ArtifactCleanupRead.model_validate(
            {**values, "relative_path": "Folder/File.zip", "path_key": "WRONG"}
        )
    with pytest.raises(ValidationError):
        content_schemas.ArtifactCleanupRead.model_validate(
            {**values, "lease_expires_at": datetime(2026, 8, 19)}
        )
    with pytest.raises(ValidationError):
        content_schemas.ArtifactCleanupRead.model_validate(
            {**values, "quarantine_path": "artifacts-quarantine/../victim.zip"}
        )


def test_legacy_building_package_is_failed_and_enqueued_without_deleting_file(
    tmp_path: Path,
) -> None:
    service, item, image = _image_item(tmp_path)
    approved = service.review(item.id, _approval(item, image))
    package = service.export_package(
        item.id, ExportCreate(expected_revision_id=approved.current_revision.id)
    )
    artifact = tmp_path / package.path
    original = artifact.read_bytes()
    with service.database.session() as session:
        record = session.get(ContentPackageRecord, package.id)
        record.status = "building"
        session.commit()
    database_path = service.database.database_path
    service.database.close()

    upgraded = Database(database_path, runtime_dir=tmp_path)
    try:
        with upgraded.session() as session:
            recovered = session.get(ContentPackageRecord, package.id)
            cleanup = session.query(content_models.ArtifactCleanupRecord).filter_by(
                owner_type="content_package", owner_id=package.id, relative_path=package.path
            ).one()
            assert recovered.status == "failed"
            assert recovered.error_detail == "worker_restart_required"
            assert cleanup.state == "pending"
            assert cleanup.expected_sha256 == package.sha256
            assert cleanup.expected_size_bytes == package.size_bytes
        assert artifact.read_bytes() == original
    finally:
        upgraded.close()


def test_repeated_startup_does_not_duplicate_recovery_cleanup(tmp_path: Path) -> None:
    service, item, image = _image_item(tmp_path)
    approved = service.review(item.id, _approval(item, image))
    package = service.export_package(
        item.id, ExportCreate(expected_revision_id=approved.current_revision.id)
    )
    with service.database.session() as session:
        session.get(ContentPackageRecord, package.id).status = "building"
        session.commit()
    database_path = service.database.database_path
    service.database.close()

    first = Database(database_path, runtime_dir=tmp_path)
    first.close()
    second = Database(database_path, runtime_dir=tmp_path)
    try:
        with second.session() as session:
            assert session.query(content_models.ArtifactCleanupRecord).filter_by(
                owner_id=package.id
            ).count() == 1
    finally:
        second.close()


def test_marker_does_not_hide_malformed_cleanup_index(tmp_path: Path) -> None:
    path = tmp_path / "bad-cleanup-index.sqlite3"
    database = Database(path, runtime_dir=tmp_path)
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX uq_artifact_gc_open_owner")
        connection.execute(
            "CREATE INDEX uq_artifact_gc_open_owner "
            "ON artifact_gc_queue(owner_type, owner_id, relative_path)"
        )
    with pytest.raises(SchemaMigrationError, match="artifact cleanup"):
        Database(path, runtime_dir=tmp_path)
    with sqlite3.connect(path) as connection:
        index = next(
            row for row in connection.execute("PRAGMA index_list(artifact_gc_queue)")
            if row[1] == "uq_artifact_gc_open_owner"
        )
        assert index[2] == 0


def test_marker_present_missing_cleanup_table_fails_without_repair(tmp_path: Path) -> None:
    path = tmp_path / "marker-missing-table.sqlite3"
    database = Database(path, runtime_dir=tmp_path)
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE artifact_gc_queue")
    with pytest.raises(SchemaMigrationError, match="artifact cleanup"):
        Database(path, runtime_dir=tmp_path)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='artifact_gc_queue'"
        ).fetchone()[0] == 0


def test_marker_present_missing_cleanup_constraints_fails_without_repair(
    tmp_path: Path,
) -> None:
    path = tmp_path / "marker-missing-constraints.sqlite3"
    database = Database(path, runtime_dir=tmp_path)
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE artifact_gc_queue")
        connection.execute(
            "CREATE TABLE artifact_gc_queue ("
            "id VARCHAR(36) NOT NULL PRIMARY KEY, owner_type VARCHAR(32) NOT NULL, "
            "owner_id VARCHAR(36) NOT NULL, relative_path TEXT NOT NULL, "
            "path_key TEXT NOT NULL, expected_sha256 VARCHAR(64) NOT NULL, "
            "expected_size_bytes INTEGER NOT NULL, state VARCHAR(20) NOT NULL, "
            "reason VARCHAR(64) NOT NULL, not_before DATETIME NOT NULL, "
            "lease_token VARCHAR(36), lease_expires_at DATETIME, quarantine_path TEXT, "
            "attempt_count INTEGER NOT NULL, last_error_category VARCHAR(64), "
            "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, completed_at DATETIME)"
        )
    with pytest.raises(SchemaMigrationError, match="artifact cleanup"):
        Database(path, runtime_dir=tmp_path)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
            "AND name='uq_artifact_gc_open_owner'"
        ).fetchone()[0] == 0


def test_marker_present_missing_build_token_fails_without_repair(tmp_path: Path) -> None:
    path = tmp_path / "marker-missing-token.sqlite3"
    database = Database(path, runtime_dir=tmp_path)
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER ck_content_packages_build_token_insert")
        connection.execute("DROP TRIGGER ck_content_packages_build_token_update")
        connection.execute("ALTER TABLE content_packages DROP COLUMN build_token")
    with pytest.raises(SchemaMigrationError, match="artifact cleanup"):
        Database(path, runtime_dir=tmp_path)
    with sqlite3.connect(path) as connection:
        assert "build_token" not in {
            row[1] for row in connection.execute("PRAGMA table_info(content_packages)")
        }


def test_half_migration_without_marker_is_retry_safe(tmp_path: Path) -> None:
    path = tmp_path / "half-migration.sqlite3"
    database = Database(path, runtime_dir=tmp_path)
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM workbench_schema_migrations "
            "WHERE name='task8_artifact_quarantine_v1'"
        )
    reopened = Database(path, runtime_dir=tmp_path)
    try:
        with reopened.engine.connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT COUNT(*) FROM workbench_schema_migrations "
                    "WHERE name='task8_artifact_quarantine_v1'"
                )
            ) == 1
    finally:
        reopened.close()
