from hashlib import sha256
import json
from pathlib import Path
import zipfile

import pytest
import httpx

from backend.app.features.content.export import UnsafeContentPath, deterministic_zip
from backend.app.features.content.schemas import ContentItemCreate, MaterialCreate, ReviewCreate
from backend.app.features.content.service import ContentStateError, ContentValidationError
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.tests.content.test_workflow import FakeModel, create_product, seed_database, seeded_service


def _item_with_material(tmp_path: Path):
    service, opportunity_id, evidence_id = seeded_service(tmp_path)
    product_id = create_product(service, opportunity_id)
    source = tmp_path / "materials" / "approved.png"
    source.parent.mkdir()
    source.write_bytes(b"real-image-bytes")
    material = service.add_material(product_id, MaterialCreate(
        logical_name="approved.png", path="materials/approved.png", media_type="image/png"
    ))
    item = service.create_content_item(ContentItemCreate(
        product_id=product_id, opportunity_id=opportunity_id,
        template_key="list-v1", evidence_ids=[evidence_id], material_ids=[material.id],
        research_facts=[{"fact": "真实事实", "evidence_ids": [evidence_id]}],
    ))
    return service, item, source


def test_export_is_blocked_before_approved_current_revision(tmp_path: Path) -> None:
    service, item, _ = _item_with_material(tmp_path)
    with pytest.raises(ContentStateError):
        service.export_package(item.id)


def test_deterministic_zip_contains_manifest_sources_reviews_and_material_hashes(tmp_path: Path) -> None:
    service, item, _ = _item_with_material(tmp_path)
    service.review(item.id, ReviewCreate(
        decision="approve", actor="operator", note="人工核对通过"
    ))
    first = service.export_package(item.id)
    first_bytes = (tmp_path / first.path).read_bytes()
    second = service.export_package(item.id)
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
        assert any(name.startswith("materials/") for name in names)
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["automatic_publish"] is False
        assert manifest["materials"][0]["version"] == 1
        assert manifest["materials"][0]["sha256"] == sha256(b"real-image-bytes").hexdigest()
        for entry in manifest["entries"]:
            assert sha256(archive.read(entry["path"])).hexdigest() == entry["sha256"]


def test_changed_or_symlinked_material_is_rejected_at_export(tmp_path: Path) -> None:
    service, item, source = _item_with_material(tmp_path)
    service.review(item.id, ReviewCreate(decision="approve", actor="operator", note="ok"))
    stored_path = service.get_product(item.product_id).materials[0].path
    (tmp_path / stored_path).write_bytes(b"tampered")
    with pytest.raises(ContentValidationError):
        service.export_package(item.id)


def test_export_output_parent_symlink_is_rejected(tmp_path: Path) -> None:
    service, item, _ = _item_with_material(tmp_path)
    service.review(item.id, ReviewCreate(decision="approve", actor="operator", note="ok"))
    outside = tmp_path.parent / "outside-packages"
    outside.mkdir(exist_ok=True)
    parent = tmp_path / "content-packages"
    try:
        parent.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation unavailable")
    with pytest.raises(ContentValidationError):
        service.export_package(item.id)
    assert list(outside.iterdir()) == []


def test_zip_rejects_traversal_and_case_collisions(tmp_path: Path) -> None:
    assert deterministic_zip({"a.txt": b"a"}, {"entries": []})
    with pytest.raises(UnsafeContentPath):
        deterministic_zip({"../escape": b"x"}, {"entries": []})
    with pytest.raises(UnsafeContentPath):
        deterministic_zip({"A.txt": b"x", "a.txt": b"y"}, {"entries": []})
    with pytest.raises(UnsafeContentPath):
        deterministic_zip({"MANIFEST.JSON": b"x"}, {"entries": []})


@pytest.mark.anyio
async def test_http_fresh_database_runs_controlled_adapter_e2e(tmp_path: Path) -> None:
    app = create_app(Settings(runtime_dir=tmp_path, database_path=tmp_path / "api.sqlite3"))
    opportunity_id, evidence_id = seed_database(app.state.database)
    app.state.content_service.model_adapter = FakeModel(evidence_id)
    source = tmp_path / "incoming" / "facts.md"
    source.parent.mkdir()
    source.write_text("approved source", encoding="utf-8")
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
        item = await client.post("/api/v1/content-items", json={
            "product_id": product_id, "opportunity_id": opportunity_id,
            "template_key": "list-v1", "evidence_ids": [evidence_id],
            "material_ids": [material.json()["id"]],
            "research_facts": [{"fact": "persisted fact", "evidence_ids": [evidence_id]}],
        })
        assert item.status_code == 201 and item.json()["status"] == "review"
        approved = await client.post(f"/api/v1/content-items/{item.json()['id']}/reviews", json={
            "decision": "approve", "actor": "operator", "note": "checked",
        })
        package = await client.post(f"/api/v1/content-items/{item.json()['id']}/export")
        listing = await client.get("/api/v1/content-packages")
    assert approved.json()["status"] == "approved"
    assert package.status_code == 201 and package.json()["status"] == "ready"
    assert listing.json() == [package.json()]


@pytest.mark.anyio
async def test_http_unconfigured_model_never_claims_a_draft(tmp_path: Path) -> None:
    app = create_app(Settings(runtime_dir=tmp_path, database_path=tmp_path / "empty.sqlite3"))
    opportunity_id, evidence_id = seed_database(app.state.database)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        product = await client.post("/api/v1/products", json={
            "name": "No model", "target_user": "operator", "opportunity_id": opportunity_id,
        })
        response = await client.post("/api/v1/content-items", json={
            "product_id": product.json()["id"], "opportunity_id": opportunity_id,
            "template_key": "list-v1", "evidence_ids": [evidence_id],
            "research_facts": [{"fact": "persisted fact", "evidence_ids": [evidence_id]}],
        })
        listing = await client.get("/api/v1/content-items")
    assert response.status_code == 503
    assert listing.json() == []
