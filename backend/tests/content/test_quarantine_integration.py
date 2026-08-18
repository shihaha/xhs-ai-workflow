from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.features.content.cleanup import ArtifactCleanupService
from backend.app.features.content.models import (
    ArtifactCleanupRecord,
    ContentItemRecord,
    ContentPackageRecord,
    ProductMaterialRecord,
)
from backend.app.features.content.schemas import ExportCreate, MaterialCreate
from backend.app.features.content.service import ContentService, ContentStateError
from backend.tests.content.test_hardening import _approval, _image_item


def _inject_cleanup(service: ContentService, tmp_path: Path) -> ArtifactCleanupService:
    cleanup = ArtifactCleanupService(service.database, runtime_dir=tmp_path)
    service.cleanup_service = cleanup
    return cleanup


def _source_payload(tmp_path: Path) -> MaterialCreate:
    source = tmp_path / "incoming" / "facts.txt"
    source.write_text("facts", encoding="utf-8")
    return MaterialCreate(
        logical_name="facts.txt",
        path="incoming/facts.txt",
        media_type="text/plain",
        kind="source",
    )


def test_material_database_failure_enqueues_without_deleting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, item, _ = _image_item(tmp_path)
    _inject_cleanup(service, tmp_path)
    real_commit = Session.commit

    def fail_material_commit(session: Session) -> None:
        if any(
            isinstance(value, ProductMaterialRecord)
            for value in session.identity_map.values()
        ):
            raise SQLAlchemyError("generic commit failure")
        real_commit(session)

    monkeypatch.setattr(Session, "commit", fail_material_commit)

    with pytest.raises(SQLAlchemyError, match="generic commit failure"):
        service.add_material(item.product_id, _source_payload(tmp_path))

    with service.database.session() as session:
        cleanup = session.scalar(
            select(ArtifactCleanupRecord).where(
                ArtifactCleanupRecord.owner_type == "material",
                ArtifactCleanupRecord.reason == "material_persistence_failed",
            )
        )
        assert cleanup is not None
        assert cleanup.state == "pending"
        assert (tmp_path / cleanup.relative_path).read_text(encoding="utf-8") == "facts"
        assert session.get(ProductMaterialRecord, cleanup.owner_id) is None


def test_ambiguous_material_commit_retains_persisted_file_and_cleanup_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, item, _ = _image_item(tmp_path)
    cleanup_service = _inject_cleanup(service, tmp_path)
    real_commit = Session.commit
    raised = False

    def commit_then_raise(session: Session) -> None:
        nonlocal raised
        if not raised and any(
            isinstance(value, ProductMaterialRecord)
            for value in session.identity_map.values()
        ):
            raised = True
            real_commit(session)
            raise SQLAlchemyError("ambiguous commit acknowledgement")
        real_commit(session)

    monkeypatch.setattr(Session, "commit", commit_then_raise)

    with pytest.raises(SQLAlchemyError, match="ambiguous commit acknowledgement"):
        service.add_material(item.product_id, _source_payload(tmp_path))

    record = cleanup_service.list_records()[0]
    assert (tmp_path / record.relative_path).exists()
    processed = cleanup_service.process_one(record.id)
    assert processed.state == "needs_human"
    assert processed.last_error_category == "live_reference"
    assert (tmp_path / record.relative_path).exists()


def test_failed_finalizer_cannot_touch_taken_over_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import backend.app.features.content.service as service_module

    service, item, image = _image_item(tmp_path)
    _inject_cleanup(service, tmp_path)
    approved = service.review(item.id, _approval(item, image))
    request = ExportCreate(expected_revision_id=approved.current_revision.id)
    real_write = service_module.write_contained_atomic
    replacement_token = str(uuid4())

    def replace_builder_after_write(root: Path, relative: str, data: bytes) -> None:
        real_write(root, relative, data)
        with service.database.session() as session:
            package = session.scalar(select(ContentPackageRecord))
            assert package is not None
            package.build_token = replacement_token
            session.commit()

    monkeypatch.setattr(
        service_module, "write_contained_atomic", replace_builder_after_write
    )

    with pytest.raises(ContentStateError, match="changed before finalization"):
        service.export_package(item.id, request)

    with service.database.session() as session:
        package = session.scalar(select(ContentPackageRecord))
        assert package is not None
        assert package.status == "building"
        assert package.build_token == replacement_token
        cleanup = session.scalar(select(ArtifactCleanupRecord))
        assert cleanup is not None
        assert cleanup.owner_id == package.id
        assert cleanup.relative_path == package.path
        assert cleanup.reason == "package_builder_lost"
        assert (tmp_path / cleanup.relative_path).exists()


def test_ready_package_and_exported_item_commit_together_for_exact_builder(
    tmp_path: Path
) -> None:
    service, item, image = _image_item(tmp_path)
    _inject_cleanup(service, tmp_path)
    approved = service.review(item.id, _approval(item, image))

    package = service.export_package(
        item.id, ExportCreate(expected_revision_id=approved.current_revision.id)
    )

    with service.database.session() as session:
        stored_package = session.get(ContentPackageRecord, package.id)
        stored_item = session.get(ContentItemRecord, item.id)
        assert stored_package is not None
        assert stored_package.status == "ready"
        assert stored_package.build_token is not None
        assert stored_item is not None
        assert stored_item.status == "exported"


def test_ambiguous_failed_finalizer_retains_artifact_and_records_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import backend.app.features.content.service as service_module

    service, item, image = _image_item(tmp_path)
    _inject_cleanup(service, tmp_path)
    approved = service.review(item.id, _approval(item, image))
    request = ExportCreate(expected_revision_id=approved.current_revision.id)
    real_write = service_module.write_contained_atomic
    real_commit = Session.commit
    commit_count = 0

    def write_then_fail(root: Path, relative: str, data: bytes) -> None:
        real_write(root, relative, data)
        raise RuntimeError("forced package build failure")

    def failed_commit_then_raise(session: Session) -> None:
        nonlocal commit_count
        commit_count += 1
        if commit_count == 2:
            real_commit(session)
            raise SQLAlchemyError("ambiguous failed finalizer commit")
        real_commit(session)

    monkeypatch.setattr(service_module, "write_contained_atomic", write_then_fail)
    monkeypatch.setattr(Session, "commit", failed_commit_then_raise)

    with pytest.raises(RuntimeError, match="forced package build failure"):
        service.export_package(item.id, request)

    with service.database.session() as session:
        package = session.scalar(select(ContentPackageRecord))
        cleanup = session.scalar(select(ArtifactCleanupRecord))
        assert package is not None
        assert package.status == "failed"
        assert cleanup is not None
        assert cleanup.state == "pending"
        assert cleanup.reason == "package_transaction_unknown"
        assert (tmp_path / cleanup.relative_path).exists()


def test_reference_created_while_cleanup_pending_is_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, item, _ = _image_item(tmp_path)
    cleanup_service = _inject_cleanup(service, tmp_path)
    real_commit = Session.commit

    def fail_material_commit(session: Session) -> None:
        if any(
            isinstance(value, ProductMaterialRecord)
            for value in session.identity_map.values()
        ):
            raise SQLAlchemyError("generic commit failure")
        real_commit(session)

    monkeypatch.setattr(Session, "commit", fail_material_commit)
    with pytest.raises(SQLAlchemyError):
        service.add_material(item.product_id, _source_payload(tmp_path))
    monkeypatch.setattr(Session, "commit", real_commit)
    record = cleanup_service.list_records()[0]

    with service.database.session() as session:
        session.add(
            ProductMaterialRecord(
                id=str(uuid4()),
                product_id=item.product_id,
                logical_name="same.txt",
                logical_key="same.txt",
                version=1,
                path=record.relative_path,
                sha256=record.expected_sha256,
                size_bytes=record.expected_size_bytes,
                media_type="text/plain",
                kind="source",
                created_at=record.created_at,
            )
        )
        session.commit()

    processed = cleanup_service.process_one(record.id)
    assert processed.state == "needs_human"
    assert processed.last_error_category == "live_reference"
    assert (tmp_path / record.relative_path).exists()
