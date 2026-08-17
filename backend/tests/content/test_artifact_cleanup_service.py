from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta
from hashlib import sha256
import os
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Update

from backend.app.db import Database
from backend.app.features.content.cleanup import (
    ArtifactCleanupCandidate,
    ArtifactCleanupService,
)
from backend.app.features.content.models import ArtifactCleanupRecord


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 18, 0, 0, 0)

    def now(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


@pytest.fixture
def cleanup_environment(tmp_path: Path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    database = Database(tmp_path / "cleanup.sqlite3", runtime_dir=runtime)
    clock = Clock()
    first = ArtifactCleanupService(database, runtime_dir=runtime, clock=clock.now)
    second = ArtifactCleanupService(database, runtime_dir=runtime, clock=clock.now)
    try:
        yield database, runtime, clock, first, second
    finally:
        database.close()


def _candidate(
    runtime: Path,
    clock: Clock,
    *,
    relative_path: str = "orphaned/material.bin",
    payload: bytes = b"abandoned artifact",
    owner_id: str | None = None,
) -> ArtifactCleanupCandidate:
    target = runtime / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return ArtifactCleanupCandidate(
        owner_type="material",
        owner_id=owner_id or str(uuid4()),
        relative_path=relative_path,
        expected_sha256=sha256(payload).hexdigest(),
        expected_size_bytes=len(payload),
        reason="unpersisted_material",
        not_before=clock.now(),
    )


def _insert_material_reference(
    database: Database,
    *,
    material_id: str,
    relative_path: str,
    digest: str,
    size_bytes: int,
    created_at: datetime,
) -> None:
    with database.engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.execute(
            text(
                "INSERT INTO content_product_materials "
                "(id,product_id,logical_name,logical_key,version,path,sha256,"
                "size_bytes,media_type,kind,created_at) VALUES "
                "(:id,:product_id,'material.bin','material.bin',1,:path,:sha256,"
                ":size_bytes,'application/octet-stream','source',:created_at)"
            ),
            {
                "id": material_id,
                "product_id": str(uuid4()),
                "path": relative_path,
                "sha256": digest,
                "size_bytes": size_bytes,
                "created_at": created_at.isoformat(sep=" "),
            },
        )
        connection.commit()


def test_candidate_is_frozen(cleanup_environment) -> None:
    _, runtime, clock, _, _ = cleanup_environment
    candidate = _candidate(runtime, clock)
    with pytest.raises(FrozenInstanceError):
        candidate.reason = "changed"  # type: ignore[misc]


def test_enqueue_is_idempotent_for_open_owner(cleanup_environment) -> None:
    _, runtime, clock, service, _ = cleanup_environment
    candidate = _candidate(runtime, clock)

    first = service.enqueue(candidate)
    second = service.enqueue(candidate)

    assert second.id == first.id
    assert service.get_record(first.id) == first
    assert [record.id for record in service.list_records()] == [first.id]


def test_two_workers_only_one_claims_due_record(cleanup_environment) -> None:
    _, runtime, clock, first, second = cleanup_environment
    record = first.enqueue(_candidate(runtime, clock))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(first.claim_due, limit=1),
            pool.submit(second.claim_due, limit=1),
        ]
    claimed = [future.result() for future in futures]

    assert sum(record.id in ids for ids in claimed) == 1
    persisted = first.get_record(record.id)
    assert persisted is not None
    assert persisted.state == "claimed"
    assert persisted.lease_token is not None


def test_recover_expired_lease_returns_it_to_pending(cleanup_environment) -> None:
    _, runtime, clock, first, _ = cleanup_environment
    record = first.enqueue(_candidate(runtime, clock))
    assert first.claim_due(limit=1) == [record.id]

    clock.advance(timedelta(minutes=5, seconds=1))

    assert first.recover_expired_leases() == 1
    recovered = first.get_record(record.id)
    assert recovered is not None
    assert recovered.state == "pending"
    assert recovered.lease_token is None
    assert recovered.attempt_count == 1


def test_recover_expired_lease_does_not_overwrite_a_new_claim(
    cleanup_environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, runtime, clock, service, _ = cleanup_environment
    record = service.enqueue(_candidate(runtime, clock))
    assert service.claim_due(limit=1) == [record.id]
    clock.advance(timedelta(minutes=6))
    replacement_token = str(uuid4())
    replacement_expiry = clock.now() + timedelta(minutes=5)
    real_execute = Session.execute
    replaced = False

    def replace_before_recovery_update(session: Session, statement, *args, **kwargs):
        nonlocal replaced
        if (
            not replaced
            and isinstance(statement, Update)
            and statement.table.name == "artifact_gc_queue"
        ):
            replaced = True
            with database.engine.connect() as connection:
                connection.execute(
                    update(ArtifactCleanupRecord)
                    .where(ArtifactCleanupRecord.id == record.id)
                    .values(
                        state="claimed",
                        lease_token=replacement_token,
                        lease_expires_at=replacement_expiry,
                    )
                )
                connection.commit()
        return real_execute(session, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", replace_before_recovery_update)

    assert service.recover_expired_leases() == 0
    current = service.get_record(record.id)
    assert current is not None
    assert current.state == "claimed"
    assert current.lease_token == replacement_token
    assert current.lease_expires_at == replacement_expiry
    assert current.attempt_count == 0


def test_ambiguous_link_is_needs_human_and_retained(cleanup_environment) -> None:
    _, runtime, clock, service, _ = cleanup_environment
    outside = runtime.parent / "outside.bin"
    outside.write_bytes(b"outside")
    linked = runtime / "orphaned" / "linked.bin"
    linked.parent.mkdir()
    try:
        linked.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation unavailable")
    candidate = ArtifactCleanupCandidate(
        owner_type="material",
        owner_id=str(uuid4()),
        relative_path="orphaned/linked.bin",
        expected_sha256=sha256(b"outside").hexdigest(),
        expected_size_bytes=7,
        reason="unpersisted_material",
        not_before=clock.now(),
    )
    record = service.enqueue(candidate)

    result = service.process_one(record.id)

    assert result.state == "needs_human"
    assert result.last_error_category == "ambiguous_path"
    assert outside.read_bytes() == b"outside"
    assert linked.exists()


def test_quarantine_then_delete_only_after_24_hours(cleanup_environment) -> None:
    _, runtime, clock, service, _ = cleanup_environment
    payload = b"delayed cleanup"
    record = service.enqueue(_candidate(runtime, clock, payload=payload))

    quarantined = service.process_one(record.id)

    assert quarantined.state == "quarantined"
    assert quarantined.quarantine_path is not None
    assert quarantined.quarantine_path.startswith(f"artifacts-quarantine/{record.id}/")
    quarantined_file = runtime / quarantined.quarantine_path
    assert quarantined_file.read_bytes() == payload
    assert not (runtime / record.relative_path).exists()
    assert service.process_one(record.id).state == "quarantined"

    clock.advance(timedelta(hours=24))
    deleted = service.process_one(record.id)

    assert deleted.state == "deleted"
    assert deleted.completed_at == clock.now()
    assert not quarantined_file.exists()


def test_live_reference_blocks_quarantine_and_retains_file(cleanup_environment) -> None:
    database, runtime, clock, service, _ = cleanup_environment
    candidate = _candidate(runtime, clock)
    record = service.enqueue(candidate)
    _insert_material_reference(
        database,
        material_id=candidate.owner_id,
        relative_path=candidate.relative_path,
        digest=candidate.expected_sha256,
        size_bytes=candidate.expected_size_bytes,
        created_at=clock.now(),
    )

    result = service.process_one(record.id)

    assert result.state == "needs_human"
    assert result.last_error_category == "live_reference"
    assert (runtime / candidate.relative_path).exists()


def test_changed_file_identity_is_needs_human_and_retained(cleanup_environment) -> None:
    _, runtime, clock, service, _ = cleanup_environment
    candidate = _candidate(runtime, clock)
    record = service.enqueue(candidate)
    target = runtime / candidate.relative_path
    target.write_bytes(b"different")

    result = service.process_one(record.id)

    assert result.state == "needs_human"
    assert result.last_error_category == "identity_mismatch"
    assert target.read_bytes() == b"different"


def test_missing_file_is_deleted_only_when_unreferenced(cleanup_environment) -> None:
    _, runtime, clock, service, _ = cleanup_environment
    candidate = _candidate(runtime, clock)
    record = service.enqueue(candidate)
    (runtime / candidate.relative_path).unlink()

    result = service.process_one(record.id)

    assert result.state == "deleted"
    assert result.last_error_category == "already_missing"


def test_run_due_once_processes_only_claimed_batch(cleanup_environment) -> None:
    _, runtime, clock, service, _ = cleanup_environment
    due = service.enqueue(_candidate(runtime, clock, relative_path="a/due.bin"))
    later_candidate = _candidate(runtime, clock, relative_path="b/later.bin")
    later_candidate = ArtifactCleanupCandidate(
        **{**later_candidate.__dict__, "not_before": clock.now() + timedelta(hours=1)}
    )
    later = service.enqueue(later_candidate)

    assert service.run_due_once(limit=10) == 1
    due_record = service.get_record(due.id)
    assert due_record is not None
    assert due_record.state == "quarantined", due_record
    assert service.get_record(later.id).state == "pending"  # type: ignore[union-attr]


def test_process_one_does_not_steal_another_workers_lease(cleanup_environment) -> None:
    _, runtime, clock, first, second = cleanup_environment
    record = first.enqueue(_candidate(runtime, clock))
    assert first.claim_due(limit=1) == [record.id]

    observed = second.process_one(record.id)

    assert observed.state == "claimed"
    assert (runtime / record.relative_path).exists()


def test_database_has_no_write_transaction_during_hash_or_move(
    cleanup_environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, runtime, clock, service, _ = cleanup_environment
    record = service.enqueue(_candidate(runtime, clock))
    import backend.app.features.content.cleanup as cleanup_module

    real_read = cleanup_module.read_contained_regular
    real_rename = cleanup_module.rename_contained_regular_to_directory

    def assert_no_write_then_read(*args, **kwargs):
        with database.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA data_version").scalar_one() >= 1
        return real_read(*args, **kwargs)

    def assert_no_write_then_rename(*args, **kwargs):
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO workbench_schema_migrations(name, applied_at) "
                "VALUES (?, CURRENT_TIMESTAMP)",
                (f"io-probe-{uuid4()}",),
            )
        return real_rename(*args, **kwargs)

    monkeypatch.setattr(cleanup_module, "read_contained_regular", assert_no_write_then_read)
    monkeypatch.setattr(
        cleanup_module,
        "rename_contained_regular_to_directory",
        assert_no_write_then_rename,
    )

    assert service.process_one(record.id).state == "quarantined"


def test_open_record_query_uses_persisted_state(cleanup_environment) -> None:
    database, runtime, clock, service, _ = cleanup_environment
    record = service.enqueue(_candidate(runtime, clock))
    service.process_one(record.id)
    with database.session() as session:
        persisted = session.scalar(
            select(ArtifactCleanupRecord).where(ArtifactCleanupRecord.id == record.id)
        )
        assert persisted is not None
        assert persisted.state == "quarantined"


def test_move_commit_failure_is_needs_human_with_quarantine_retained(
    cleanup_environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime, clock, service, _ = cleanup_environment
    record = service.enqueue(_candidate(runtime, clock))
    import backend.app.features.content.cleanup as cleanup_module

    real_rename = cleanup_module.rename_contained_regular_to_directory
    real_commit = Session.commit
    moved = False
    failed_once = False

    def record_move(*args, **kwargs):
        nonlocal moved
        result = real_rename(*args, **kwargs)
        moved = result.status == "trusted"
        return result

    def fail_first_post_move_commit(session: Session) -> None:
        nonlocal failed_once
        if moved and not failed_once:
            failed_once = True
            raise SQLAlchemyError("forced ambiguous quarantine commit")
        real_commit(session)

    monkeypatch.setattr(
        cleanup_module, "rename_contained_regular_to_directory", record_move
    )
    monkeypatch.setattr(Session, "commit", fail_first_post_move_commit)

    result = service.process_one(record.id)

    assert result.state == "needs_human"
    assert result.last_error_category == "quarantine_commit_ambiguous"
    assert not (runtime / record.relative_path).exists()
    retained = runtime / "artifacts-quarantine" / record.id / "material.bin"
    assert retained.exists()


def test_reference_created_during_move_blocks_finalization_and_retains_quarantine(
    cleanup_environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, runtime, clock, service, _ = cleanup_environment
    candidate = _candidate(runtime, clock)
    record = service.enqueue(candidate)
    import backend.app.features.content.cleanup as cleanup_module

    real_rename = cleanup_module.rename_contained_regular_to_directory

    def move_then_reference(*args, **kwargs):
        result = real_rename(*args, **kwargs)
        _insert_material_reference(
            database,
            material_id=str(uuid4()),
            relative_path=candidate.relative_path,
            digest=candidate.expected_sha256,
            size_bytes=candidate.expected_size_bytes,
            created_at=clock.now(),
        )
        return result

    monkeypatch.setattr(
        cleanup_module, "rename_contained_regular_to_directory", move_then_reference
    )

    result = service.process_one(record.id)

    assert result.state == "needs_human"
    assert result.last_error_category == "live_reference"
    assert (runtime / "artifacts-quarantine" / record.id / "material.bin").exists()


def test_quarantined_record_continues_after_service_restart(cleanup_environment) -> None:
    database, runtime, clock, service, _ = cleanup_environment
    record = service.enqueue(_candidate(runtime, clock))
    quarantined = service.process_one(record.id)
    assert quarantined.state == "quarantined"

    clock.advance(timedelta(hours=24))
    restarted = ArtifactCleanupService(database, runtime_dir=runtime, clock=clock.now)

    assert restarted.run_due_once(limit=1) == 1
    assert restarted.get_record(record.id).state == "deleted"  # type: ignore[union-attr]


def test_missing_file_with_live_reference_is_not_reported_deleted(cleanup_environment) -> None:
    database, runtime, clock, service, _ = cleanup_environment
    candidate = _candidate(runtime, clock)
    record = service.enqueue(candidate)
    (runtime / candidate.relative_path).unlink()
    _insert_material_reference(
        database,
        material_id=str(uuid4()),
        relative_path=candidate.relative_path,
        digest=candidate.expected_sha256,
        size_bytes=candidate.expected_size_bytes,
        created_at=clock.now(),
    )

    result = service.process_one(record.id)

    assert result.state == "needs_human"
    assert result.last_error_category == "live_reference"


def test_hardlink_reference_blocks_quarantine(cleanup_environment) -> None:
    database, runtime, clock, service, _ = cleanup_environment
    candidate = _candidate(runtime, clock)
    record = service.enqueue(candidate)
    alias = runtime / "referenced" / "alias.bin"
    alias.parent.mkdir()
    try:
        os.link(runtime / candidate.relative_path, alias)
    except OSError:
        pytest.skip("hardlink creation unavailable")
    _insert_material_reference(
        database,
        material_id=str(uuid4()),
        relative_path="referenced/alias.bin",
        digest=candidate.expected_sha256,
        size_bytes=candidate.expected_size_bytes,
        created_at=clock.now(),
    )

    result = service.process_one(record.id)

    assert result.state == "needs_human"
    assert result.last_error_category == "live_reference"
    assert (runtime / candidate.relative_path).exists()


def test_replaced_quarantine_identity_is_retained_for_human(cleanup_environment) -> None:
    _, runtime, clock, service, _ = cleanup_environment
    record = service.enqueue(_candidate(runtime, clock))
    quarantined = service.process_one(record.id)
    assert quarantined.quarantine_path is not None
    target = runtime / quarantined.quarantine_path
    target.write_bytes(b"replacement")
    clock.advance(timedelta(hours=24))

    result = service.process_one(record.id)

    assert result.state == "needs_human"
    assert result.last_error_category == "identity_mismatch"
    assert target.read_bytes() == b"replacement"


def test_original_path_reappearing_during_grace_period_blocks_delete(
    cleanup_environment,
) -> None:
    _, runtime, clock, service, _ = cleanup_environment
    record = service.enqueue(_candidate(runtime, clock))
    quarantined = service.process_one(record.id)
    assert quarantined.quarantine_path is not None
    original = runtime / record.relative_path
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(b"new occupant")
    clock.advance(timedelta(hours=24))

    result = service.process_one(record.id)

    assert result.state == "needs_human"
    assert result.last_error_category == "original_path_reappeared"
    assert original.read_bytes() == b"new occupant"
    assert (runtime / quarantined.quarantine_path).exists()


def test_owner_identity_reuse_at_another_path_is_needs_human(cleanup_environment) -> None:
    database, runtime, clock, service, _ = cleanup_environment
    candidate = _candidate(runtime, clock)
    record = service.enqueue(candidate)
    other = runtime / "referenced" / "other.bin"
    other.parent.mkdir()
    other.write_bytes(b"other")
    _insert_material_reference(
        database,
        material_id=candidate.owner_id,
        relative_path="referenced/other.bin",
        digest=sha256(b"other").hexdigest(),
        size_bytes=5,
        created_at=clock.now(),
    )

    result = service.process_one(record.id)

    assert result.state == "needs_human"
    assert result.last_error_category == "owner_identity_mismatch"
    assert (runtime / candidate.relative_path).exists()


def test_other_cleanup_hardlink_identity_blocks_quarantine(cleanup_environment) -> None:
    _, runtime, clock, service, _ = cleanup_environment
    first_candidate = _candidate(runtime, clock, relative_path="orphaned/first.bin")
    alias = runtime / "orphaned" / "second.bin"
    try:
        os.link(runtime / first_candidate.relative_path, alias)
    except OSError:
        pytest.skip("hardlink creation unavailable")
    first = service.enqueue(first_candidate)
    service.enqueue(
        ArtifactCleanupCandidate(
            owner_type="material",
            owner_id=str(uuid4()),
            relative_path="orphaned/second.bin",
            expected_sha256=first_candidate.expected_sha256,
            expected_size_bytes=first_candidate.expected_size_bytes,
            reason="unpersisted_material",
            not_before=clock.now(),
        )
    )

    result = service.process_one(first.id)

    assert result.state == "needs_human"
    assert result.last_error_category == "live_reference"
    assert (runtime / first_candidate.relative_path).exists()


def test_reference_created_after_delete_handle_open_blocks_deletion(
    cleanup_environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, runtime, clock, service, _ = cleanup_environment
    record = service.enqueue(_candidate(runtime, clock))
    quarantined = service.process_one(record.id)
    assert quarantined.quarantine_path is not None
    clock.advance(timedelta(hours=24))
    import backend.app.features.content.export as export_module

    real_open = export_module._open_delete_handle

    def open_then_reference(path: Path) -> int:
        descriptor = real_open(path)
        _insert_material_reference(
            database,
            material_id=str(uuid4()),
            relative_path=record.relative_path,
            digest=record.expected_sha256,
            size_bytes=record.expected_size_bytes,
            created_at=clock.now(),
        )
        return descriptor

    monkeypatch.setattr(export_module, "_open_delete_handle", open_then_reference)

    result = service.process_one(record.id)

    assert result.state == "needs_human"
    assert result.last_error_category == "live_reference"
    assert (runtime / quarantined.quarantine_path).exists()


def test_reference_created_after_authorization_cannot_race_final_delete(
    cleanup_environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, runtime, clock, service, _ = cleanup_environment
    record = service.enqueue(_candidate(runtime, clock))
    quarantined = service.process_one(record.id)
    assert quarantined.quarantine_path is not None
    clock.advance(timedelta(hours=24))
    import backend.app.features.content.export as export_module

    real_delete = export_module._delete_open_file
    writer_started = Event()
    writer_finished = Event()
    observed_states: list[str] = []
    writer_future = None

    def insert_only_if_cleanup_is_open() -> None:
        try:
            with database.engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
                writer_started.set()
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                state = connection.execute(
                    select(ArtifactCleanupRecord.state).where(
                        ArtifactCleanupRecord.id == record.id
                    )
                ).scalar_one()
                observed_states.append(state)
                if state != "deleted":
                    connection.execute(
                        text(
                            "INSERT INTO content_product_materials "
                            "(id,product_id,logical_name,logical_key,version,path,sha256,"
                            "size_bytes,media_type,kind,created_at) VALUES "
                            "(:id,:product_id,'material.bin','material.bin',1,:path,:sha256,"
                            ":size_bytes,'application/octet-stream','source',:created_at)"
                        ),
                        {
                            "id": str(uuid4()),
                            "product_id": str(uuid4()),
                            "path": record.relative_path,
                            "sha256": record.expected_sha256,
                            "size_bytes": record.expected_size_bytes,
                            "created_at": clock.now().isoformat(sep=" "),
                        },
                    )
                connection.commit()
        finally:
            writer_finished.set()

    def insert_after_authorization(descriptor: int) -> bool:
        nonlocal writer_future
        writer_future = pool.submit(insert_only_if_cleanup_is_open)
        assert writer_started.wait(1)
        assert not writer_finished.wait(0.1)
        return real_delete(descriptor)

    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(export_module, "_delete_open_file", insert_after_authorization)

        result = service.process_one(record.id)
        assert writer_future is not None
        writer_future.result(timeout=5)

    assert observed_states == ["deleted"]
    assert result.state == "deleted"
    assert not (runtime / quarantined.quarantine_path).exists()
    with database.engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM content_product_materials")
        ).scalar_one() == 0


def test_final_delete_commit_fault_recovers_from_missing_file(
    cleanup_environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime, clock, service, _ = cleanup_environment
    record = service.enqueue(_candidate(runtime, clock))
    quarantined = service.process_one(record.id)
    assert quarantined.quarantine_path is not None
    target = runtime / quarantined.quarantine_path
    clock.advance(timedelta(hours=24))
    real_commit = Connection.commit
    failed = False

    def fail_first_commit(connection: Connection) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise SQLAlchemyError("injected final commit fault")
        real_commit(connection)

    with monkeypatch.context() as context:
        context.setattr(Connection, "commit", fail_first_commit)
        ambiguous = service.process_one(record.id)

    assert failed is True
    assert ambiguous.state == "claimed"
    assert not target.exists()
    clock.advance(timedelta(minutes=6))
    assert service.recover_expired_leases() == 1

    recovered = service.process_one(record.id)

    assert recovered.state == "deleted"
    assert recovered.last_error_category == "already_missing"


def test_same_bytes_replaced_in_quarantine_are_not_deleted(cleanup_environment) -> None:
    _, runtime, clock, service, _ = cleanup_environment
    payload = b"same bytes, new file identity"
    record = service.enqueue(_candidate(runtime, clock, payload=payload))
    quarantined = service.process_one(record.id)
    assert quarantined.quarantine_path is not None
    target = runtime / quarantined.quarantine_path
    target.unlink()
    target.write_bytes(payload)
    clock.advance(timedelta(hours=24))

    result = service.process_one(record.id)

    assert result.state == "needs_human"
    assert result.last_error_category == "quarantine_identity_changed"
    assert target.read_bytes() == payload


def test_missing_branch_cannot_finalize_with_expired_lease(cleanup_environment) -> None:
    _, runtime, clock, service, _ = cleanup_environment
    candidate = _candidate(runtime, clock)
    record = service.enqueue(candidate)
    (runtime / candidate.relative_path).unlink()
    assert service.claim_due(limit=1) == [record.id]
    clock.advance(timedelta(minutes=6))

    result = service.process_one(record.id)

    assert result.state == "claimed"
    assert result.completed_at is None


def test_candidate_rejects_oversize_and_overlong_path_before_database(
    cleanup_environment,
) -> None:
    _, runtime, clock, service, _ = cleanup_environment
    with pytest.raises(ValueError):
        ArtifactCleanupCandidate(
            owner_type="material",
            owner_id=str(uuid4()),
            relative_path="a/" + "x" * 999,
            expected_sha256="a" * 64,
            expected_size_bytes=1,
            reason="invalid",
            not_before=clock.now(),
        )
    with pytest.raises(ValueError):
        ArtifactCleanupCandidate(
            owner_type="material",
            owner_id=str(uuid4()),
            relative_path="a/file.bin",
            expected_sha256="a" * 64,
            expected_size_bytes=250 * 1024 * 1024 + 1,
            reason="invalid",
            not_before=clock.now(),
        )
    assert service.list_records() == []


def test_grace_deadline_starts_after_successful_move_verification(
    cleanup_environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime, clock, service, _ = cleanup_environment
    record = service.enqueue(_candidate(runtime, clock))
    import backend.app.features.content.cleanup as cleanup_module

    real_rename = cleanup_module.rename_contained_regular_to_directory

    def slow_move(*args, **kwargs):
        result = real_rename(*args, **kwargs)
        clock.advance(timedelta(minutes=1))
        return result

    monkeypatch.setattr(
        cleanup_module, "rename_contained_regular_to_directory", slow_move
    )

    result = service.process_one(record.id)

    assert result.state == "quarantined"
    assert result.not_before == clock.now() + timedelta(hours=24)


def test_target_parent_swap_never_moves_artifact_outside_runtime(
    cleanup_environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime, clock, service, _ = cleanup_environment
    payload = b"must stay in runtime"
    record = service.enqueue(_candidate(runtime, clock, payload=payload))
    outside = runtime.parent / "outside-quarantine"
    outside.mkdir()
    import backend.app.features.content.export as export_module

    real_rename = export_module._rename_open_file
    swap_attempted = False

    def swap_target_parent(descriptor: int, root_handle: int, target_name: str) -> bool:
        nonlocal swap_attempted
        swap_attempted = True
        target_parent = runtime / "artifacts-quarantine" / record.id
        displaced = target_parent.with_name(target_parent.name + "-original")
        try:
            target_parent.rename(displaced)
        except OSError:
            return real_rename(descriptor, root_handle, target_name)
        target_parent.symlink_to(outside, target_is_directory=True)
        return real_rename(descriptor, root_handle, target_name)

    monkeypatch.setattr(export_module, "_rename_open_file", swap_target_parent)

    result = service.process_one(record.id)

    assert result.state == "quarantined"
    assert swap_attempted is True
    assert not (outside / "material.bin").exists()
    retained = list(runtime.rglob("material.bin"))
    assert retained and retained[0].read_bytes() == payload
