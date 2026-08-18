from pathlib import Path
import sqlite3
import warnings

import pytest
from sqlalchemy import text

from backend.app.db import Database, SchemaMigrationError
from backend.app.features.content.export import (
    UnsafeContentPath,
    deterministic_zip,
    remove_contained_regular,
)
from backend.app.features.content.models import ArtifactCleanupRecord, ContentPackageRecord
from backend.app.features.content.schemas import ExportCreate
from backend.app.features.content.service import ContentValidationError, _validate_material_bytes
from backend.tests.content.test_hardening import _approval, _image_item


def test_startup_recovery_retains_configured_runtime_and_database_parent_files(
    tmp_path: Path,
) -> None:
    database_dir = tmp_path / "database"
    runtime_dir = tmp_path / "runtime"
    database_dir.mkdir()
    runtime_dir.mkdir()
    service, item, image = _image_item(runtime_dir)
    approved = service.review(item.id, _approval(item, image))
    package = service.export_package(
        item.id, ExportCreate(expected_revision_id=approved.current_revision.id)
    )
    database_path = database_dir / "workbench.sqlite3"
    with service.database.engine.begin() as connection:
        connection.execute(
            text("UPDATE content_packages SET status='building' WHERE id=:id"),
            {"id": package.id},
        )
    service.database.close()
    runtime_database = runtime_dir / "db.sqlite3"
    runtime_database.replace(database_path)
    unrelated = database_dir / package.path
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"unrelated")

    reopened = Database(database_path, runtime_dir=runtime_dir)
    try:
        assert (runtime_dir / package.path).exists()
        assert unrelated.read_bytes() == b"unrelated"
        with reopened.session() as session:
            assert session.get(ContentPackageRecord, package.id).status == "failed"
            cleanup = session.query(ArtifactCleanupRecord).filter_by(
                owner_type="content_package",
                owner_id=package.id,
                relative_path=package.path,
                state="pending",
            ).one()
            assert cleanup.state == "pending"
    finally:
        reopened.close()


def test_remove_contained_regular_is_handle_bound_during_parent_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import backend.app.features.content.export as export_module

    root = tmp_path / "runtime"
    parent = root / "packages"
    parent.mkdir(parents=True)
    target = parent / "orphan.zip"
    target.write_bytes(b"orphan")
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "orphan.zip"
    victim.write_bytes(b"outside-victim")
    swapped = False

    def swap_parent() -> None:
        nonlocal swapped
        if swapped:
            return
        swapped = True
        parent.rename(root / "packages-original")
        try:
            parent.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("directory symlink creation unavailable")

    if hasattr(export_module, "_delete_open_file"):
        real_delete = export_module._delete_open_file

        def racing_delete(handle):
            swap_parent()
            return real_delete(handle)

        monkeypatch.setattr(export_module, "_delete_open_file", racing_delete)
    else:
        real_unlink = Path.unlink

        def racing_unlink(path, *args, **kwargs):
            if path == target:
                swap_parent()
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", racing_unlink)

    result = remove_contained_regular(root, "packages/orphan.zip")
    assert swapped is True
    assert victim.read_bytes() == b"outside-victim"
    assert result is False or not (root / "packages-original" / "orphan.zip").exists()


def test_export_reservation_failure_is_finalized_and_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import backend.app.features.content.service as service_module

    service, item, image = _image_item(tmp_path)
    approved = service.review(item.id, _approval(item, image))
    request = ExportCreate(expected_revision_id=approved.current_revision.id)
    real_write = service_module.write_contained_atomic

    def fail_after_reservation(root, relative, data):
        real_write(root, relative, data)
        raise ContentValidationError("forced post-reservation failure")

    monkeypatch.setattr(service_module, "write_contained_atomic", fail_after_reservation)
    with pytest.raises(ContentValidationError, match="forced"):
        service.export_package(item.id, request)
    package = service.list_packages()[0]
    assert package.status == "failed"
    with service.database.session() as session:
        assert session.get(ContentPackageRecord, package.id).error_detail == "package_build_failed"

    monkeypatch.setattr(service_module, "write_contained_atomic", real_write)
    assert service.export_package(item.id, request).status == "ready"


def _replace_table_sql(path: Path, table: str, transform) -> None:
    with sqlite3.connect(path) as connection:
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0]
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
        connection.execute(transform(sql))
        connection.execute(f"DROP TABLE {table}_old")
        if table == "content_reviews":
            connection.execute(
                "CREATE UNIQUE INDEX uq_review_terminal_revision ON content_reviews(revision_id) "
                "WHERE decision IN ('approve','reject')"
            )
        connection.execute(
            "INSERT INTO content_products (id,opportunity_id,name,target_user,created_at) "
            "VALUES ('p1','missing','bad','bad','2026-08-18')"
        )


@pytest.mark.parametrize(
    ("table", "transform"),
    [
        (
            "content_packages",
            lambda sql: sql.replace(
                "status IN ('building','ready','failed')",
                "status IN ('building','ready','failed') OR 1=1",
            ).replace("content_packages", "content_packages", 1),
        ),
        (
            "content_reviews",
            lambda sql: sql.replace("ON DELETE CASCADE", "ON DELETE SET NULL", 1),
        ),
        (
            "content_reviews",
            lambda sql: sql.replace(
                ", \n\tFOREIGN KEY(content_item_id) REFERENCES content_items (id) ON DELETE CASCADE",
                "",
                1,
            ),
        ),
    ],
)
def test_schema_rejects_weakened_checks_and_foreign_keys_with_data(
    tmp_path: Path, table: str, transform
) -> None:
    path = tmp_path / f"malformed-{table}.sqlite3"
    database = Database(path)
    database.close()
    _replace_table_sql(path, table, transform)
    with pytest.raises(SchemaMigrationError):
        Database(path)


def test_image_pixel_cap_is_checked_before_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    from PIL import Image

    load_called = False

    class HugeImage:
        format = "PNG"
        width = 20_000
        height = 20_000

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def verify(self):
            return None

        def load(self):
            nonlocal load_called
            load_called = True
            raise AssertionError("large image decoded before dimension rejection")

    monkeypatch.setattr(Image, "open", lambda *_args, **_kwargs: HugeImage())
    with pytest.raises(ContentValidationError):
        _validate_material_bytes(b"image", declared="image/png", kind="output_image")
    assert load_called is False


def test_decompression_bomb_warning_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    from PIL import Image

    class WarningImage:
        format = "PNG"
        width = 1
        height = 1

        def __enter__(self):
            warnings.warn("bomb", Image.DecompressionBombWarning)
            return self

        def __exit__(self, *_):
            return False

        def verify(self):
            return None

        def load(self):
            return None

    monkeypatch.setattr(Image, "open", lambda *_args, **_kwargs: WarningImage())
    with pytest.raises(ContentValidationError):
        _validate_material_bytes(b"image", declared="image/png", kind="output_image")


@pytest.mark.parametrize(
    "name",
    [
        "CON.txt", "CON .txt", "LPT1 .png", "CONIN$", "CLOCK$.json",
        "tail.", "trail ", "bad\x7f.txt", "bad\x85.txt",
    ],
)
def test_zip_rejects_windows_unsafe_entry_names(name: str) -> None:
    with pytest.raises(UnsafeContentPath):
        deterministic_zip({name: b"x"}, {"entries": []})


def test_zip_rejects_nfc_casefold_windows_equivalent_collision() -> None:
    with pytest.raises(UnsafeContentPath):
        deterministic_zip({"E\u0301.txt": b"x", "\u00e9.TXT": b"y"}, {"entries": []})
