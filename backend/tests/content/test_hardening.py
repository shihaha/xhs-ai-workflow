import base64
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Lock

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from backend.app.features.content.schemas import (
    ContentItemCreate, ExportCreate, MaterialCreate, RegenerateCreate, ReviewCreate,
)
from backend.app.features.analysis.models import AnalysisRecord, OpportunityRecord
from backend.app.features.content.models import ContentPackageRecord, ProductRecord
from backend.app.db import Database
from backend.app.features.content.export import UnsafeContentPath, write_contained_atomic
from backend.app.features.content.service import ContentStateError, ContentValidationError
from backend.tests.content.test_workflow import create_product, seeded_service


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _image_item(tmp_path: Path):
    service, opportunity_id, evidence_id = seeded_service(tmp_path)
    product_id = create_product(service, opportunity_id)
    source = tmp_path / "incoming" / "cover.png"
    source.parent.mkdir()
    source.write_bytes(PNG_1X1)
    image = service.add_material(product_id, MaterialCreate(
        logical_name="cover.png", path="incoming/cover.png", media_type="image/png",
        kind="output_image",
    ))
    item = service.create_content_item(ContentItemCreate(
        product_id=product_id, opportunity_id=opportunity_id,
        template_key="list-v1", evidence_ids=[evidence_id],
        image_material_ids=[image.id], cover_material_id=image.id,
        research_facts=[{"fact": "真实事实", "evidence_ids": [evidence_id]}],
    ))
    return service, item, image


@pytest.mark.parametrize(
    "name",
    ["CON", "aux.txt", "bad:name.png", "trail. ", "line\nfeed.png", ".."],
)
def test_windows_unsafe_logical_names_are_rejected(name: str) -> None:
    with pytest.raises(ValidationError):
        MaterialCreate(
            logical_name=name, path="incoming/file.png", media_type="image/png",
            kind="output_image",
        )


def test_mutating_requests_require_revision_cas_and_forbid_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ReviewCreate(decision="approve", actor="operator", note="missing revision")
    with pytest.raises(ValidationError):
        RegenerateCreate.model_validate({"expected_revision_id": "r1", "extra": True})
    with pytest.raises(ValidationError):
        ExportCreate.model_validate({"expected_revision_id": "r1", "extra": True})


@pytest.mark.parametrize(
    ("payload", "media_type"),
    [(b"", "text/plain"), (b"not png", "image/png"), (b"\xff", "text/plain")],
)
def test_material_bytes_must_match_declared_supported_media(
    tmp_path: Path, payload: bytes, media_type: str
) -> None:
    service, opportunity_id, _ = seeded_service(tmp_path)
    product_id = create_product(service, opportunity_id)
    source = tmp_path / "incoming" / "file.bin"
    source.parent.mkdir()
    source.write_bytes(payload)
    with pytest.raises(ContentValidationError):
        service.add_material(product_id, MaterialCreate(
            logical_name="file.bin", path="incoming/file.bin", media_type=media_type,
            kind="source",
        ))


def test_zero_image_content_item_is_rejected(tmp_path: Path) -> None:
    service, opportunity_id, evidence_id = seeded_service(tmp_path)
    product_id = create_product(service, opportunity_id)
    with pytest.raises((ValidationError, ContentValidationError)):
        service.create_content_item(ContentItemCreate(
            product_id=product_id, opportunity_id=opportunity_id,
            template_key="list-v1", evidence_ids=[evidence_id],
            image_material_ids=[], cover_material_id="missing",
            research_facts=[{"fact": "真实事实", "evidence_ids": [evidence_id]}],
        ))


def test_review_compare_and_swap_allows_only_one_terminal_decision(tmp_path: Path) -> None:
    service, item, image = _image_item(tmp_path)
    revision_id = item.current_revision.id
    checks = [{"material_id": image.id, "passed": True, "observation": "封面文字清晰且与正文一致"}]

    def decide(decision: str):
        return service.review(item.id, ReviewCreate(
            decision=decision, actor="operator", note="并发审核",
            expected_revision_id=revision_id, visual_checks=checks,
        ))

    outcomes = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(decide, "approve"), pool.submit(decide, "reject")]
        for future in futures:
            try:
                outcomes.append(future.result().status)
            except ContentStateError:
                outcomes.append("conflict")
    assert sorted(outcomes).count("conflict") == 1
    assert len(service.get_content_item(item.id).reviews) == 1


def test_stale_revision_cannot_approve_regenerated_revision(tmp_path: Path) -> None:
    service, item, image = _image_item(tmp_path)
    r1 = item.current_revision.id
    service.review(item.id, ReviewCreate(
        decision="reject", actor="operator", note="需要重写", expected_revision_id=r1,
        visual_checks=[{"material_id": image.id, "passed": False, "observation": "封面主标题不够具体"}],
    ))
    r2 = service.regenerate(item.id, RegenerateCreate(expected_revision_id=r1)).current_revision.id
    with pytest.raises(ContentStateError):
        service.review(item.id, ReviewCreate(
            decision="approve", actor="operator", note="旧客户端批准",
            expected_revision_id=r1,
            visual_checks=[{"material_id": image.id, "passed": True, "observation": "看起来通过"}],
        ))
    assert r2 != r1


def test_material_availability_is_derived_from_real_file(tmp_path: Path) -> None:
    service, _, image = _image_item(tmp_path)
    assert image.availability == "available"
    (tmp_path / image.path).unlink()
    refreshed = service.get_product(image.product_id).materials[0]
    assert refreshed.availability == "missing"


def test_parent_swap_before_output_handle_never_writes_payload_outside_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import backend.app.features.content.export as export_module

    root = tmp_path / "runtime"
    parent = root / "safe"
    parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    real_open = os.open
    swapped = False

    def racing_open(path, flags, mode=0o777):
        nonlocal swapped
        if not swapped:
            swapped = True
            parent.rename(root / "safe-original")
            try:
                parent.symlink_to(outside, target_is_directory=True)
            except OSError:
                pytest.skip("directory symlink creation unavailable")
        return real_open(path, flags, mode)

    monkeypatch.setattr(export_module.os, "open", racing_open)
    with pytest.raises(UnsafeContentPath):
        write_contained_atomic(root, "safe/result.bin", b"secret-payload")
    escaped = outside / "result.bin"
    assert not escaped.exists() or escaped.read_bytes() == b""


def _approval(item, image):
    return ReviewCreate(
        decision="approve", actor="operator", note="人工复核通过",
        expected_revision_id=item.current_revision.id,
        visual_checks=[{"material_id": image.id, "passed": True, "observation": "逐图打开后确认清晰且图文一致"}],
    )


def test_opportunity_downgrade_blocks_review_and_export_without_false_records(tmp_path: Path) -> None:
    service, item, image = _image_item(tmp_path)
    with service.database.session() as session:
        product = session.get(ProductRecord, item.product_id)
        opportunity = session.get(OpportunityRecord, product.opportunity_id)
        analysis = session.get(AnalysisRecord, opportunity.analysis_id)
        analysis.status = "needs_human"
        session.commit()
    with pytest.raises(ContentValidationError):
        service.review(item.id, _approval(item, image))
    assert service.get_content_item(item.id).reviews == []


def test_export_revalidates_trust_and_live_materials(tmp_path: Path) -> None:
    service, item, image = _image_item(tmp_path)
    approved = service.review(item.id, _approval(item, image))
    with service.database.session() as session:
        product = session.get(ProductRecord, item.product_id)
        opportunity = session.get(OpportunityRecord, product.opportunity_id)
        analysis = session.get(AnalysisRecord, opportunity.analysis_id)
        analysis.status = "needs_human"
        session.commit()
    with pytest.raises(ContentValidationError):
        service.export_package(item.id, ExportCreate(expected_revision_id=approved.current_revision.id))
    assert service.list_packages() == []


def test_concurrent_export_has_one_persisted_builder_and_no_integrity_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, item, image = _image_item(tmp_path)
    approved = service.review(item.id, _approval(item, image))
    request = ExportCreate(expected_revision_id=approved.current_revision.id)

    # Force both reservation transactions to observe no package before either
    # INSERT is flushed. The first transaction is then allowed to commit before
    # the second flushes, making the UNIQUE(revision_id) arbitration repeatable.
    flush_barrier = Barrier(2)
    winner_committed = Event()
    designation_lock = Lock()
    next_designation = 0
    original_flush = Session.flush
    original_commit = Session.commit

    def synchronized_flush(session: Session, *args, **kwargs):
        nonlocal next_designation
        is_initial_package_flush = any(
            isinstance(record, ContentPackageRecord) for record in session.new
        ) and "export_race_designation" not in session.info
        if not is_initial_package_flush:
            return original_flush(session, *args, **kwargs)
        with designation_lock:
            designation = next_designation
            next_designation += 1
        session.info["export_race_designation"] = designation
        flush_barrier.wait(timeout=2)
        if designation == 1:
            assert winner_committed.wait(timeout=2)
        return original_flush(session, *args, **kwargs)

    def synchronized_commit(session: Session):
        result = original_commit(session)
        if session.info.get("export_race_designation") == 0:
            winner_committed.set()
        return result

    monkeypatch.setattr(Session, "flush", synchronized_flush)
    monkeypatch.setattr(Session, "commit", synchronized_commit)

    def export():
        try:
            return service.export_package(item.id, request).id
        except ContentStateError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [future.result() for future in [pool.submit(export), pool.submit(export)]]
    ids = {value for value in outcomes if value != "conflict"}
    assert len(ids) == 1
    assert outcomes.count("conflict") == 1
    assert len(service.list_packages()) == 1
    with service.database.engine.connect() as connection:
        cleanup_rows = connection.exec_driver_sql(
            "SELECT state FROM artifact_gc_queue WHERE owner_type='content_package'"
        ).scalars().all()
    assert cleanup_rows == ["cancelled"]


def test_package_and_exported_item_report_missing_file_truthfully(tmp_path: Path) -> None:
    service, item, image = _image_item(tmp_path)
    approved = service.review(item.id, _approval(item, image))
    package = service.export_package(item.id, ExportCreate(expected_revision_id=approved.current_revision.id))
    (tmp_path / package.path).unlink()
    assert service.list_packages()[0].availability == "missing"
    with pytest.raises(ContentStateError):
        service.get_package(package.id)
    assert service.get_content_item(item.id).export_availability == "missing"


def test_model_return_cannot_commit_after_opportunity_trust_drift(tmp_path: Path) -> None:
    service, opportunity_id, evidence_id = seeded_service(tmp_path)
    product_id = create_product(service, opportunity_id)
    image_path = tmp_path / "cover.png"
    image_path.write_bytes(PNG_1X1)
    image = service.add_material(product_id, MaterialCreate(
        logical_name="cover.png", path="cover.png", media_type="image/png", kind="output_image"
    ))
    original = service.model_adapter.generate_structured

    def drifting(request, schema):
        result = original(request, schema)
        with service.database.session() as session:
            opportunity = session.get(OpportunityRecord, opportunity_id)
            analysis = session.get(AnalysisRecord, opportunity.analysis_id)
            analysis.status = "needs_human"
            session.commit()
        return result

    service.model_adapter.generate_structured = drifting
    with pytest.raises(ContentValidationError):
        service.create_content_item(ContentItemCreate(
            product_id=product_id, opportunity_id=opportunity_id,
            template_key="list-v1", evidence_ids=[evidence_id],
            image_material_ids=[image.id], cover_material_id=image.id,
            research_facts=[{"fact": "真实事实", "evidence_ids": [evidence_id]}],
        ))
    assert service.list_content_items() == []


def test_startup_recovers_stranded_package_builder_to_failed(tmp_path: Path) -> None:
    service, item, image = _image_item(tmp_path)
    approved = service.review(item.id, _approval(item, image))
    package = service.export_package(item.id, ExportCreate(expected_revision_id=approved.current_revision.id))
    with service.database.session() as session:
        record = session.get(ContentPackageRecord, package.id)
        record.status = "building"
        session.commit()
    database_path = service.database.database_path
    service.database.close()
    reopened = Database(database_path)
    with reopened.session() as session:
        record = session.get(ContentPackageRecord, package.id)
        assert record.status == "failed"
        assert record.error_detail == "worker_restart_required"
    reopened.close()
