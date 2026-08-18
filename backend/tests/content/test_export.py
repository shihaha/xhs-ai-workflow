from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import zipfile

import pytest
import httpx

import backend.app.features.content.export as content_export
import backend.app.features.content.service as content_service_module
from backend.app.features.content.export import UnsafeContentPath, deterministic_zip
from backend.app.features.content.models import ContentPackageRecord
from backend.app.features.content.schemas import ContentItemCreate, ExportCreate, MaterialCreate, ReviewCreate
from backend.app.features.content.service import ContentStateError, ContentValidationError
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.tests.content.test_hardening import PNG_1X1
from backend.tests.content.test_workflow import (
    FakeModel,
    UnconfiguredNoCallModel,
    create_product,
    seed_database,
    seeded_service,
)


def _item_with_material(tmp_path: Path):
    service, opportunity_id, evidence_id = seeded_service(tmp_path)
    product_id = create_product(service, opportunity_id)
    source = tmp_path / "materials" / "approved.png"
    source.parent.mkdir()
    source.write_bytes(PNG_1X1)
    material = service.add_material(product_id, MaterialCreate(
        logical_name="approved.png", path="materials/approved.png", media_type="image/png", kind="output_image"
    ))
    item = service.create_content_item(ContentItemCreate(
        product_id=product_id, opportunity_id=opportunity_id,
        template_key="list-v1", evidence_ids=[evidence_id],
        image_material_ids=[material.id], cover_material_id=material.id,
        research_facts=[{"fact": "真实事实", "evidence_ids": [evidence_id]}],
    ))
    return service, item, source


def test_export_is_blocked_before_approved_current_revision(tmp_path: Path) -> None:
    service, item, _ = _item_with_material(tmp_path)
    with pytest.raises(ContentStateError):
        service.export_package(item.id, ExportCreate(expected_revision_id=item.current_revision.id))


def test_deterministic_zip_contains_manifest_sources_reviews_and_material_hashes(tmp_path: Path) -> None:
    service, item, _ = _item_with_material(tmp_path)
    service.review(item.id, ReviewCreate(
        decision="approve", actor="operator", note="人工核对通过", expected_revision_id=item.current_revision.id,
        visual_checks=[{"material_id": item.image_material_ids[0], "passed": True, "observation": "图文清晰一致"}],
    ))
    first = service.export_package(item.id, ExportCreate(expected_revision_id=item.current_revision.id))
    first_bytes = (tmp_path / first.path).read_bytes()
    second = service.export_package(item.id, ExportCreate(expected_revision_id=item.current_revision.id))
    second_bytes = (tmp_path / second.path).read_bytes()

    assert first.id == second.id
    assert first_bytes == second_bytes
    assert first.sha256 == sha256(first_bytes).hexdigest()
    with zipfile.ZipFile(tmp_path / first.path) as archive:
        names = archive.namelist()
        assert names == sorted(names)
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())
        assert all((info.external_attr >> 16) & 0o777 == 0o644 for info in archive.infolist())
        assert "content/final.md" in names
        assert "sources/evidence.json" in names
        assert "reviews/history.json" in names
        assert any(name.startswith("images/") for name in names)
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["automatic_publish"] is False
        assert manifest["materials"][0]["version"] == 1
        assert manifest["materials"][0]["sha256"] == sha256(PNG_1X1).hexdigest()
        assert manifest["images"]["count"] == 1
        for entry in manifest["entries"]:
            assert sha256(archive.read(entry["path"])).hexdigest() == entry["sha256"]


def test_changed_or_symlinked_material_is_rejected_at_export(tmp_path: Path) -> None:
    service, item, source = _item_with_material(tmp_path)
    service.review(item.id, ReviewCreate(
        decision="approve", actor="operator", note="ok", expected_revision_id=item.current_revision.id,
        visual_checks=[{"material_id": item.image_material_ids[0], "passed": True, "observation": "清晰"}],
    ))
    stored_path = service.get_product(item.product_id).materials[0].path
    (tmp_path / stored_path).write_bytes(b"tampered")
    with pytest.raises(ContentValidationError):
        service.export_package(item.id, ExportCreate(expected_revision_id=item.current_revision.id))


def test_export_output_parent_symlink_is_rejected(tmp_path: Path) -> None:
    service, item, _ = _item_with_material(tmp_path)
    service.review(item.id, ReviewCreate(
        decision="approve", actor="operator", note="ok", expected_revision_id=item.current_revision.id,
        visual_checks=[{"material_id": item.image_material_ids[0], "passed": True, "observation": "清晰"}],
    ))
    outside = tmp_path.parent / "outside-packages"
    outside.mkdir(exist_ok=True)
    parent = tmp_path / "content-packages"
    try:
        parent.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation unavailable")
    with pytest.raises(ContentValidationError):
        service.export_package(item.id, ExportCreate(expected_revision_id=item.current_revision.id))
    assert list(outside.iterdir()) == []


def test_zip_rejects_traversal_and_case_collisions(tmp_path: Path) -> None:
    assert deterministic_zip({"a.txt": b"a"}, {"entries": []})
    with pytest.raises(UnsafeContentPath):
        deterministic_zip({"../escape": b"x"}, {"entries": []})
    with pytest.raises(UnsafeContentPath):
        deterministic_zip({"A.txt": b"x", "a.txt": b"y"}, {"entries": []})
    with pytest.raises(UnsafeContentPath):
        deterministic_zip({"MANIFEST.JSON": b"x"}, {"entries": []})


def test_archive_byte_limit_includes_zip_container_overhead(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(content_export, "MAX_PACKAGE_BYTES", 300)
    monkeypatch.setattr(content_export, "MAX_UNCOMPRESSED_PACKAGE_BYTES", 1024, raising=False)

    with pytest.raises(UnsafeContentPath, match="archive size"):
        deterministic_zip({"payload.bin": bytes(range(64))}, {"entries": []})


def test_uncompressed_aggregate_limit_is_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(content_export, "MAX_PACKAGE_BYTES", 10_000)
    monkeypatch.setattr(content_export, "MAX_UNCOMPRESSED_PACKAGE_BYTES", 50, raising=False)

    with pytest.raises(UnsafeContentPath, match="uncompressed size"):
        deterministic_zip({"payload.bin": bytes(range(64))}, {"entries": []})


def test_near_limit_valid_archive_remains_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(content_export, "MAX_PACKAGE_BYTES", 300)
    monkeypatch.setattr(content_export, "MAX_UNCOMPRESSED_PACKAGE_BYTES", 1024, raising=False)
    monkeypatch.setattr(content_service_module, "MAX_PACKAGE_BYTES", 300)
    archive = deterministic_zip({"payload.bin": bytes(range(32))}, {"entries": []})
    assert 250 < len(archive) <= 300

    service, item, _ = _item_with_material(tmp_path)
    relative_path = "content-packages/near-limit.zip"
    target = tmp_path / relative_path
    target.parent.mkdir()
    target.write_bytes(archive)
    with service.database.session() as session:
        session.add(ContentPackageRecord(
            content_item_id=item.id,
            revision_id=item.current_revision.id,
            status="ready",
            path=relative_path,
            sha256=sha256(archive).hexdigest(),
            size_bytes=len(archive),
            build_token=None,
            created_at=datetime.now(UTC).replace(tzinfo=None),
        ))
        session.commit()

    assert service.list_packages()[0].availability == "available"


@pytest.mark.anyio
async def test_http_fresh_database_runs_controlled_adapter_e2e(tmp_path: Path) -> None:
    app = create_app(Settings(runtime_dir=tmp_path, database_path=tmp_path / "api.sqlite3"))
    opportunity_id, evidence_id = seed_database(app.state.database)
    app.state.content_service.model_adapter = FakeModel(evidence_id)
    source = tmp_path / "incoming" / "facts.md"
    source.parent.mkdir()
    source.write_text("approved source", encoding="utf-8")
    image_source = tmp_path / "incoming" / "cover.png"
    image_source.write_bytes(PNG_1X1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        product = await client.post("/api/v1/products", json={
            "name": "API product", "target_user": "operator", "opportunity_id": opportunity_id,
        })
        assert product.status_code == 201
        product_id = product.json()["id"]
        material = await client.post(f"/api/v1/products/{product_id}/materials", json={
            "logical_name": "facts.md", "path": "incoming/facts.md", "media_type": "text/markdown",
        })
        assert material.status_code == 201
        image = await client.post(f"/api/v1/products/{product_id}/materials", json={
            "logical_name": "cover.png", "path": "incoming/cover.png",
            "media_type": "image/png", "kind": "output_image",
        })
        assert image.status_code == 201
        item = await client.post("/api/v1/content-items", json={
            "product_id": product_id, "opportunity_id": opportunity_id,
            "template_key": "list-v1", "evidence_ids": [evidence_id],
            "material_ids": [material.json()["id"]],
            "image_material_ids": [image.json()["id"]], "cover_material_id": image.json()["id"],
            "research_facts": [{"fact": "persisted fact", "evidence_ids": [evidence_id]}],
        })
        assert item.status_code == 201 and item.json()["status"] == "review"
        approved = await client.post(f"/api/v1/content-items/{item.json()['id']}/reviews", json={
            "decision": "approve", "actor": "operator", "note": "checked",
            "expected_revision_id": item.json()["current_revision"]["id"],
            "visual_checks": [{"material_id": image.json()["id"], "passed": True, "observation": "清晰一致"}],
        })
        package = await client.post(f"/api/v1/content-items/{item.json()['id']}/export", json={
            "expected_revision_id": item.json()["current_revision"]["id"]
        })
        listing = await client.get("/api/v1/content-packages")
    assert approved.json()["status"] == "approved"
    assert package.status_code == 201 and package.json()["status"] == "ready"
    assert listing.json() == [package.json()]


@pytest.mark.anyio
async def test_http_unconfigured_model_never_claims_a_draft(tmp_path: Path) -> None:
    app = create_app(Settings(runtime_dir=tmp_path, database_path=tmp_path / "empty.sqlite3"))
    opportunity_id, evidence_id = seed_database(app.state.database)
    unavailable = UnconfiguredNoCallModel()
    app.state.content_service.model_adapter = unavailable
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        product = await client.post("/api/v1/products", json={
            "name": "No model", "target_user": "operator", "opportunity_id": opportunity_id,
        })
        cover = tmp_path / "cover.png"
        cover.write_bytes(PNG_1X1)
        image = await client.post(f"/api/v1/products/{product.json()['id']}/materials", json={
            "logical_name": "cover.png", "path": "cover.png", "media_type": "image/png", "kind": "output_image",
        })
        response = await client.post("/api/v1/content-items", json={
            "product_id": product.json()["id"], "opportunity_id": opportunity_id,
            "template_key": "list-v1", "evidence_ids": [evidence_id],
            "image_material_ids": [image.json()["id"]], "cover_material_id": image.json()["id"],
            "research_facts": [{"fact": "persisted fact", "evidence_ids": [evidence_id]}],
        })
        listing = await client.get("/api/v1/content-items")
    assert response.status_code == 503
    assert unavailable.calls == 0
    assert listing.json() == []


@pytest.mark.anyio
async def test_missing_product_precedes_model_configuration_error(tmp_path: Path) -> None:
    app = create_app(Settings(runtime_dir=tmp_path, database_path=tmp_path / "missing.sqlite3"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/api/v1/content-items", json={
            "product_id": "missing-product",
            "opportunity_id": "missing-opportunity",
            "template_key": "list-v1",
            "evidence_ids": ["rank-item:1"],
            "image_material_ids": ["missing-image"],
            "cover_material_id": "missing-image",
            "research_facts": [{"fact": "persisted fact", "evidence_ids": ["rank-item:1"]}],
        })

    assert response.status_code == 404
