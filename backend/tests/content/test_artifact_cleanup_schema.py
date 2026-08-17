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


def _cleanup_values(*, state: str = "pending", owner_id: str | None = None) -> dict[str, object]:
    return {
        "id": str(uuid4()),
        "owner_type": "content_package",
        "owner_id": owner_id or str(uuid4()),
        "relative_path": "content-packages/item/package.zip",
        "expected_sha256": "a" * 64,
        "expected_size_bytes": 12,
        "state": state,
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
