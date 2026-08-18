from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.db import canonical_artifact_path_key
from backend.app.features.content.cleanup import ArtifactCleanupService
from backend.app.features.content.cleanup import ArtifactCleanupCandidate
from backend.app.features.content.api import _translate
from backend.app.features.content.export import UnsafeContentPath
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


def test_package_builder_lost_translates_to_conflict() -> None:
    response = _translate(ContentStateError("package_builder_lost"))
    assert response.status_code == 409


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
        assert cleanup.not_before <= datetime.now(UTC).replace(tzinfo=None)


def test_package_reservation_commit_ack_lost_continues_after_exact_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, item, image = _image_item(tmp_path)
    _inject_cleanup(service, tmp_path)
    approved = service.review(item.id, _approval(item, image))
    real_commit = Session.commit
    raised = False

    def reservation_commit_then_raise(session: Session) -> None:
        nonlocal raised
        real_commit(session)
        if raised:
            return
        with service.database.engine.connect() as connection:
            landed = connection.exec_driver_sql(
                "SELECT COUNT(*) FROM content_packages p JOIN artifact_gc_queue g "
                "ON g.owner_id=p.id AND g.relative_path=p.path "
                "WHERE p.status='building' AND g.state='pending' "
                "AND g.reason='package_build_reserved'"
            ).scalar_one()
        if landed == 1:
            raised = True
            raise SQLAlchemyError("reservation acknowledgement lost")

    monkeypatch.setattr(Session, "commit", reservation_commit_then_raise)
    package = service.export_package(
        item.id, ExportCreate(expected_revision_id=approved.current_revision.id)
    )
    assert package.status == "ready"
    assert package.availability == "available"


def test_package_reservation_not_landed_never_writes_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, item, image = _image_item(tmp_path)
    _inject_cleanup(service, tmp_path)
    approved = service.review(item.id, _approval(item, image))
    writes: list[str] = []

    monkeypatch.setattr(
        Session, "commit", lambda _session: (_ for _ in ()).throw(
            SQLAlchemyError("reservation commit rejected")
        )
    )
    monkeypatch.setattr(
        "backend.app.features.content.service.write_contained_atomic",
        lambda _root, relative, _data: writes.append(relative),
    )

    with pytest.raises(ContentStateError, match="package_reservation_failed"):
        service.export_package(
            item.id, ExportCreate(expected_revision_id=approved.current_revision.id)
        )
    assert writes == []
    with service.database.session() as session:
        assert session.scalar(select(ContentPackageRecord)) is None


def test_package_reservation_partial_landing_is_transaction_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, item, image = _image_item(tmp_path)
    _inject_cleanup(service, tmp_path)
    approved = service.review(item.id, _approval(item, image))
    real_commit = Session.commit
    mutated = False
    writes: list[str] = []

    def commit_mutate_cleanup_then_raise(session: Session) -> None:
        nonlocal mutated
        real_commit(session)
        if mutated:
            return
        with service.database.engine.begin() as connection:
            cleanup_id = connection.exec_driver_sql(
                "SELECT id FROM artifact_gc_queue "
                "WHERE reason='package_build_reserved' AND state='pending'"
            ).scalar_one_or_none()
            if cleanup_id is not None:
                connection.exec_driver_sql(
                    "UPDATE artifact_gc_queue SET expected_size_bytes=expected_size_bytes+1 "
                    "WHERE id=?", (cleanup_id,)
                )
                mutated = True
        if mutated:
            raise SQLAlchemyError("reservation acknowledgement ambiguous")

    monkeypatch.setattr(Session, "commit", commit_mutate_cleanup_then_raise)
    monkeypatch.setattr(
        "backend.app.features.content.service.write_contained_atomic",
        lambda _root, relative, _data: writes.append(relative),
    )

    with pytest.raises(ContentStateError, match="package_reservation_transaction_unknown"):
        service.export_package(
            item.id, ExportCreate(expected_revision_id=approved.current_revision.id)
        )
    assert writes == []


def test_package_reservation_unreadable_proof_is_transaction_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, item, image = _image_item(tmp_path)
    _inject_cleanup(service, tmp_path)
    approved = service.review(item.id, _approval(item, image))
    real_session = service.database.session
    session_calls = 0
    writes: list[str] = []

    @contextmanager
    def unreadable_proof_session():
        nonlocal session_calls
        session_calls += 1
        if session_calls == 3:
            raise SQLAlchemyError("fresh reservation proof unavailable")
        with real_session() as session:
            yield session

    monkeypatch.setattr(service.database, "session", unreadable_proof_session)
    monkeypatch.setattr(
        Session, "commit", lambda _session: (_ for _ in ()).throw(
            SQLAlchemyError("reservation acknowledgement unavailable")
        )
    )
    monkeypatch.setattr(
        "backend.app.features.content.service.write_contained_atomic",
        lambda _root, relative, _data: writes.append(relative),
    )

    with pytest.raises(ContentStateError, match="package_reservation_transaction_unknown"):
        service.export_package(
            item.id, ExportCreate(expected_revision_id=approved.current_revision.id)
        )
    assert writes == []


def test_failed_package_commit_not_landed_is_transaction_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import backend.app.features.content.service as service_module

    service, item, image = _image_item(tmp_path)
    _inject_cleanup(service, tmp_path)
    approved = service.review(item.id, _approval(item, image))
    real_write = service_module.write_contained_atomic
    real_commit = Session.commit
    commit_count = 0

    def write_then_fail(root: Path, relative: str, data: bytes) -> None:
        real_write(root, relative, data)
        raise RuntimeError("forced package failure")

    def reject_failed_commit(session: Session) -> None:
        nonlocal commit_count
        commit_count += 1
        if commit_count == 2:
            raise SQLAlchemyError("failed transition rejected")
        real_commit(session)

    monkeypatch.setattr(service_module, "write_contained_atomic", write_then_fail)
    monkeypatch.setattr(Session, "commit", reject_failed_commit)
    with pytest.raises(ContentStateError, match="package_failure_transaction_unknown"):
        service.export_package(
            item.id, ExportCreate(expected_revision_id=approved.current_revision.id)
        )


def test_failed_package_ack_with_cleanup_identity_mismatch_is_transaction_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import backend.app.features.content.service as service_module

    service, item, image = _image_item(tmp_path)
    _inject_cleanup(service, tmp_path)
    approved = service.review(item.id, _approval(item, image))
    real_write = service_module.write_contained_atomic
    real_commit = Session.commit
    commit_count = 0

    def write_then_fail(root: Path, relative: str, data: bytes) -> None:
        real_write(root, relative, data)
        raise RuntimeError("forced package failure")

    def commit_mutate_cleanup_then_raise(session: Session) -> None:
        nonlocal commit_count
        commit_count += 1
        real_commit(session)
        if commit_count == 2:
            with service.database.engine.begin() as connection:
                connection.exec_driver_sql(
                    "UPDATE artifact_gc_queue SET expected_size_bytes=expected_size_bytes+1 "
                    "WHERE reason='package_build_reserved' AND state='pending'"
                )
            raise SQLAlchemyError("failed transition acknowledgement ambiguous")

    monkeypatch.setattr(service_module, "write_contained_atomic", write_then_fail)
    monkeypatch.setattr(Session, "commit", commit_mutate_cleanup_then_raise)
    with pytest.raises(ContentStateError, match="package_failure_transaction_unknown"):
        service.export_package(
            item.id, ExportCreate(expected_revision_id=approved.current_revision.id)
        )


def test_explicit_material_write_failure_makes_reservation_due_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, item, _ = _image_item(tmp_path)
    _inject_cleanup(service, tmp_path)

    monkeypatch.setattr(
        "backend.app.features.content.service.write_contained_atomic",
        lambda _root, _relative, _data: (_ for _ in ()).throw(
            RuntimeError("explicit material write failure")
        ),
    )
    before = datetime.now(UTC).replace(tzinfo=None)
    with pytest.raises(RuntimeError, match="explicit material write failure"):
        service.add_material(item.product_id, _source_payload(tmp_path))

    record = next(
        value for value in service.cleanup_service.list_records()
        if value.reason == "material_write_reserved" and value.state == "pending"
    )
    assert record.not_before <= datetime.now(UTC).replace(tzinfo=None)
    assert record.not_before >= before - timedelta(seconds=1)


def test_material_database_not_landed_is_due_now_without_deleting(
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

    before = datetime.now(UTC).replace(tzinfo=None)
    with pytest.raises(ContentStateError, match="material_persistence_not_landed"):
        service.add_material(item.product_id, _source_payload(tmp_path))

    with service.database.session() as session:
        cleanup = session.scalar(
            select(ArtifactCleanupRecord).where(
                ArtifactCleanupRecord.owner_type == "material",
                ArtifactCleanupRecord.reason == "material_persistence_failed",
                ArtifactCleanupRecord.state == "pending",
            )
        )
        assert cleanup is not None
        assert cleanup.state == "pending"
        assert cleanup.not_before <= datetime.now(UTC).replace(tzinfo=None)
        assert cleanup.not_before >= before - timedelta(seconds=1)
        assert (tmp_path / cleanup.relative_path).read_text(encoding="utf-8") == "facts"
        assert session.get(ProductMaterialRecord, cleanup.owner_id) is None


def test_material_persistence_unknown_but_writable_is_due_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, item, _ = _image_item(tmp_path)
    _inject_cleanup(service, tmp_path)
    real_commit = Session.commit
    mutated = False

    def commit_partial_facts_then_raise(session: Session) -> None:
        nonlocal mutated
        real_commit(session)
        if mutated or not any(
            isinstance(value, ProductMaterialRecord)
            for value in session.identity_map.values()
        ):
            return
        with service.database.engine.begin() as connection:
            owner_id = connection.exec_driver_sql(
                "SELECT id FROM content_product_materials ORDER BY created_at DESC LIMIT 1"
            ).scalar_one()
            connection.exec_driver_sql(
                "UPDATE artifact_gc_queue SET state='pending' WHERE owner_id=?",
                (owner_id,),
            )
            connection.exec_driver_sql(
                "UPDATE content_product_materials SET logical_name='mismatched.txt' "
                "WHERE id=?", (owner_id,),
            )
        mutated = True
        raise SQLAlchemyError("material acknowledgement partially observable")

    monkeypatch.setattr(Session, "commit", commit_partial_facts_then_raise)
    before = datetime.now(UTC).replace(tzinfo=None)
    with pytest.raises(ContentStateError, match="transaction_unknown"):
        service.add_material(item.product_id, _source_payload(tmp_path))

    with service.database.session() as session:
        cleanup = session.scalar(select(ArtifactCleanupRecord).where(
            ArtifactCleanupRecord.owner_type == "material",
            ArtifactCleanupRecord.state == "pending",
        ))
        assert cleanup is not None
        assert cleanup.reason == "material_transaction_unknown"
        assert cleanup.not_before <= datetime.now(UTC).replace(tzinfo=None)
        assert cleanup.not_before >= before - timedelta(seconds=1)


def test_material_persistence_unreadable_retains_delayed_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, item, _ = _image_item(tmp_path)
    _inject_cleanup(service, tmp_path)
    real_session = service.database.session
    real_commit = Session.commit
    unreadable = False

    @contextmanager
    def session_with_outage():
        if unreadable:
            raise SQLAlchemyError("database unavailable for fresh proof")
        with real_session() as session:
            yield session

    def fail_material_commit(session: Session) -> None:
        nonlocal unreadable
        if any(
            isinstance(value, ProductMaterialRecord)
            for value in session.identity_map.values()
        ):
            unreadable = True
            raise SQLAlchemyError("material commit acknowledgement unavailable")
        real_commit(session)

    monkeypatch.setattr(service.database, "session", session_with_outage)
    monkeypatch.setattr(Session, "commit", fail_material_commit)
    with pytest.raises(ContentStateError, match="material_failure_transaction_unknown"):
        service.add_material(item.product_id, _source_payload(tmp_path))

    unreadable = False
    with real_session() as session:
        cleanup = session.scalar(select(ArtifactCleanupRecord).where(
            ArtifactCleanupRecord.owner_type == "material",
            ArtifactCleanupRecord.state == "pending",
        ))
        assert cleanup is not None
        assert cleanup.reason == "material_write_reserved"
        assert cleanup.not_before >= datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=23)


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

    with pytest.raises(ContentStateError, match="package_builder_lost"):
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


def test_unsafe_path_failure_after_builder_takeover_is_builder_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import backend.app.features.content.service as service_module

    service, item, image = _image_item(tmp_path)
    _inject_cleanup(service, tmp_path)
    approved = service.review(item.id, _approval(item, image))
    real_write = service_module.write_contained_atomic
    replacement_token = str(uuid4())

    def takeover_then_reject_path(root: Path, relative: str, data: bytes) -> None:
        real_write(root, relative, data)
        with service.database.session() as session:
            package = session.scalar(select(ContentPackageRecord))
            assert package is not None
            package.build_token = replacement_token
            session.commit()
        raise UnsafeContentPath("forced unsafe package path")

    monkeypatch.setattr(
        service_module, "write_contained_atomic", takeover_then_reject_path
    )
    with pytest.raises(ContentStateError, match="package_builder_lost"):
        service.export_package(
            item.id, ExportCreate(expected_revision_id=approved.current_revision.id)
        )

    with service.database.session() as session:
        package = session.scalar(select(ContentPackageRecord))
        cleanup = session.scalar(select(ArtifactCleanupRecord).where(
            ArtifactCleanupRecord.owner_type == "content_package",
            ArtifactCleanupRecord.state == "pending",
        ))
        assert package is not None and package.status == "building"
        assert package.build_token == replacement_token
        assert cleanup is not None and cleanup.reason == "package_build_reserved"


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


def test_replacement_reservation_partial_cleanup_is_transaction_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, item, image = _image_item(tmp_path)
    cleanup_service = _inject_cleanup(service, tmp_path)
    approved = service.review(item.id, _approval(item, image))
    request = ExportCreate(expected_revision_id=approved.current_revision.id)
    original = service.export_package(item.id, request)
    (tmp_path / original.path).write_bytes(b"corrupt")
    now = datetime.now(UTC).replace(tzinfo=None)
    path_key = canonical_artifact_path_key(original.path)
    assert path_key is not None
    with service.database.session() as session:
        source_build_token = session.get(ContentPackageRecord, original.id).build_token
    assert source_build_token is not None
    partial = ArtifactCleanupRecord(
        id=str(uuid4()), owner_type="content_package", owner_id=original.id,
        source_build_token=source_build_token,
        relative_path=original.path, path_key=path_key,
        expected_sha256=original.sha256,
        expected_size_bytes=original.size_bytes, state="cancelled",
        reason="package_replaced", not_before=now,
        created_at=now, updated_at=now, completed_at=now,
    )
    with service.database.session() as session:
        session.add(partial)
        session.commit()

    real_enqueue = cleanup_service.enqueue_in_session

    def reuse_partial(session: Session, candidate):
        if candidate.reason == "package_replaced":
            return partial
        return real_enqueue(session, candidate)

    monkeypatch.setattr(cleanup_service, "enqueue_in_session", reuse_partial)
    monkeypatch.setattr(
        Session, "commit", lambda _session: (_ for _ in ()).throw(
            SQLAlchemyError("replacement reservation rejected")
        )
    )
    with pytest.raises(ContentStateError, match="package_reservation_transaction_unknown"):
        service.export_package(item.id, request)

    with service.database.session() as session:
        stored = session.get(ContentPackageRecord, original.id)
        assert stored is not None and stored.status == "ready"
        assert stored.path == original.path


def test_replacement_reservation_with_both_cleanups_absent_is_not_landed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, item, image = _image_item(tmp_path)
    _inject_cleanup(service, tmp_path)
    approved = service.review(item.id, _approval(item, image))
    request = ExportCreate(expected_revision_id=approved.current_revision.id)
    original = service.export_package(item.id, request)
    (tmp_path / original.path).write_bytes(b"corrupt")

    monkeypatch.setattr(
        Session, "commit", lambda _session: (_ for _ in ()).throw(
            SQLAlchemyError("replacement reservation rejected")
        )
    )
    with pytest.raises(ContentStateError, match="package_reservation_failed"):
        service.export_package(item.id, request)

    with service.database.session() as session:
        stored = session.get(ContentPackageRecord, original.id)
        assert stored is not None and stored.status == "ready"
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
        assert package.sha256 == cleanup.expected_sha256
        assert package.size_bytes == cleanup.expected_size_bytes
        assert cleanup.state == "pending"
        assert cleanup.reason == "package_build_reserved"
        assert cleanup.not_before <= datetime.now(UTC).replace(tzinfo=None)
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
    with pytest.raises(ContentStateError, match="material_persistence_not_landed"):
        service.add_material(item.product_id, _source_payload(tmp_path))
    monkeypatch.setattr(Session, "commit", real_commit)
    record = next(
        value for value in cleanup_service.list_records()
        if value.reason == "material_persistence_failed" and value.state == "pending"
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


def test_retried_package_keeps_old_generation_cleanup_authorized(
    tmp_path: Path,
) -> None:
    service, item, image = _image_item(tmp_path)
    cleanup_service = _inject_cleanup(service, tmp_path)
    approved = service.review(item.id, _approval(item, image))
    request = ExportCreate(expected_revision_id=approved.current_revision.id)
    first = service.export_package(item.id, request)
    first_bytes = (tmp_path / first.path).read_bytes()

    (tmp_path / first.path).write_bytes(b"corrupt")
    second = service.export_package(item.id, request)
    assert second.path != first.path

    old_cleanup = next(
        cleanup
        for cleanup in cleanup_service.list_records()
        if cleanup.reason == "package_replaced" and cleanup.relative_path == first.path
    )
    assert old_cleanup.source_build_token is not None
    with service.database.session() as session:
        current = session.get(ContentPackageRecord, first.id)
        assert current is not None
        assert current.build_token != old_cleanup.source_build_token

    # Restore the recorded immutable bytes: replacement tests deliberately corrupt
    # the file only to force a new package generation.
    (tmp_path / first.path).write_bytes(first_bytes)
    with service.database.engine.begin() as connection:
        connection.execute(
            text("UPDATE artifact_gc_queue SET not_before=created_at WHERE id=:id"),
            {"id": old_cleanup.id},
        )
    processed = cleanup_service.process_one(old_cleanup.id)
    assert processed.state == "quarantined"
    assert not (tmp_path / first.path).exists()
