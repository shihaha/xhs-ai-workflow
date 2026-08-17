from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.db import Database, SchemaMigrationError
from backend.app.features.content.models import (
    ContentItemRecord,
    ContentPackageRecord,
    ProductMaterialRecord,
)
from backend.app.features.content.schemas import ExportCreate, MaterialCreate
from backend.app.features.content.service import ContentStateError, ContentValidationError
from backend.tests.content.test_hardening import PNG_1X1, _approval, _image_item


def _new_source(tmp_path: Path) -> MaterialCreate:
    source = tmp_path / "incoming" / "facts.txt"
    source.write_text("facts", encoding="utf-8")
    return MaterialCreate(
        logical_name="facts.txt",
        path="incoming/facts.txt",
        media_type="text/plain",
        kind="source",
    )


def test_generic_database_failure_after_material_write_removes_only_unpersisted_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import backend.app.features.content.service as service_module

    service, item, _ = _image_item(tmp_path)
    payload = _new_source(tmp_path)
    removed: list[str] = []
    real_commit = Session.commit

    def fail_commit(session: Session) -> None:
        if any(isinstance(value, ProductMaterialRecord) for value in session.identity_map.values()):
            raise SQLAlchemyError("generic commit failure")
        real_commit(session)

    def record_remove(_root: Path, relative: str, **_kwargs: object) -> bool:
        removed.append(relative)
        return True

    monkeypatch.setattr(Session, "commit", fail_commit)
    monkeypatch.setattr(service_module, "remove_contained_regular", record_remove)

    with pytest.raises(SQLAlchemyError, match="generic commit failure"):
        service.add_material(item.product_id, payload)

    assert len(removed) == 1
    with service.database.session() as session:
        assert session.scalar(select(func.count(ProductMaterialRecord.id)).where(
            ProductMaterialRecord.logical_name == "facts.txt"
        )) == 0


def test_commit_then_raise_does_not_delete_persisted_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import backend.app.features.content.service as service_module

    service, item, _ = _image_item(tmp_path)
    payload = _new_source(tmp_path)
    removed: list[str] = []
    real_commit = Session.commit

    def commit_then_raise(session: Session) -> None:
        real_commit(session)
        raise SQLAlchemyError("ambiguous commit acknowledgement")

    monkeypatch.setattr(Session, "commit", commit_then_raise)
    monkeypatch.setattr(
        service_module,
        "remove_contained_regular",
        lambda _root, relative, **_kwargs: removed.append(relative) or True,
    )

    with pytest.raises(SQLAlchemyError, match="ambiguous commit acknowledgement"):
        service.add_material(item.product_id, payload)

    assert removed == []
    with service.database.engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT COUNT(*) FROM content_product_materials WHERE logical_name='facts.txt'"
        ).scalar_one() == 1


def test_path_validation_failure_after_material_creation_cleans_unpersisted_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import backend.app.features.content.service as service_module

    service, item, _ = _image_item(tmp_path)
    payload = _new_source(tmp_path)
    real_write = service_module.write_contained_atomic
    removed: list[str] = []

    def write_then_reject(root: Path, relative: str, data: bytes) -> None:
        real_write(root, relative, data)
        raise service_module.UnsafeContentPath("post-create validation failure")

    monkeypatch.setattr(service_module, "write_contained_atomic", write_then_reject)
    monkeypatch.setattr(
        service_module,
        "remove_contained_regular",
        lambda _root, relative, **_kwargs: removed.append(relative) or True,
    )

    with pytest.raises(ContentValidationError, match="post-create validation failure"):
        service.add_material(item.product_id, payload)

    assert len(removed) == 1


@pytest.mark.parametrize("conflict_kind", ["material", "ready", "building", "failed"])
def test_startup_preserves_windows_equivalent_path_referenced_by_any_record(
    tmp_path: Path, conflict_kind: str
) -> None:
    service, item, image = _image_item(tmp_path)
    approved = service.review(item.id, _approval(item, image))
    package = service.export_package(
        item.id, ExportCreate(expected_revision_id=approved.current_revision.id)
    )
    artifact = tmp_path / package.path
    original = artifact.read_bytes()
    case_variant = package.path.upper()
    with service.database.session() as session:
        stranded = session.get(ContentPackageRecord, package.id)
        stranded.status = "building"
        if conflict_kind == "material":
            session.get(ProductMaterialRecord, image.id).path = case_variant
        session.commit()
    if conflict_kind != "material":
        with service.database.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.exec_driver_sql(
                "INSERT INTO content_packages "
                "(id,content_item_id,revision_id,status,path,sha256,size_bytes,created_at,error_detail) "
                "VALUES (?,?,?,?,?,?,0,?,NULL)",
                (
                    str(uuid4()), item.id, str(uuid4()), conflict_kind,
                    case_variant, "0" * 64, "2026-08-18 00:00:00",
                ),
            )
    database_path = service.database.database_path
    service.database.close()

    reopened = Database(database_path, runtime_dir=tmp_path)
    try:
        assert artifact.read_bytes() == original
    finally:
        reopened.close()


@pytest.mark.parametrize(
    ("package_id", "item_id"),
    [
        ("AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA", None),
        ("not-a-uuid", None),
        (None, "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB"),
    ],
)
def test_startup_preserves_artifact_for_noncanonical_database_identity(
    tmp_path: Path, package_id: str | None, item_id: str | None
) -> None:
    service, item, image = _image_item(tmp_path)
    approved = service.review(item.id, _approval(item, image))
    package = service.export_package(
        item.id, ExportCreate(expected_revision_id=approved.current_revision.id)
    )
    artifact = tmp_path / package.path
    original = artifact.read_bytes()
    changed_package_id = package_id or package.id
    changed_item_id = item_id or item.id
    changed_path = f"content-packages/{changed_item_id}/{changed_package_id}.zip"
    changed_artifact = tmp_path / changed_path
    changed_artifact.parent.mkdir(parents=True, exist_ok=True)
    if changed_artifact != artifact:
        changed_artifact.write_bytes(original)
    with service.database.engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql(
            "UPDATE content_packages SET id=?, content_item_id=?, status='building', path=? WHERE id=?",
            (changed_package_id, changed_item_id, changed_path, package.id),
        )
    database_path = service.database.database_path
    service.database.close()

    if package_id is not None:
        with pytest.raises(SchemaMigrationError, match="manual migration"):
            Database(database_path, runtime_dir=tmp_path)
        assert changed_artifact.read_bytes() == original
        return

    reopened = Database(database_path, runtime_dir=tmp_path)
    try:
        assert changed_artifact.read_bytes() == original
    finally:
        reopened.close()


@pytest.mark.parametrize("mutation", ["path", "status", "other_builder", "item"])
def test_package_finalizer_cas_rejects_mid_build_takeover_without_overwriting_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    import backend.app.features.content.service as service_module

    service, item, image = _image_item(tmp_path)
    approved = service.review(item.id, _approval(item, image))
    request = ExportCreate(expected_revision_id=approved.current_revision.id)
    real_write = service_module.write_contained_atomic
    built_paths: list[str] = []

    def mutate_reservation_after_write(root: Path, relative: str, data: bytes) -> None:
        real_write(root, relative, data)
        built_paths.append(relative)
        with service.database.session() as session:
            package = session.query(ContentPackageRecord).one()
            if mutation in {"path", "other_builder"}:
                package.path = f"content-packages/{item.id}/{uuid4()}.zip"
            elif mutation == "status":
                package.status = "failed"
            else:
                session.get(ContentItemRecord, item.id).status = "rejected"
            session.commit()
        if mutation == "other_builder":
            with service.database.engine.begin() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
                connection.exec_driver_sql(
                    "INSERT INTO content_packages "
                    "(id,content_item_id,revision_id,status,path,sha256,size_bytes,created_at,error_detail) "
                    "VALUES (?,?,?,?,?,?,0,?,NULL)",
                    (
                        str(uuid4()), item.id, str(uuid4()), "building", relative,
                        "0" * 64, "2026-08-18 00:00:00",
                    ),
                )

    monkeypatch.setattr(service_module, "write_contained_atomic", mutate_reservation_after_write)

    with pytest.raises(ContentStateError, match="changed before finalization"):
        service.export_package(item.id, request)

    assert len(built_paths) == 1
    if mutation in {"path", "item"}:
        assert not (tmp_path / built_paths[0]).exists()
    else:
        assert (tmp_path / built_paths[0]).exists()
    with service.database.session() as session:
        packages = session.query(ContentPackageRecord).all()
        assert all(package.status != "ready" for package in packages)
        expected_item_status = "rejected" if mutation == "item" else "approved"
        assert session.get(ContentItemRecord, item.id).status == expected_item_status
