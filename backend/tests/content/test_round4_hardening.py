from pathlib import Path
import sqlite3

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.db import Database, SchemaMigrationError
from backend.app.features.content.models import ContentPackageRecord
from backend.app.features.content.schemas import ContentItemCreate, ExportCreate, MaterialCreate
from backend.app.features.content.service import ContentStateError
from backend.tests.content.test_hardening import PNG_1X1, _approval, _image_item


def test_material_conflict_cleanup_never_deletes_during_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, item, _ = _image_item(tmp_path)
    source = tmp_path / "incoming" / "facts.txt"
    source.write_text("facts", encoding="utf-8")
    def reject_path_unlink(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("cleanup must not perform a path-based unlink")

    def fail_commit(_session: Session) -> None:
        raise IntegrityError("forced material version conflict", {}, Exception("forced"))

    monkeypatch.setattr(Path, "unlink", reject_path_unlink)
    monkeypatch.setattr(Session, "commit", fail_commit)

    with pytest.raises(ContentStateError, match="Concurrent material version conflict"):
        service.add_material(
            item.product_id,
            MaterialCreate(
                logical_name="facts.txt",
                path="incoming/facts.txt",
                media_type="text/plain",
                kind="source",
            ),
        )

    retained = list((tmp_path / "content-materials" / item.product_id).rglob("source.txt"))
    assert len(retained) == 1
    assert retained[0].read_text(encoding="utf-8") == "facts"


def test_startup_preserves_building_path_owned_by_managed_material(tmp_path: Path) -> None:
    service, item, image = _image_item(tmp_path)
    approved = service.review(item.id, _approval(item, image))
    package = service.export_package(
        item.id, ExportCreate(expected_revision_id=approved.current_revision.id)
    )
    material_path = tmp_path / image.path
    with service.database.session() as session:
        record = session.get(ContentPackageRecord, package.id)
        record.status = "building"
        record.path = image.path
        session.commit()
    database_path = service.database.database_path
    service.database.close()

    reopened = Database(database_path, runtime_dir=tmp_path)
    try:
        assert material_path.read_bytes() == PNG_1X1
        with reopened.session() as session:
            assert session.get(ContentPackageRecord, package.id).status == "failed"
    finally:
        reopened.close()


def test_startup_preserves_building_path_owned_by_ready_package(tmp_path: Path) -> None:
    service, first_item, image = _image_item(tmp_path)
    first_approved = service.review(first_item.id, _approval(first_item, image))
    first_package = service.export_package(
        first_item.id, ExportCreate(expected_revision_id=first_approved.current_revision.id)
    )
    second_item = service.create_content_item(
        ContentItemCreate(
            product_id=first_item.product_id,
            opportunity_id=first_item.opportunity_id,
            template_key=first_item.template_key,
            evidence_ids=list(first_item.evidence_ids),
            image_material_ids=[image.id],
            cover_material_id=image.id,
            research_facts=list(first_item.research_facts),
        )
    )
    second_approved = service.review(second_item.id, _approval(second_item, image))
    second_package = service.export_package(
        second_item.id, ExportCreate(expected_revision_id=second_approved.current_revision.id)
    )
    ready_path = tmp_path / second_package.path
    ready_bytes = ready_path.read_bytes()
    with service.database.session() as session:
        record = session.get(ContentPackageRecord, first_package.id)
        record.status = "building"
        record.path = second_package.path
        session.commit()
    database_path = service.database.database_path
    service.database.close()

    reopened = Database(database_path, runtime_dir=tmp_path)
    try:
        assert ready_path.read_bytes() == ready_bytes
        with reopened.session() as session:
            assert session.get(ContentPackageRecord, first_package.id).status == "failed"
            assert session.get(ContentPackageRecord, second_package.id).status == "ready"
    finally:
        reopened.close()


def test_startup_preserves_unowned_building_path_outside_expected_package_shape(
    tmp_path: Path,
) -> None:
    service, item, image = _image_item(tmp_path)
    approved = service.review(item.id, _approval(item, image))
    package = service.export_package(
        item.id, ExportCreate(expected_revision_id=approved.current_revision.id)
    )
    unrelated = tmp_path / "unrelated" / "artifact.zip"
    unrelated.parent.mkdir()
    unrelated.write_bytes(b"unrelated")
    with service.database.session() as session:
        record = session.get(ContentPackageRecord, package.id)
        record.status = "building"
        record.path = "unrelated/artifact.zip"
        session.commit()
    database_path = service.database.database_path
    service.database.close()

    reopened = Database(database_path, runtime_dir=tmp_path)
    try:
        assert unrelated.read_bytes() == b"unrelated"
        with reopened.session() as session:
            assert session.get(ContentPackageRecord, package.id).status == "failed"
    finally:
        reopened.close()


@pytest.mark.parametrize(
    ("table", "transform"),
    [
        (
            "content_product_materials",
            lambda sql: sql.replace("'source'", "'sou rce'", 1),
        ),
        (
            "content_product_materials",
            lambda sql: sql.replace("*[^0-9a-f]*", "*[^0-9a- f]*", 1),
        ),
    ],
)
def test_schema_literal_changes_in_named_checks_fail_closed_with_data(
    tmp_path: Path, table: str, transform
) -> None:
    path = tmp_path / "literal-check.sqlite3"
    database = Database(path)
    database.close()
    with sqlite3.connect(path) as connection:
        original = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0]
        changed = transform(original)
        assert changed != original
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='table' AND name=?", (changed, table)
        )
        schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute(f"PRAGMA schema_version={schema_version + 1}")
        connection.execute("PRAGMA writable_schema=OFF")
        connection.execute(
            "INSERT INTO content_products (id,opportunity_id,name,target_user,created_at) "
            "VALUES ('p1','missing','bad','bad','2026-08-18')"
        )

    with pytest.raises(SchemaMigrationError):
        Database(path)


def test_schema_literal_change_in_partial_index_fails_closed_with_data(tmp_path: Path) -> None:
    path = tmp_path / "literal-index.sqlite3"
    database = Database(path)
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP INDEX uq_review_terminal_revision")
        connection.execute(
            "CREATE UNIQUE INDEX uq_review_terminal_revision ON content_reviews(revision_id) "
            "WHERE decision IN ('app rove','reject')"
        )
        connection.execute(
            "INSERT INTO content_products (id,opportunity_id,name,target_user,created_at) "
            "VALUES ('p1','missing','bad','bad','2026-08-18')"
        )

    with pytest.raises(SchemaMigrationError):
        Database(path)
