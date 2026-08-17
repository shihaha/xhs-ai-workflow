import json
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
import sqlite3
import struct
import zlib
import zipfile

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from backend.app.db import Database, SchemaMigrationError
from backend.app.features.analysis.models import AnalysisRecord, OpportunityRecord
from backend.app.features.content.models import (
    ArtifactCleanupRecord,
    ContentPackageRecord,
    ProductRecord,
)
from backend.app.features.content.schemas import ExportCreate, MaterialCreate, RegenerateCreate, ReviewCreate
from backend.app.features.content.service import ContentStateError, ContentValidationError
from backend.tests.content.test_hardening import PNG_1X1, _approval, _image_item


def _reject(service, item, image):
    return service.review(item.id, ReviewCreate(
        decision="reject", actor="operator", note="需要返工",
        expected_revision_id=item.current_revision.id,
        visual_checks=[{"material_id": image.id, "passed": False, "observation": "封面信息不完整"}],
    ))


def test_regenerate_post_model_trust_drift_restores_rejected_atomically(tmp_path: Path) -> None:
    service, item, image = _image_item(tmp_path)
    rejected = _reject(service, item, image)
    original = service.model_adapter.generate_structured

    def drifting(request, schema):
        result = original(request, schema)
        with service.database.session() as session:
            product = session.get(ProductRecord, item.product_id)
            opportunity = session.get(OpportunityRecord, product.opportunity_id)
            analysis = session.get(AnalysisRecord, opportunity.analysis_id)
            analysis.status = "needs_human"
            session.commit()
        return result

    service.model_adapter.generate_structured = drifting
    with pytest.raises(ContentValidationError):
        service.regenerate(item.id, RegenerateCreate(expected_revision_id=rejected.current_revision.id))
    persisted = service.get_content_item(item.id)
    assert persisted.status == "rejected"
    assert persisted.current_revision.id == rejected.current_revision.id


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def test_crc_valid_but_undecodable_png_is_rejected(tmp_path: Path) -> None:
    service, item, _ = _image_item(tmp_path)
    product_id = item.product_id
    fake = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        + _chunk(b"IDAT", b"not-a-deflate-stream")
        + _chunk(b"IEND", b"")
    )
    path = tmp_path / "fake.png"
    path.write_bytes(fake)
    with pytest.raises(ContentValidationError):
        service.add_material(product_id, MaterialCreate(
            logical_name="fake.png", path="fake.png", media_type="image/png", kind="output_image"
        ))


def test_corrupt_package_reexport_has_one_builder_and_removes_old_contained_artifact(tmp_path: Path) -> None:
    service, item, image = _image_item(tmp_path)
    approved = service.review(item.id, _approval(item, image))
    request = ExportCreate(expected_revision_id=approved.current_revision.id)
    original = service.export_package(item.id, request)
    old_path = tmp_path / original.path
    old_path.write_bytes(b"corrupt")

    def export():
        try:
            return service.export_package(item.id, request).id
        except ContentStateError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in [pool.submit(export), pool.submit(export)]]
    assert {value for value in results if value != "conflict"} == {original.id}
    assert not old_path.exists()
    assert service.get_package(original.id).availability == "available"


def test_schema_validator_rejects_named_but_nonunique_terminal_index_with_data(tmp_path: Path) -> None:
    path = tmp_path / "bad-index.sqlite3"
    database = Database(path)
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP INDEX uq_review_terminal_revision")
        connection.execute("CREATE INDEX uq_review_terminal_revision ON content_reviews(revision_id)")
        connection.execute(
            "INSERT INTO content_products (id,opportunity_id,name,target_user,created_at) VALUES (?,?,?,?,?)",
            ("p1", "missing", "bad", "bad", "2026-08-18"),
        )
    with pytest.raises(SchemaMigrationError):
        Database(path)


def test_schema_validator_rejects_wrong_terminal_index_predicate_with_data(tmp_path: Path) -> None:
    path = tmp_path / "bad-index-predicate.sqlite3"
    database = Database(path)
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP INDEX uq_review_terminal_revision")
        connection.execute(
            "CREATE UNIQUE INDEX uq_review_terminal_revision ON content_reviews(revision_id) "
            "WHERE decision = 'approve' OR 'reject' = 'reject'"
        )
        connection.execute(
            "INSERT INTO content_products (id,opportunity_id,name,target_user,created_at) VALUES (?,?,?,?,?)",
            ("p1", "missing", "bad", "bad", "2026-08-18"),
        )
    with pytest.raises(SchemaMigrationError):
        Database(path)


def test_package_read_uses_package_limit_not_material_file_limit(tmp_path: Path) -> None:
    service, item, image = _image_item(tmp_path)
    approved = service.review(item.id, _approval(item, image))
    package = service.export_package(item.id, ExportCreate(expected_revision_id=approved.current_revision.id))
    payload = b"x" * (51 * 1024 * 1024)
    (tmp_path / package.path).write_bytes(payload)
    with service.database.session() as session:
        record = session.get(ContentPackageRecord, package.id)
        record.sha256 = sha256(payload).hexdigest()
        record.size_bytes = len(payload)
        session.commit()
    assert service.get_package(package.id).availability == "available"


def test_startup_recovery_retains_building_artifact_and_enqueues_cleanup(tmp_path: Path) -> None:
    service, item, image = _image_item(tmp_path)
    approved = service.review(item.id, _approval(item, image))
    package = service.export_package(item.id, ExportCreate(expected_revision_id=approved.current_revision.id))
    target = tmp_path / package.path
    with service.database.session() as session:
        record = session.get(ContentPackageRecord, package.id)
        record.status = "building"
        session.commit()
    database_path = service.database.database_path
    service.database.close()
    reopened = Database(database_path, runtime_dir=tmp_path)
    try:
        assert target.exists()
        with reopened.session() as session:
            assert session.get(ContentPackageRecord, package.id).status == "failed"
            cleanup = session.query(ArtifactCleanupRecord).filter_by(
                owner_type="content_package",
                owner_id=package.id,
                relative_path=package.path,
            ).one()
            assert cleanup.state == "pending"
    finally:
        reopened.close()


def test_export_includes_visual_checks_and_complete_image_plan(tmp_path: Path) -> None:
    service, item, image = _image_item(tmp_path)
    approved = service.review(item.id, _approval(item, image))
    package = service.export_package(item.id, ExportCreate(expected_revision_id=approved.current_revision.id))
    with zipfile.ZipFile(tmp_path / package.path) as archive:
        history = json.loads(archive.read("reviews/history.json"))
        plan = json.loads(archive.read("content/image-plan.json"))
        manifest = json.loads(archive.read("manifest.json"))
    assert history[-1]["visual_checks"][0]["material_id"] == image.id
    assert plan == [entry.model_dump() for entry in approved.current_revision.image_plan]
    assert any(entry["path"] == "content/image-plan.json" for entry in manifest["entries"])


@pytest.mark.parametrize("name", ["CONIN$", "CONOUT$", "CLOCK$", "bad\x7f.txt", "bad\x85.txt"])
def test_complete_windows_reserved_and_control_names_are_rejected(name: str) -> None:
    with pytest.raises(ValidationError):
        MaterialCreate(logical_name=name, path="safe.txt", media_type="text/plain")


def test_windows_case_equivalent_material_names_share_version_sequence(tmp_path: Path) -> None:
    service, item, _ = _image_item(tmp_path)
    for filename, value in [("Facts.md", "one"), ("facts.md", "two")]:
        (tmp_path / filename).write_text(value, encoding="utf-8")
    first = service.add_material(item.product_id, MaterialCreate(
        logical_name="Facts.md", path="Facts.md", media_type="text/markdown"
    ))
    second = service.add_material(item.product_id, MaterialCreate(
        logical_name="facts.md", path="facts.md", media_type="text/markdown"
    ))
    assert (first.version, second.version) == (1, 2)
