from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.features.content.cleanup import ArtifactCleanupService
from backend.app.features.content.cleanup import ArtifactCleanupCandidate
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


def test_enqueue_and_cancel_can_share_caller_transaction(tmp_path: Path) -> None:
    service, item, _ = _image_item(tmp_path)
    cleanup_service = _inject_cleanup(service, tmp_path)
    candidate = ArtifactCleanupCandidate(
        owner_type="material",
        owner_id=str(uuid4()),
        relative_path=f"content-materials/{item.product_id}/{uuid4()}/source.txt",
        expected_sha256="a" * 64,
        expected_size_bytes=5,
        reason="material_write_reserved",
        not_before=item.created_at,
    )
    with service.database.session() as session:
        cleanup = cleanup_service.enqueue_in_session(session, candidate)
        assert cleanup_service.cancel_in_session(
            session, cleanup.id, candidate=candidate
        )
        session.commit()

    stored = cleanup_service.get_record(cleanup.id)
    assert stored is not None
    assert stored.state == "cancelled"


def test_material_cleanup_reservation_failure_prevents_managed_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, item, _ = _image_item(tmp_path)
    cleanup_service = _inject_cleanup(service, tmp_path)
    writes: list[str] = []

    def reject_reservation(*_args: object, **_kwargs: object) -> object:
        raise SQLAlchemyError("cleanup outbox unavailable")

    monkeypatch.setattr(
        cleanup_service, "enqueue_in_session", reject_reservation, raising=False
    )
    monkeypatch.setattr(
        "backend.app.features.content.service.write_contained_atomic",
        lambda _root, relative, _data: writes.append(relative),
    )

    with pytest.raises(SQLAlchemyError, match="cleanup outbox unavailable"):
        service.add_material(item.product_id, _source_payload(tmp_path))

    assert writes == []


def test_material_pending_reservation_exists_before_managed_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, item, _ = _image_item(tmp_path)
    _inject_cleanup(service, tmp_path)

    def observe_then_crash(_root: Path, relative: str, _data: bytes) -> None:
        with service.database.engine.connect() as connection:
            cleanup = connection.exec_driver_sql(
                "SELECT state, relative_path, reason, not_before, created_at "
                "FROM artifact_gc_queue "
                "WHERE reason='material_write_reserved' AND state='pending'"
            ).mappings().one()
        assert cleanup["state"] == "pending"
        assert cleanup["relative_path"] == relative
        assert cleanup["reason"] == "material_write_reserved"
        assert (
            datetime.fromisoformat(cleanup["not_before"])
            - datetime.fromisoformat(cleanup["created_at"])
        ) >= timedelta(hours=23)
        raise RuntimeError("simulated process stop")

    monkeypatch.setattr(
        "backend.app.features.content.service.write_contained_atomic",
        observe_then_crash,
    )
    with pytest.raises(RuntimeError, match="simulated process stop"):
        service.add_material(item.product_id, _source_payload(tmp_path))

    assert any(
        record.state == "pending" and record.reason == "material_write_reserved"
        for record in service.cleanup_service.list_records()
    )


def test_package_cleanup_reservation_failure_rolls_back_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, item, image = _image_item(tmp_path)
    cleanup_service = _inject_cleanup(service, tmp_path)
    approved = service.review(item.id, _approval(item, image))

    def reject_build_reservation(_session: Session, candidate) -> object:
        if candidate.reason == "package_build_reserved":
            raise SQLAlchemyError("cleanup outbox unavailable")
        raise AssertionError(candidate.reason)

    monkeypatch.setattr(
        cleanup_service, "enqueue_in_session", reject_build_reservation, raising=False
    )
    with pytest.raises(SQLAlchemyError, match="cleanup outbox unavailable"):
        service.export_package(
            item.id, ExportCreate(expected_revision_id=approved.current_revision.id)
        )

    with service.database.session() as session:
        assert session.scalar(select(ContentPackageRecord)) is None


def test_package_pending_reservation_exists_before_archive_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, item, image = _image_item(tmp_path)
    _inject_cleanup(service, tmp_path)
    approved = service.review(item.id, _approval(item, image))

    def observe_then_crash(_root: Path, relative: str, _data: bytes) -> None:
        with service.database.engine.connect() as connection:
            cleanup = connection.exec_driver_sql(
                "SELECT state, relative_path, reason, not_before, created_at "
                "FROM artifact_gc_queue "
                "WHERE reason='package_build_reserved' AND state='pending'"
            ).mappings().one()
        assert cleanup["state"] == "pending"
        assert cleanup["relative_path"] == relative
        assert cleanup["reason"] == "package_build_reserved"
        assert (
            datetime.fromisoformat(cleanup["not_before"])
            - datetime.fromisoformat(cleanup["created_at"])
        ) >= timedelta(hours=23)
        raise RuntimeError("simulated package process stop")

    monkeypatch.setattr(
        "backend.app.features.content.service.write_contained_atomic",
        observe_then_crash,
    )
    with pytest.raises(RuntimeError, match="simulated package process stop"):
        service.export_package(
            item.id, ExportCreate(expected_revision_id=approved.current_revision.id)
        )

    with service.database.session() as session:
        package = session.scalar(select(ContentPackageRecord))
        cleanup = session.scalar(select(ArtifactCleanupRecord).where(
            ArtifactCleanupRecord.reason == "package_build_reserved"
        ))
        assert package is not None and package.status == "failed"
        assert cleanup is not None and cleanup.state == "pending"


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

    with pytest.raises(ContentStateError, match="transaction_unknown"):
        service.add_material(item.product_id, _source_payload(tmp_path))

    with service.database.session() as session:
        cleanup = session.scalar(
            select(ArtifactCleanupRecord).where(
                ArtifactCleanupRecord.owner_type == "material",
                ArtifactCleanupRecord.reason == "material_write_reserved",
                ArtifactCleanupRecord.state == "pending",
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

    material = service.add_material(item.product_id, _source_payload(tmp_path))

    record = next(
        value for value in cleanup_service.list_records()
        if value.owner_id == material.id
    )
    assert record.state == "cancelled"
    assert (tmp_path / material.path).exists()


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
        cleanup = session.scalar(select(ArtifactCleanupRecord).where(
            ArtifactCleanupRecord.owner_type == "content_package",
            ArtifactCleanupRecord.state == "pending",
        ))
        assert cleanup is not None
        assert cleanup.owner_id == package.id
        assert cleanup.relative_path == package.path
        assert cleanup.reason == "package_build_reserved"
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


def test_ready_commit_acknowledgement_failure_returns_proven_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, item, image = _image_item(tmp_path)
    cleanup_service = _inject_cleanup(service, tmp_path)
    approved = service.review(item.id, _approval(item, image))
    real_commit = Session.commit
    raised = False

    def ready_commit_then_raise(session: Session) -> None:
        nonlocal raised
        real_commit(session)
        if raised:
            return
        with service.database.engine.connect() as connection:
            ready = connection.exec_driver_sql(
                "SELECT COUNT(*) FROM content_packages WHERE status='ready'"
            ).scalar_one()
            cancelled = connection.exec_driver_sql(
                "SELECT COUNT(*) FROM artifact_gc_queue "
                "WHERE owner_type='content_package' AND state='cancelled'"
            ).scalar_one()
        if ready == 1 and cancelled == 1:
            raised = True
            raise SQLAlchemyError("ambiguous ready acknowledgement")

    monkeypatch.setattr(Session, "commit", ready_commit_then_raise)

    package = service.export_package(
        item.id, ExportCreate(expected_revision_id=approved.current_revision.id)
    )

    assert package.status == "ready"
    assert package.availability == "available"
    build_cleanup = next(
        record for record in cleanup_service.list_records()
        if record.owner_type == "content_package"
    )
    assert build_cleanup.state == "cancelled"


def test_replacement_cleanup_failure_rolls_back_path_and_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, item, image = _image_item(tmp_path)
    cleanup_service = _inject_cleanup(service, tmp_path)
    approved = service.review(item.id, _approval(item, image))
    request = ExportCreate(expected_revision_id=approved.current_revision.id)
    original = service.export_package(item.id, request)
    (tmp_path / original.path).write_bytes(b"corrupt")
    real_enqueue = cleanup_service.enqueue_in_session

    def reject_replacement(session: Session, candidate):
        if candidate.reason == "package_replaced":
            raise SQLAlchemyError("replacement outbox unavailable")
        return real_enqueue(session, candidate)

    monkeypatch.setattr(cleanup_service, "enqueue_in_session", reject_replacement)
    with pytest.raises(SQLAlchemyError, match="replacement outbox unavailable"):
        service.export_package(item.id, request)

    with service.database.session() as session:
        stored = session.get(ContentPackageRecord, original.id)
        assert stored is not None
        assert stored.status == "ready"
        assert stored.path == original.path


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
        cleanup = session.scalar(select(ArtifactCleanupRecord).where(
            ArtifactCleanupRecord.owner_type == "content_package",
            ArtifactCleanupRecord.state == "pending",
        ))
        assert package is not None
        assert package.status == "failed"
        assert cleanup is not None
        assert cleanup.state == "pending"
        assert cleanup.reason == "package_build_reserved"
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
    with pytest.raises(ContentStateError, match="transaction_unknown"):
        service.add_material(item.product_id, _source_payload(tmp_path))
    monkeypatch.setattr(Session, "commit", real_commit)
    record = next(
        value for value in cleanup_service.list_records()
        if value.reason == "material_write_reserved" and value.state == "pending"
    )

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

    with service.database.engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE artifact_gc_queue SET not_before=created_at WHERE id=?",
            (record.id,),
        )

    processed = cleanup_service.process_one(record.id)
    assert processed.state == "needs_human"
    assert processed.last_error_category == "live_reference"
    assert (tmp_path / record.relative_path).exists()
