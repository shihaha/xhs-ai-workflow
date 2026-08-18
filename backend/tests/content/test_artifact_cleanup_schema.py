import sqlite3
from datetime import UTC, datetime, timedelta
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
from backend.app.features.content.service import ContentStateError
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
        "quarantine_volume_id": None,
        "quarantine_file_id": None,
        "quarantine_size_bytes": None,
        "quarantine_mtime_ns": None,
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
        assert {
            "quarantine_volume_id", "quarantine_file_id",
            "quarantine_size_bytes", "quarantine_mtime_ns",
        } <= cleanup_columns
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
            assert connection.scalar(
                text(
                    "SELECT COUNT(*) FROM workbench_schema_migrations "
                    "WHERE name='task8_artifact_quarantine_reference_guard_v1'"
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


def test_cleanup_size_and_path_lengths_are_physically_bounded(tmp_path: Path) -> None:
    database = Database(tmp_path / "cleanup-bounds.sqlite3", runtime_dir=tmp_path)
    try:
        with pytest.raises(IntegrityError):
            _insert_cleanup(database, expected_size_bytes=250 * 1024 * 1024 + 1)
        long_path = "a/" + "x" * 999
        with pytest.raises(IntegrityError):
            _insert_cleanup(
                database,
                relative_path=long_path,
                path_key=long_path,
            )
    finally:
        database.close()


def test_quarantine_identity_is_all_or_none_and_required_for_quarantined_state(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "quarantine-identity.sqlite3", runtime_dir=tmp_path)
    try:
        with pytest.raises(IntegrityError):
            _insert_cleanup(
                database,
                state="quarantined",
                quarantine_path="artifacts-quarantine/id/file.zip",
            )
        record = _insert_cleanup(
            database,
            state="quarantined",
            quarantine_path="artifacts-quarantine/id/file.zip",
            quarantine_volume_id=12,
            quarantine_file_id=34,
            quarantine_size_bytes=12,
            quarantine_mtime_ns=56,
        )
        assert record.quarantine_file_id == 34
    finally:
        database.close()


def test_material_and_package_writes_cannot_reference_quarantine_path(
    tmp_path: Path,
) -> None:
    service, item, image = _image_item(tmp_path)
    approved = service.review(item.id, _approval(item, image))
    package = service.export_package(
        item.id, ExportCreate(expected_revision_id=approved.current_revision.id)
    )
    quarantine_path = "artifacts-quarantine/guarded/Café.ZIP"
    try:
        _insert_cleanup(
            service.database,
            state="deleted",
            quarantine_path=quarantine_path,
            quarantine_volume_id=1,
            quarantine_file_id=2,
            quarantine_size_bytes=12,
            quarantine_mtime_ns=3,
        )
        with pytest.raises(IntegrityError, match="quarantine path"):
            with service.database.engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO content_product_materials "
                        "(id,product_id,logical_name,logical_key,version,path,sha256,"
                        "size_bytes,media_type,kind,created_at) VALUES "
                        "(:id,:product_id,'guard.txt','guard.txt',99,:path,:sha256,"
                        "12,'text/plain','source',CURRENT_TIMESTAMP)"
                    ),
                    {
                        "id": str(uuid4()),
                        "product_id": image.product_id,
                        "path": "ARTIFACTS-QUARANTINE/guarded/Cafe\u0301.zip",
                        "sha256": "a" * 64,
                    },
                )
        with pytest.raises(IntegrityError, match="quarantine path"):
            with service.database.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE content_product_materials SET path=:path WHERE id=:id"
                    ),
                    {"path": quarantine_path.lower(), "id": image.id},
                )
        with pytest.raises(IntegrityError, match="quarantine path"):
            with service.database.engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO content_packages "
                        "(id,content_item_id,revision_id,status,path,sha256,size_bytes,"
                        "build_token,created_at,error_detail) VALUES "
                        "(:id,:item_id,:revision_id,'failed',:path,:sha256,0,NULL,"
                        "CURRENT_TIMESTAMP,NULL)"
                    ),
                    {
                        "id": str(uuid4()),
                        "item_id": package.content_item_id,
                        "revision_id": package.revision_id,
                        "path": quarantine_path.lower(),
                        "sha256": "a" * 64,
                    },
                )
        with pytest.raises(IntegrityError, match="quarantine path"):
            with service.database.engine.begin() as connection:
                connection.execute(
                    text("UPDATE content_packages SET path=:path WHERE id=:id"),
                    {"path": quarantine_path.lower(), "id": package.id},
                )
    finally:
        service.database.close()


def test_cleanup_insert_and_update_cannot_capture_existing_references(
    tmp_path: Path,
) -> None:
    service, item, image = _image_item(tmp_path)
    approved = service.review(item.id, _approval(item, image))
    package = service.export_package(
        item.id, ExportCreate(expected_revision_id=approved.current_revision.id)
    )
    material_alias = "Reserved/Cafe\u0301.txt."
    material_quarantine = "reserved/Café.txt"
    try:
        with service.database.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE content_product_materials SET path=:path WHERE id=:id"
                ),
                {"path": material_alias, "id": image.id},
            )
        with pytest.raises(IntegrityError, match="existing artifact reference"):
            _insert_cleanup(
                service.database,
                state="deleted",
                quarantine_path=material_quarantine,
                quarantine_volume_id=1,
                quarantine_file_id=2,
                quarantine_size_bytes=12,
                quarantine_mtime_ns=3,
            )
        with pytest.raises(IntegrityError, match="existing artifact reference"):
            _insert_cleanup(
                service.database,
                state="needs_human",
                quarantine_path=package.path.upper(),
                quarantine_volume_id=4,
                quarantine_file_id=5,
                quarantine_size_bytes=package.size_bytes,
                quarantine_mtime_ns=6,
            )

        pending = _insert_cleanup(service.database)
        with pytest.raises(IntegrityError, match="existing artifact reference"):
            with service.database.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE artifact_gc_queue SET state='needs_human', "
                        "quarantine_path=:path, quarantine_volume_id=7, "
                        "quarantine_file_id=8, quarantine_size_bytes=:size, "
                        "quarantine_mtime_ns=9 WHERE id=:id"
                    ),
                    {
                        "path": package.path.swapcase(),
                        "size": package.size_bytes,
                        "id": pending.id,
                    },
                )

        pending_with_path = _insert_cleanup(
            service.database,
            state="pending",
            quarantine_path=material_quarantine,
            quarantine_volume_id=10,
            quarantine_file_id=11,
            quarantine_size_bytes=12,
            quarantine_mtime_ns=13,
        )
        with pytest.raises(IntegrityError, match="existing artifact reference"):
            with service.database.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE artifact_gc_queue SET state='deleted' WHERE id=:id"
                    ),
                    {"id": pending_with_path.id},
                )
    finally:
        service.database.close()


def test_live_reference_exception_requires_real_conflict_and_complete_identity(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "live-reference-exception.sqlite3", runtime_dir=tmp_path)
    pending = _insert_cleanup(database)
    quarantine_path = f"artifacts-quarantine/{pending.id}/Café.ZIP"
    try:
        with pytest.raises(IntegrityError, match="existing artifact reference"):
            with database.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE artifact_gc_queue SET state='needs_human', "
                        "last_error_category='live_reference', quarantine_path=:path, "
                        "quarantine_volume_id=1, quarantine_file_id=2, "
                        "quarantine_size_bytes=12, quarantine_mtime_ns=3 "
                        "WHERE id=:id"
                    ),
                    {"path": quarantine_path, "id": pending.id},
                )

        with database.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.execute(
                text(
                    "INSERT INTO content_product_materials "
                    "(id,product_id,logical_name,logical_key,version,path,sha256,"
                    "size_bytes,media_type,kind,created_at) VALUES "
                    "(:id,:product_id,'wrong.txt','wrong.txt',1,:path,:sha256,"
                    "13,'text/plain','source',CURRENT_TIMESTAMP)"
                ),
                {
                    "id": str(uuid4()),
                    "product_id": str(uuid4()),
                    "path": quarantine_path.swapcase(),
                    "sha256": "d" * 64,
                },
            )
            connection.commit()

        with pytest.raises(IntegrityError, match="existing artifact reference"):
            with database.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE artifact_gc_queue SET state='needs_human', "
                        "last_error_category='live_reference', quarantine_path=:path, "
                        "quarantine_volume_id=1, quarantine_file_id=2, "
                        "quarantine_size_bytes=12, quarantine_mtime_ns=3 "
                        "WHERE id=:id"
                    ),
                    {"path": quarantine_path, "id": pending.id},
                )

        with database.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.execute(
                text(
                    "INSERT INTO content_product_materials "
                    "(id,product_id,logical_name,logical_key,version,path,sha256,"
                    "size_bytes,media_type,kind,created_at) VALUES "
                    "(:id,:product_id,'guard.txt','guard.txt',1,:path,:sha256,"
                    "12,'text/plain','source',CURRENT_TIMESTAMP)"
                ),
                {
                    "id": str(uuid4()),
                    "product_id": str(uuid4()),
                    "path": (
                        f"ARTIFACTS-QUARANTINE/{pending.id}/Cafe\u0301.zip."
                    ),
                    "sha256": "a" * 64,
                },
            )
            connection.commit()

        with database.engine.begin() as connection:
            result = connection.execute(
                text(
                    "UPDATE artifact_gc_queue SET state='needs_human', "
                    "last_error_category='live_reference', quarantine_path=:path, "
                    "quarantine_volume_id=1, quarantine_file_id=2, "
                    "quarantine_size_bytes=12, quarantine_mtime_ns=3 "
                    "WHERE id=:id"
                ),
                {"path": quarantine_path, "id": pending.id},
            )
            assert result.rowcount == 1
    finally:
        database.close()

    reopened = Database(
        tmp_path / "live-reference-exception.sqlite3", runtime_dir=tmp_path
    )
    try:
        with reopened.session() as session:
            persisted = session.get(content_models.ArtifactCleanupRecord, pending.id)
            assert persisted is not None
            assert persisted.state == "needs_human"
            assert persisted.last_error_category == "live_reference"
    finally:
        reopened.close()


def test_same_package_owner_with_nonoriginal_quarantine_reference_is_live(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "same-owner-new-reference.sqlite3", runtime_dir=tmp_path)
    owner_id = str(uuid4())
    digest = "e" * 64
    pending = _insert_cleanup(
        database,
        owner_type="content_package",
        owner_id=owner_id,
        expected_sha256=digest,
        expected_size_bytes=23,
    )
    quarantine_path = f"artifacts-quarantine/{pending.id}/package.zip"
    try:
        with database.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.execute(
                text(
                    "INSERT INTO content_packages "
                    "(id,content_item_id,revision_id,status,path,sha256,size_bytes,"
                    "build_token,created_at,error_detail) VALUES "
                    "(:id,:item_id,:revision_id,'failed',:path,:sha256,:size,NULL,"
                    "CURRENT_TIMESTAMP,'different_artifact')"
                ),
                {
                    "id": owner_id,
                    "item_id": str(uuid4()),
                    "revision_id": str(uuid4()),
                    "path": quarantine_path.swapcase(),
                    "sha256": digest,
                    "size": 23,
                },
            )
            connection.commit()

        with database.engine.begin() as connection:
            result = connection.execute(
                text(
                    "UPDATE artifact_gc_queue SET state='needs_human', "
                    "last_error_category='live_reference', quarantine_path=:path, "
                    "quarantine_volume_id=1, quarantine_file_id=2, "
                    "quarantine_size_bytes=23, quarantine_mtime_ns=3 WHERE id=:id"
                ),
                {"path": quarantine_path, "id": pending.id},
            )
            assert result.rowcount == 1
    finally:
        database.close()


def _persist_moved_live_reference(
    database: Database, cleanup_id: str, quarantine_path: str, *, size: int
) -> None:
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE artifact_gc_queue SET state='needs_human', "
                "last_error_category='live_reference', quarantine_path=:path, "
                "quarantine_volume_id=1, quarantine_file_id=2, "
                "quarantine_size_bytes=:size, quarantine_mtime_ns=3 WHERE id=:id"
            ),
            {"path": quarantine_path, "size": size, "id": cleanup_id},
        )


def _insert_live_material_support(
    database: Database,
    *,
    material_id: str,
    path: str,
    sha256: str,
    size_bytes: int,
) -> None:
    with database.engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.execute(
            text(
                "INSERT INTO content_product_materials "
                "(id,product_id,logical_name,logical_key,version,path,sha256,"
                "size_bytes,media_type,kind,created_at) VALUES "
                "(:id,:product_id,:name,:key,1,:path,:sha256,:size,"
                "'text/plain','source',CURRENT_TIMESTAMP)"
            ),
            {
                "id": material_id,
                "product_id": str(uuid4()),
                "name": f"{material_id}.txt",
                "key": f"{material_id}.txt",
                "path": path,
                "sha256": sha256,
                "size": size_bytes,
            },
        )
        connection.commit()


@pytest.mark.parametrize(
    ("assignment", "value"),
    [
        ("path=:value", "elsewhere/material.txt"),
        ("sha256=:value", "b" * 64),
        ("size_bytes=:value", 13),
    ],
)
def test_material_support_identity_cannot_mutate_away_from_live_reference(
    tmp_path: Path, assignment: str, value: object
) -> None:
    database = Database(tmp_path / f"material-support-{uuid4()}.sqlite3", runtime_dir=tmp_path)
    cleanup = _insert_cleanup(database)
    material_id = str(uuid4())
    quarantine_path = f"artifacts-quarantine/{cleanup.id}/Café.ZIP"
    try:
        _insert_live_material_support(
            database,
            material_id=material_id,
            path=f"ARTIFACTS-QUARANTINE/{cleanup.id}/Cafe\u0301.zip.",
            sha256="a" * 64,
            size_bytes=12,
        )
        _persist_moved_live_reference(database, cleanup.id, quarantine_path, size=12)

        with pytest.raises(IntegrityError, match="existing artifact reference"):
            with database.engine.begin() as connection:
                connection.execute(
                    text(
                        f"UPDATE content_product_materials SET {assignment} WHERE id=:id"
                    ),
                    {"value": value, "id": material_id},
                )
    finally:
        database.close()


@pytest.mark.parametrize(
    ("assignment", "value"),
    [
        ("path=:value", "elsewhere/package.zip"),
        ("sha256=:value", "c" * 64),
        ("size_bytes=:value", 18),
        ("status=:value", "failed"),
    ],
)
def test_package_support_identity_cannot_mutate_away_from_live_reference(
    tmp_path: Path, assignment: str, value: object
) -> None:
    database = Database(tmp_path / f"package-support-{uuid4()}.sqlite3", runtime_dir=tmp_path)
    package_id = str(uuid4())
    quarantine_path = f"artifacts-quarantine/{uuid4()}/Café.ZIP"
    cleanup = _insert_cleanup(
        database,
        owner_type="content_package",
        owner_id=package_id,
        relative_path=quarantine_path,
        path_key=quarantine_path.lower(),
        expected_sha256="a" * 64,
        expected_size_bytes=17,
    )
    try:
        with database.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.execute(
                text(
                    "INSERT INTO content_packages "
                    "(id,content_item_id,revision_id,status,path,sha256,size_bytes,"
                    "build_token,created_at,error_detail) VALUES "
                    "(:id,:item_id,:revision_id,'ready',:path,:sha256,:size,NULL,"
                    "CURRENT_TIMESTAMP,NULL)"
                ),
                {
                    "id": package_id,
                    "item_id": str(uuid4()),
                    "revision_id": str(uuid4()),
                    "path": quarantine_path.swapcase() + ".",
                    "sha256": "a" * 64,
                    "size": 17,
                },
            )
            connection.commit()
        _persist_moved_live_reference(database, cleanup.id, quarantine_path, size=17)

        with pytest.raises(IntegrityError, match="existing artifact reference"):
            with database.engine.begin() as connection:
                connection.execute(
                    text(f"UPDATE content_packages SET {assignment} WHERE id=:id"),
                    {"value": value, "id": package_id},
                )
    finally:
        database.close()


def test_one_support_reference_may_change_when_another_valid_support_remains(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "remaining-support.sqlite3", runtime_dir=tmp_path)
    cleanup = _insert_cleanup(database)
    quarantine_path = f"artifacts-quarantine/{cleanup.id}/Café.ZIP"
    first_id = str(uuid4())
    second_id = str(uuid4())
    try:
        for material_id in (first_id, second_id):
            _insert_live_material_support(
                database,
                material_id=material_id,
                path=f"ARTIFACTS-QUARANTINE/{cleanup.id}/Cafe\u0301.zip.",
                sha256="a" * 64,
                size_bytes=12,
            )
        _persist_moved_live_reference(database, cleanup.id, quarantine_path, size=12)

        with database.engine.begin() as connection:
            result = connection.execute(
                text(
                    "UPDATE content_product_materials SET sha256=:sha256 WHERE id=:id"
                ),
                {"sha256": "d" * 64, "id": first_id},
            )
            assert result.rowcount == 1
    finally:
        database.close()


def test_material_support_cannot_change_id_into_excluded_cleanup_owner(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "material-support-id.sqlite3", runtime_dir=tmp_path)
    owner_id = str(uuid4())
    cleanup = _insert_cleanup(
        database, owner_type="material", owner_id=owner_id
    )
    support_id = str(uuid4())
    quarantine_path = f"artifacts-quarantine/{cleanup.id}/file.zip"
    try:
        _insert_live_material_support(
            database,
            material_id=support_id,
            path=quarantine_path,
            sha256="a" * 64,
            size_bytes=12,
        )
        _persist_moved_live_reference(database, cleanup.id, quarantine_path, size=12)

        with pytest.raises(IntegrityError, match="existing artifact reference"):
            with database.engine.begin() as connection:
                connection.execute(
                    text("UPDATE content_product_materials SET id=:new_id WHERE id=:id"),
                    {"new_id": owner_id, "id": support_id},
                )
    finally:
        database.close()


def test_package_support_cannot_change_id_into_exact_failed_cleanup_owner(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "package-support-id.sqlite3", runtime_dir=tmp_path)
    owner_id = str(uuid4())
    support_id = str(uuid4())
    quarantine_path = f"artifacts-quarantine/{uuid4()}/package.zip"
    cleanup = _insert_cleanup(
        database,
        owner_type="content_package",
        owner_id=owner_id,
        relative_path=quarantine_path,
        path_key=quarantine_path.lower(),
        expected_sha256="a" * 64,
        expected_size_bytes=12,
    )
    try:
        with database.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.execute(
                text(
                    "INSERT INTO content_packages "
                    "(id,content_item_id,revision_id,status,path,sha256,size_bytes,"
                    "build_token,created_at,error_detail) VALUES "
                    "(:id,:item_id,:revision_id,'failed',:path,:sha256,12,NULL,"
                    "CURRENT_TIMESTAMP,'worker_restart_required')"
                ),
                {
                    "id": support_id,
                    "item_id": str(uuid4()),
                    "revision_id": str(uuid4()),
                    "path": quarantine_path,
                    "sha256": "a" * 64,
                },
            )
            connection.commit()
        _persist_moved_live_reference(database, cleanup.id, quarantine_path, size=12)

        with pytest.raises(IntegrityError, match="existing artifact reference"):
            with database.engine.begin() as connection:
                connection.execute(
                    text("UPDATE content_packages SET id=:new_id WHERE id=:id"),
                    {"new_id": owner_id, "id": support_id},
                )
    finally:
        database.close()


def test_startup_rejects_support_identity_mutated_without_runtime_guard(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mutated-support.sqlite3"
    database = Database(path, runtime_dir=tmp_path)
    cleanup = _insert_cleanup(database)
    material_id = str(uuid4())
    quarantine_path = f"artifacts-quarantine/{cleanup.id}/file.zip"
    _insert_live_material_support(
        database,
        material_id=material_id,
        path=quarantine_path,
        sha256="a" * 64,
        size_bytes=12,
    )
    _persist_moved_live_reference(database, cleanup.id, quarantine_path, size=12)
    with database.engine.begin() as connection:
        trigger_sql = connection.scalar(
            text(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='ck_gc_material_path_update'"
            )
        )
        connection.execute(text("DROP TRIGGER ck_gc_material_path_update"))
        connection.execute(
            text("UPDATE content_product_materials SET sha256=:sha256 WHERE id=:id"),
            {"sha256": "e" * 64, "id": material_id},
        )
        connection.execute(text(trigger_sql))
    database.close()

    with pytest.raises(SchemaMigrationError, match="reference guard"):
        Database(path, runtime_dir=tmp_path)


@pytest.mark.parametrize(
    "mutation",
    ["owner_type", "owner_id", "expected_sha256", "expected_size_bytes"],
)
def test_cleanup_support_identity_cannot_mutate_away_from_live_reference(
    tmp_path: Path, mutation: str
) -> None:
    database = Database(tmp_path / f"cleanup-{mutation}.sqlite3", runtime_dir=tmp_path)
    support_id = str(uuid4())
    cleanup_overrides: dict[str, object] = {}
    value: object
    if mutation == "owner_type":
        cleanup_overrides = {"owner_type": "content_package", "owner_id": support_id}
        value = "material"
    elif mutation == "owner_id":
        cleanup_overrides = {"owner_type": "material", "owner_id": str(uuid4())}
        value = support_id
    elif mutation == "expected_sha256":
        value = "b" * 64
    else:
        value = 13
    cleanup = _insert_cleanup(database, **cleanup_overrides)
    quarantine_path = f"artifacts-quarantine/{cleanup.id}/Café.ZIP"
    try:
        _insert_live_material_support(
            database,
            material_id=support_id,
            path=f"ARTIFACTS-QUARANTINE/{cleanup.id}/Cafe\u0301.zip.",
            sha256="a" * 64,
            size_bytes=12,
        )
        _persist_moved_live_reference(database, cleanup.id, quarantine_path, size=12)

        with pytest.raises(IntegrityError, match="existing artifact reference"):
            with database.engine.begin() as connection:
                connection.execute(
                    text(
                        f"UPDATE artifact_gc_queue SET {mutation}=:value WHERE id=:id"
                    ),
                    {"value": value, "id": cleanup.id},
                )
    finally:
        database.close()


def test_cleanup_original_path_identity_cannot_mutate_away_from_live_reference(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "cleanup-original-path.sqlite3", runtime_dir=tmp_path)
    original_path = "content-packages/item/Café.ZIP"
    cleanup = _insert_cleanup(
        database,
        relative_path=original_path,
        path_key=original_path.lower(),
    )
    quarantine_path = f"artifacts-quarantine/{cleanup.id}/file.zip"
    try:
        _insert_live_material_support(
            database,
            material_id=str(uuid4()),
            path="CONTENT-PACKAGES/item/Cafe\u0301.zip.",
            sha256="a" * 64,
            size_bytes=12,
        )
        _persist_moved_live_reference(database, cleanup.id, quarantine_path, size=12)

        replacement = "content-packages/item/other.zip"
        with pytest.raises(IntegrityError, match="existing artifact reference"):
            with database.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE artifact_gc_queue SET relative_path=:path, "
                        "path_key=:key WHERE id=:id"
                    ),
                    {"path": replacement, "key": replacement, "id": cleanup.id},
                )
    finally:
        database.close()


def test_cleanup_owner_may_change_when_another_valid_support_remains(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "cleanup-owner-second-support.sqlite3", runtime_dir=tmp_path)
    first_id = str(uuid4())
    cleanup = _insert_cleanup(
        database, owner_type="material", owner_id=str(uuid4())
    )
    quarantine_path = f"artifacts-quarantine/{cleanup.id}/file.zip"
    try:
        for support_id in (first_id, str(uuid4())):
            _insert_live_material_support(
                database,
                material_id=support_id,
                path=quarantine_path,
                sha256="a" * 64,
                size_bytes=12,
            )
        _persist_moved_live_reference(database, cleanup.id, quarantine_path, size=12)

        with database.engine.begin() as connection:
            result = connection.execute(
                text("UPDATE artifact_gc_queue SET owner_id=:owner_id WHERE id=:id"),
                {"owner_id": first_id, "id": cleanup.id},
            )
            assert result.rowcount == 1
    finally:
        database.close()


def test_startup_rejects_cleanup_identity_mutated_without_runtime_guard(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mutated-cleanup-support.sqlite3"
    database = Database(path, runtime_dir=tmp_path)
    cleanup = _insert_cleanup(database)
    quarantine_path = f"artifacts-quarantine/{cleanup.id}/file.zip"
    _insert_live_material_support(
        database,
        material_id=str(uuid4()),
        path=quarantine_path,
        sha256="a" * 64,
        size_bytes=12,
    )
    _persist_moved_live_reference(database, cleanup.id, quarantine_path, size=12)
    with database.engine.begin() as connection:
        trigger_sql = connection.scalar(
            text(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='ck_gc_cleanup_reference_update'"
            )
        )
        connection.execute(text("DROP TRIGGER ck_gc_cleanup_reference_update"))
        connection.execute(
            text(
                "UPDATE artifact_gc_queue SET expected_sha256=:sha256 WHERE id=:id"
            ),
            {"sha256": "c" * 64, "id": cleanup.id},
        )
        connection.execute(text(trigger_sql))
    database.close()

    with pytest.raises(SchemaMigrationError, match="reference guard"):
        Database(path, runtime_dir=tmp_path)


def test_reference_guard_marker_rejects_old_cleanup_update_trigger(
    tmp_path: Path,
) -> None:
    path = tmp_path / "old-cleanup-update-trigger.sqlite3"
    database = Database(path, runtime_dir=tmp_path)
    database.close()
    with sqlite3.connect(path) as connection:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='ck_gc_cleanup_reference_update'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER ck_gc_cleanup_reference_update")
        connection.execute(
            trigger_sql.replace(
                "BEFORE UPDATE OF owner_type, owner_id, relative_path, path_key, "
                "expected_sha256, expected_size_bytes, quarantine_path, state,",
                "BEFORE UPDATE OF quarantine_path, state,",
            )
        )

    with pytest.raises(SchemaMigrationError, match="reference guard"):
        Database(path, runtime_dir=tmp_path)


def test_startup_rejects_fabricated_live_reference_exception(tmp_path: Path) -> None:
    path = tmp_path / "fabricated-live-reference.sqlite3"
    database = Database(path, runtime_dir=tmp_path)
    pending = _insert_cleanup(database)
    with database.engine.begin() as connection:
        trigger_sql = connection.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='ck_gc_cleanup_reference_update'"
            )
        ).scalar_one()
        connection.execute(text("DROP TRIGGER ck_gc_cleanup_reference_update"))
        connection.execute(
            text(
                "UPDATE artifact_gc_queue SET state='needs_human', "
                "last_error_category='live_reference', quarantine_path=:path, "
                "quarantine_volume_id=1, quarantine_file_id=2, "
                "quarantine_size_bytes=12, quarantine_mtime_ns=3 WHERE id=:id"
            ),
            {
                "path": f"artifacts-quarantine/{pending.id}/file.zip",
                "id": pending.id,
            },
        )
        connection.execute(text(trigger_sql))
    database.close()

    with pytest.raises(SchemaMigrationError, match="reference guard"):
        Database(path, runtime_dir=tmp_path)


def test_failed_package_owner_cannot_authorize_fabricated_moved_live_reference(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "failed-owner-not-live.sqlite3", runtime_dir=tmp_path)
    owner_id = str(uuid4())
    relative_path = "content-packages/item/Caf\u00e9.ZIP"
    digest = "b" * 64
    cleanup = _insert_cleanup(
        database,
        owner_type="content_package",
        owner_id=owner_id,
        relative_path=relative_path,
        path_key=relative_path.lower(),
        expected_sha256=digest,
        expected_size_bytes=17,
    )
    try:
        with database.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.execute(
                text(
                    "INSERT INTO content_packages "
                    "(id,content_item_id,revision_id,status,path,sha256,size_bytes,"
                    "build_token,created_at,error_detail) VALUES "
                    "(:id,:item_id,:revision_id,'failed',:path,:sha256,:size,NULL,"
                    "CURRENT_TIMESTAMP,'worker_restart_required')"
                ),
                {
                    "id": owner_id,
                    "item_id": str(uuid4()),
                    "revision_id": str(uuid4()),
                    "path": "CONTENT-PACKAGES/ITEM/Cafe\u0301.zip.",
                    "sha256": digest,
                    "size": 17,
                },
            )
            connection.commit()

        with pytest.raises(IntegrityError, match="existing artifact reference"):
            with database.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE artifact_gc_queue SET state='needs_human', "
                        "last_error_category='live_reference', "
                        "quarantine_path=:path, quarantine_volume_id=111, "
                        "quarantine_file_id=222, quarantine_size_bytes=17, "
                        "quarantine_mtime_ns=333 WHERE id=:id"
                    ),
                    {
                        "path": f"artifacts-quarantine/{cleanup.id}/fake.zip",
                        "id": cleanup.id,
                    },
                )
    finally:
        database.close()


def test_material_owner_original_cannot_authorize_fabricated_moved_fact(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "material-owner-not-moved.sqlite3", runtime_dir=tmp_path)
    owner_id = str(uuid4())
    digest = "f" * 64
    cleanup = _insert_cleanup(
        database,
        owner_type="material",
        owner_id=owner_id,
        relative_path="content-materials/item/Caf\u00e9.PNG",
        path_key="content-materials/item/caf\u00e9.png",
        expected_sha256=digest,
        expected_size_bytes=29,
    )
    try:
        with database.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.execute(
                text(
                    "INSERT INTO content_product_materials "
                    "(id,product_id,logical_name,logical_key,version,path,sha256,"
                    "size_bytes,media_type,kind,created_at) VALUES "
                    "(:id,:product_id,'image.png','image.png',1,:path,:sha256,"
                    "29,'image/png','output_image',CURRENT_TIMESTAMP)"
                ),
                {
                    "id": owner_id,
                    "product_id": str(uuid4()),
                    "path": "CONTENT-MATERIALS/ITEM/Cafe\u0301.png.",
                    "sha256": digest,
                },
            )
            connection.commit()

        with pytest.raises(IntegrityError, match="existing artifact reference"):
            with database.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE artifact_gc_queue SET state='needs_human', "
                        "last_error_category='live_reference', "
                        "quarantine_path=:path, quarantine_volume_id=111, "
                        "quarantine_file_id=222, quarantine_size_bytes=29, "
                        "quarantine_mtime_ns=333 WHERE id=:id"
                    ),
                    {
                        "path": f"artifacts-quarantine/{cleanup.id}/fake.png",
                        "id": cleanup.id,
                    },
                )
    finally:
        database.close()


def test_startup_rejects_failed_package_owner_as_only_moved_live_reference(
    tmp_path: Path,
) -> None:
    path = tmp_path / "failed-owner-startup.sqlite3"
    database = Database(path, runtime_dir=tmp_path)
    owner_id = str(uuid4())
    relative_path = "content-packages/item/Caf\u00e9.ZIP"
    digest = "c" * 64
    cleanup = _insert_cleanup(
        database,
        owner_type="content_package",
        owner_id=owner_id,
        relative_path=relative_path,
        path_key=relative_path.lower(),
        expected_sha256=digest,
        expected_size_bytes=19,
    )
    with database.engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.execute(
            text(
                "INSERT INTO content_packages "
                "(id,content_item_id,revision_id,status,path,sha256,size_bytes,"
                "build_token,created_at,error_detail) VALUES "
                "(:id,:item_id,:revision_id,'failed',:path,:sha256,:size,NULL,"
                "CURRENT_TIMESTAMP,'worker_restart_required')"
            ),
            {
                "id": owner_id,
                "item_id": str(uuid4()),
                "revision_id": str(uuid4()),
                "path": "CONTENT-PACKAGES/ITEM/Cafe\u0301.zip.",
                "sha256": digest,
                "size": 19,
            },
        )
        connection.commit()
    with database.engine.begin() as connection:
        trigger_sql = connection.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='ck_gc_cleanup_reference_update'"
            )
        ).scalar_one()
        connection.execute(text("DROP TRIGGER ck_gc_cleanup_reference_update"))
        connection.execute(
            text(
                "UPDATE artifact_gc_queue SET state='needs_human', "
                "last_error_category='live_reference', quarantine_path=:path, "
                "quarantine_volume_id=111, quarantine_file_id=222, "
                "quarantine_size_bytes=19, quarantine_mtime_ns=333 WHERE id=:id"
            ),
            {
                "path": f"artifacts-quarantine/{cleanup.id}/fake.zip",
                "id": cleanup.id,
            },
        )
        connection.execute(text(trigger_sql))
    database.close()

    with pytest.raises(SchemaMigrationError, match="reference guard"):
        Database(path, runtime_dir=tmp_path)


def test_reference_guard_marker_detects_missing_reverse_trigger(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing-reverse-trigger.sqlite3"
    database = Database(path, runtime_dir=tmp_path)
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DROP TRIGGER IF EXISTS ck_gc_cleanup_reference_update"
        )

    with pytest.raises(SchemaMigrationError, match="reference guard"):
        Database(path, runtime_dir=tmp_path)


def test_content_service_rejects_existing_package_that_is_quarantined(
    tmp_path: Path,
) -> None:
    service, item, image = _image_item(tmp_path)
    approved = service.review(item.id, _approval(item, image))
    package = service.export_package(
        item.id, ExportCreate(expected_revision_id=approved.current_revision.id)
    )
    try:
        with service.database.engine.begin() as connection:
            connection.execute(
                text("DROP TRIGGER ck_gc_cleanup_reference_insert")
            )
        _insert_cleanup(
            service.database,
            state="deleted",
            quarantine_path=package.path,
            quarantine_volume_id=1,
            quarantine_file_id=2,
            quarantine_size_bytes=package.size_bytes,
            quarantine_mtime_ns=3,
        )

        with pytest.raises(ContentStateError, match="quarantine"):
            service.export_package(
                item.id,
                ExportCreate(expected_revision_id=approved.current_revision.id),
            )
    finally:
        service.database.close()


def test_reference_guard_marker_detects_trigger_tampering(tmp_path: Path) -> None:
    path = tmp_path / "tampered-reference-trigger.sqlite3"
    database = Database(path, runtime_dir=tmp_path)
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER ck_gc_material_path_insert")

    with pytest.raises(SchemaMigrationError, match="reference guard"):
        Database(path, runtime_dir=tmp_path)

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='trigger' AND name='ck_gc_material_path_insert'"
        ).fetchone()[0] == 0


def test_reference_guard_marker_rejects_weak_trigger(tmp_path: Path) -> None:
    path = tmp_path / "weak-reference-trigger.sqlite3"
    database = Database(path, runtime_dir=tmp_path)
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER ck_gc_material_path_insert")
        connection.execute(
            "CREATE TRIGGER ck_gc_material_path_insert "
            "BEFORE INSERT ON content_product_materials WHEN 0 BEGIN "
            "SELECT RAISE(ABORT, 'disabled'); END"
        )

    with pytest.raises(SchemaMigrationError, match="reference guard"):
        Database(path, runtime_dir=tmp_path)


def test_reference_guard_marker_rejects_path_only_update_trigger(
    tmp_path: Path,
) -> None:
    path = tmp_path / "path-only-update-trigger.sqlite3"
    database = Database(path, runtime_dir=tmp_path)
    database.close()
    with sqlite3.connect(path) as connection:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='ck_gc_material_path_update'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER ck_gc_material_path_update")
        connection.execute(
            trigger_sql.replace(
                "AFTER UPDATE OF id, path, sha256, size_bytes",
                "AFTER UPDATE OF path",
            )
        )

    with pytest.raises(SchemaMigrationError, match="reference guard"):
        Database(path, runtime_dir=tmp_path)


def test_reference_guard_migration_fails_closed_on_historical_conflict(
    tmp_path: Path,
) -> None:
    service, item, image = _image_item(tmp_path)
    path = service.database.database_path
    approved = service.review(item.id, _approval(item, image))
    package = service.export_package(
        item.id, ExportCreate(expected_revision_id=approved.current_revision.id)
    )
    with service.database.engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM workbench_schema_migrations "
                "WHERE name='task8_artifact_quarantine_reference_guard_v1'"
            )
        )
        for name in (
            "ck_gc_material_path_insert",
            "ck_gc_material_path_update",
            "ck_gc_package_path_insert",
            "ck_gc_package_path_update",
            "ck_gc_cleanup_reference_insert",
            "ck_gc_cleanup_reference_update",
        ):
            connection.execute(text(f"DROP TRIGGER {name}"))
    _insert_cleanup(
        service.database,
        state="deleted",
        quarantine_path=package.path.upper(),
        quarantine_volume_id=1,
        quarantine_file_id=2,
        quarantine_size_bytes=package.size_bytes,
        quarantine_mtime_ns=3,
    )
    service.database.close()

    with pytest.raises(SchemaMigrationError, match="Historical quarantine"):
        Database(path, runtime_dir=tmp_path)

    assert (tmp_path / package.path).exists()


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
    future_due = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=24)
    with service.database.engine.begin() as connection:
        connection.execute(
            text("UPDATE content_packages SET status='building' WHERE id=:id"),
            {"id": package.id},
        )
        connection.execute(
            text(
                "UPDATE artifact_gc_queue SET state='pending', not_before=:future_due "
                "WHERE owner_type='content_package' AND owner_id=:id"
            ),
            {"id": package.id, "future_due": future_due},
        )
    database_path = service.database.database_path
    service.database.close()

    recovery_started = datetime.now(UTC).replace(tzinfo=None)
    upgraded = Database(database_path, runtime_dir=tmp_path)
    try:
        with upgraded.session() as session:
            recovered = session.get(ContentPackageRecord, package.id)
            cleanup = session.query(content_models.ArtifactCleanupRecord).filter_by(
                owner_type="content_package", owner_id=package.id,
                relative_path=package.path, state="pending"
            ).one()
            assert recovered.status == "failed"
            assert recovered.error_detail == "worker_restart_required"
            assert cleanup.state == "pending"
            assert cleanup.not_before <= datetime.now(UTC).replace(tzinfo=None)
            assert cleanup.not_before >= recovery_started
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
                owner_id=package.id, state="pending"
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
