"""Regression tests for S2A fix round 3/5."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
import hashlib
import os
import sqlite3
from threading import Thread
from uuid import uuid4

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from backend.app.adapters.contracts import CollectionItem, CollectionRequest, CollectionResult
from backend.app.db import Database, SchemaMigrationError
from backend.app.features.xhs.models import (
    XhsAccountNoteRecord,
    XhsAccountProfileRecord,
    XhsArtifactPromotionJournalRecord,
)
import backend.app.features.xhs.staging_cleanup as staging_module
import backend.app.features.xhs.service as service_module
from backend.app.features.xhs.service import ArtifactCommitUnknown, XhsCollectionService
from backend.app.models.jobs import JobRecord, JobState
from backend.app.services.jobs import JobService


JOURNAL_MIGRATION = "xhs_artifact_promotion_journal_v2"
JOURNAL_TABLE = "xhs_artifact_promotion_journal"


def _account_result(user_id: str = "round3-user") -> CollectionResult:
    return CollectionResult(
        status="succeeded",
        items=[
            CollectionItem(
                id=f"profile:{user_id}",
                kind="profile",
                source_url=f"https://www.xiaohongshu.com/user/profile/{user_id}",
                raw_evidence={"profile": {"user_id": user_id}},
                data={"user_id": user_id, "nickname": "Round 3"},
            ),
            CollectionItem(
                id="note:round3-note",
                kind="note",
                source_url="https://www.xiaohongshu.com/explore/round3-note",
                raw_evidence={"row": {"note_id": "round3-note", "user_id": user_id}},
                data={"note_id": "round3-note", "user_id": user_id},
            ),
        ],
        expected_count_known=True,
        expected_count=2,
        succeeded_count=2,
        observed_count=2,
        overflow_count=0,
        complete=True,
    )


class _Adapter:
    def fetch_account(self, request: CollectionRequest) -> CollectionResult:
        return _account_result(str(request.parameters["user_id"]))


def _service(tmp_path: Path) -> XhsCollectionService:
    runtime = tmp_path / "runtime"
    database = Database(runtime / "workbench.sqlite3", runtime_dir=runtime)
    return XhsCollectionService(
        database=database,
        job_service=JobService(database, runtime_dir=runtime),
        adapter=_Adapter(),
        runtime_dir=runtime,
        submitter=lambda *_args: None,
    )


def test_fresh_database_installs_valid_artifact_promotion_journal(
    tmp_path: Path,
) -> None:
    database = Database(
        tmp_path / "workbench.sqlite3",
        runtime_dir=tmp_path / "runtime",
    )

    inspector = inspect(database.engine)
    assert JOURNAL_TABLE in inspector.get_table_names()
    assert {column["name"] for column in inspector.get_columns(JOURNAL_TABLE)} == {
        "id",
        "job_id",
        "artifact_kind",
        "producer",
        "stage_path",
        "final_path",
        "sha256",
        "size_bytes",
        "file_dev",
        "file_ino",
        "file_mtime_ns",
        "target_state",
        "state",
        "resolution",
        "artifact_id",
        "created_at",
        "updated_at",
        "completed_at",
        "owner_token",
        "recovery_lease_expires_at",
    }
    foreign_keys = {
        (tuple(item["constrained_columns"]), item["referred_table"], item["options"].get("ondelete"))
        for item in inspector.get_foreign_keys(JOURNAL_TABLE)
    }
    assert (("job_id",), "jobs", "RESTRICT") in foreign_keys
    assert (("artifact_id",), "job_artifacts", "RESTRICT") in foreign_keys
    with database.engine.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT COUNT(*) FROM workbench_schema_migrations "
                "WHERE name=:name"
            ),
            {"name": JOURNAL_MIGRATION},
        ) == 1
    database.close()


def _journal_disk_state(path: Path) -> tuple[object, ...]:
    with sqlite3.connect(path) as connection:
        return (
            connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name IN (?, ?) OR (type='trigger' AND tbl_name=?) "
                "ORDER BY type, name",
                (
                    JOURNAL_TABLE,
                    "xhs_artifact_promotion_journal_v0",
                    JOURNAL_TABLE,
                ),
            ).fetchall(),
            connection.execute(
                "SELECT name, applied_at FROM workbench_schema_migrations "
                "WHERE name=?",
                (JOURNAL_MIGRATION,),
            ).fetchall(),
        )


def test_journal_marker_present_is_validation_only_when_table_is_missing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "marker.sqlite3"
    database = Database(path, runtime_dir=tmp_path / "runtime")
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute(f"DROP TABLE {JOURNAL_TABLE}")
    before = _journal_disk_state(path)

    with pytest.raises(SchemaMigrationError, match="journal schema validation"):
        Database(path, runtime_dir=tmp_path / "runtime")

    assert _journal_disk_state(path) == before


def test_missing_journal_marker_repairs_only_an_empty_malformed_table(
    tmp_path: Path,
) -> None:
    path = tmp_path / "empty-malformed.sqlite3"
    database = Database(path, runtime_dir=tmp_path / "runtime")
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM workbench_schema_migrations WHERE name=?",
            (JOURNAL_MIGRATION,),
        )
        connection.execute(f"DROP TABLE {JOURNAL_TABLE}")
        connection.execute(f"CREATE TABLE {JOURNAL_TABLE}(id TEXT PRIMARY KEY)")

    repaired = Database(path, runtime_dir=tmp_path / "runtime")

    assert {column["name"] for column in inspect(repaired.engine).get_columns(JOURNAL_TABLE)} == {
        "id", "job_id", "artifact_kind", "producer", "stage_path", "final_path",
        "sha256", "size_bytes", "file_dev", "file_ino", "file_mtime_ns",
        "target_state", "state", "resolution", "artifact_id", "created_at",
        "updated_at", "completed_at", "owner_token",
        "recovery_lease_expires_at",
    }
    repaired.close()


def test_missing_journal_marker_never_guesses_a_populated_malformed_table(
    tmp_path: Path,
) -> None:
    path = tmp_path / "populated-malformed.sqlite3"
    database = Database(path, runtime_dir=tmp_path / "runtime")
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM workbench_schema_migrations WHERE name=?",
            (JOURNAL_MIGRATION,),
        )
        connection.execute(f"DROP TABLE {JOURNAL_TABLE}")
        connection.execute(f"CREATE TABLE {JOURNAL_TABLE}(id TEXT PRIMARY KEY)")
        connection.execute(f"INSERT INTO {JOURNAL_TABLE}(id) VALUES ('unknown')")

    with pytest.raises(SchemaMigrationError, match="manual migration"):
        Database(path, runtime_dir=tmp_path / "runtime")

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            f"SELECT id FROM {JOURNAL_TABLE}"
        ).fetchall() == [("unknown",)]


def test_journal_half_migration_leftover_fails_every_restart_without_changes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "half.sqlite3"
    database = Database(path, runtime_dir=tmp_path / "runtime")
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE xhs_artifact_promotion_journal_v0(id TEXT PRIMARY KEY)"
        )
        connection.execute(
            "INSERT INTO xhs_artifact_promotion_journal_v0 VALUES ('preserve-me')"
        )
    before = _journal_disk_state(path)

    for _attempt in range(2):
        with pytest.raises(SchemaMigrationError, match="Incomplete XHS artifact"):
            Database(path, runtime_dir=tmp_path / "runtime")
        assert _journal_disk_state(path) == before


def test_journal_physical_triggers_reject_binding_mutation_and_deletion(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    queued = service.submit_account("round3-user", 1)
    completed = service.execute(queued.id)
    assert completed is not None and completed.state is JobState.succeeded

    with service.database.session() as session:
        journal = session.scalar(select(XhsArtifactPromotionJournalRecord))
        assert journal is not None
        journal.artifact_kind = "wrong-kind"
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    with service.database.session() as session:
        journal = session.scalar(select(XhsArtifactPromotionJournalRecord))
        assert journal is not None
        session.delete(journal)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
    service.database.close()


def test_journal_marker_requires_all_physical_triggers_without_repair(
    tmp_path: Path,
) -> None:
    path = tmp_path / "trigger-marker.sqlite3"
    database = Database(path, runtime_dir=tmp_path / "runtime")
    with database.engine.connect() as connection:
        trigger_names = set(connection.execute(text(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'ck_xhs_artifact_journal_%'"
        )).scalars())
    database.close()
    assert trigger_names == {
        "ck_xhs_artifact_journal_binding_insert",
        "ck_xhs_artifact_journal_binding_update",
        "ck_xhs_artifact_journal_no_delete",
        "ck_xhs_artifact_journal_job_update",
        "ck_xhs_artifact_journal_job_delete",
        "ck_xhs_artifact_journal_artifact_update",
        "ck_xhs_artifact_journal_artifact_delete",
    }
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER ck_xhs_artifact_journal_binding_update")
    before = _journal_disk_state(path)

    with pytest.raises(SchemaMigrationError, match="journal schema validation"):
        Database(path, runtime_dir=tmp_path / "runtime")

    assert _journal_disk_state(path) == before


def test_handle_bound_store_writes_promotes_and_demotes_one_exact_identity(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    journal_id = str(uuid4())
    job_id = str(uuid4())
    stage_name = f"{job_id}-{journal_id.replace('-', '')}.stage"
    final_name = f"{job_id}.json"
    payload = b'{"bounded":"evidence"}'
    store_type = getattr(staging_module, "TrustedXhsArtifactStore")

    with store_type(runtime, max_bytes=4096) as store:
        identity = store.create_stage(stage_name, payload)
        assert store.read_stage(stage_name, identity) == payload

        promoted = store.promote(stage_name, final_name, identity)
        assert promoted == identity
        assert store.inspect_stage(stage_name).status == "missing"
        assert store.read_final(final_name, identity) == payload

        discarded = store.demote_and_discard(final_name, stage_name, identity)
        if os.name == "nt":
            assert discarded is True
            assert store.inspect_final(final_name).status == "missing"
            assert store.inspect_stage(stage_name).status == "missing"
        else:
            assert discarded is False
            assert store.inspect_final(final_name).status == "missing"
            assert store.inspect_stage(stage_name).status == "trusted"

    assert not list((runtime / "evidence" / "xhs").glob("*.json"))


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point semantics")
def test_handle_bound_store_rejects_staging_reparse_without_touching_target(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    xhs_root = runtime / "evidence" / "xhs"
    xhs_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, xhs_root / ".staging", target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlink unavailable: {error}")
    store_type = getattr(staging_module, "TrustedXhsArtifactStore")
    error_type = getattr(staging_module, "UnsafeXhsArtifactStore")

    with pytest.raises(error_type):
        store_type(runtime, max_bytes=4096)

    assert list(outside.iterdir()) == []


def test_handle_bound_store_rejects_stage_name_identity_swap_before_promotion(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    journal_id = str(uuid4())
    job_id = str(uuid4())
    stage_name = f"{job_id}-{journal_id.replace('-', '')}.stage"
    final_name = f"{job_id}.json"

    with staging_module.TrustedXhsArtifactStore(runtime, max_bytes=4096) as store:
        identity = store.create_stage(stage_name, b"trusted-original")
        stage = runtime / "evidence" / "xhs" / ".staging" / stage_name
        replacement = stage.with_name(f"{uuid4()}.replacement")
        replacement.write_bytes(b"attacker-replacement")
        try:
            os.replace(replacement, stage)
        except PermissionError:
            # Windows may deny the name swap while the exact stage handle is held.
            assert store.read_stage(stage_name, identity) == b"trusted-original"
            assert store.inspect_final(final_name).status == "missing"
            return

        with pytest.raises(
            staging_module.UnsafeXhsArtifactStore,
            match="name identity changed",
        ):
            store.promote(stage_name, final_name, identity)

        assert store.inspect_final(final_name).status == "missing"
        assert stage.read_bytes() == b"attacker-replacement"


def test_handle_bound_store_rejects_stage_with_an_added_hardlink(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    journal_id = str(uuid4())
    job_id = str(uuid4())
    stage_name = f"{job_id}-{journal_id.replace('-', '')}.stage"
    final_name = f"{job_id}.json"

    with staging_module.TrustedXhsArtifactStore(runtime, max_bytes=4096) as store:
        identity = store.create_stage(stage_name, b"single-link-only")
        stage = runtime / "evidence" / "xhs" / ".staging" / stage_name
        alias = tmp_path / "untrusted-alias"
        try:
            os.link(stage, alias)
        except PermissionError:
            assert store.read_stage(stage_name, identity) == b"single-link-only"
            return

        with pytest.raises(
            staging_module.UnsafeXhsArtifactStore,
            match="bounded regular file",
        ):
            store.promote(stage_name, final_name, identity)
        assert store.inspect_final(final_name).status == "missing"


def test_promotion_crash_is_reconciled_from_durable_prepared_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    queued = service.submit_account("round3-user", 1)
    service.job_service.claim(queued.id)
    claimed = service.job_service.get(queued.id)

    class SimulatedCrash(BaseException):
        pass

    def crash_after_promotion() -> None:
        raise SimulatedCrash("process stopped after promotion")

    monkeypatch.setattr(
        service_module,
        "_artifact_promotion_after_atomic_hook",
        crash_after_promotion,
    )

    with pytest.raises(SimulatedCrash, match="after promotion"):
        service._finalize_result(claimed, _account_result())

    with service.database.session() as session:
        journal = session.scalar(select(XhsArtifactPromotionJournalRecord))
        assert journal is not None and journal.state == "prepared"
        final_path = service.runtime_dir / journal.final_path
        assert final_path.is_file()
        journal.recovery_lease_expires_at = datetime(2000, 1, 1)
        session.commit()
    service.database.close()
    monkeypatch.setattr(
        service_module,
        "_artifact_promotion_after_atomic_hook",
        None,
    )

    restarted = _service(tmp_path)

    recovered = restarted.job_service.get(queued.id)
    assert recovered.state is JobState.needs_human
    assert recovered.error_category == "artifact_commit_rolled_back"
    with restarted.database.session() as session:
        journal = session.scalar(select(XhsArtifactPromotionJournalRecord))
        assert journal is not None
        assert (journal.state, journal.resolution) == ("completed", "rolled_back")
        assert session.scalars(select(XhsAccountProfileRecord)).all() == []
        assert session.scalars(select(XhsAccountNoteRecord)).all() == []
    evidence_root = restarted.runtime_dir / "evidence" / "xhs"
    assert not [path for path in evidence_root.rglob("*") if path.is_file()]
    restarted.database.close()


def test_commit_landed_ack_lost_and_probe_fault_never_removes_formal_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    queued = service.submit_account("round3-user", 1)
    service.job_service.claim(queued.id)
    claimed = service.job_service.get(queued.id)
    original_session = service.database.session
    ack_lost = False

    class CommitAckProxy:
        def __init__(self, session: object) -> None:
            self._session = session

        def __getattr__(self, name: str) -> object:
            return getattr(self._session, name)

        def commit(self) -> None:
            nonlocal ack_lost
            final_state = self._session.get(JobRecord, queued.id).state
            self._session.commit()
            if not ack_lost and JobState(final_state) is JobState.succeeded:
                ack_lost = True
                raise RuntimeError("commit acknowledgement lost")

    @contextmanager
    def uncertain_session():
        with original_session() as session:
            yield CommitAckProxy(session)

    service.database.session = uncertain_session
    probe_calls = 0

    def unreadable_probe(*_args: object, **_kwargs: object) -> object:
        nonlocal probe_calls
        probe_calls += 1
        raise OSError("controlled read fault")

    monkeypatch.setattr(service, "_probe_commit_outcome", unreadable_probe)

    with pytest.raises(ArtifactCommitUnknown, match="artifact_commit_unknown"):
        service._finalize_result(claimed, _account_result())

    assert ack_lost is True and probe_calls >= 2
    with original_session() as session:
        journal = session.scalar(select(XhsArtifactPromotionJournalRecord))
        assert journal is not None
        assert (journal.state, journal.resolution) == ("completed", "committed")
        final = service.runtime_dir / journal.final_path
        before = final.read_bytes()
        assert hashlib.sha256(before).hexdigest() == journal.sha256
    service.database.close()

    restarted = _service(tmp_path)
    durable = restarted.job_service.get(queued.id)
    assert durable.state is JobState.succeeded
    with restarted.database.session() as session:
        journal = session.scalar(select(XhsArtifactPromotionJournalRecord))
        assert journal is not None
        assert (restarted.runtime_dir / journal.final_path).read_bytes() == before
    restarted.database.close()


def test_prepared_journal_ack_unknown_preserves_stage_until_restart_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    queued = service.submit_account("round3-user", 1)
    service.job_service.claim(queued.id)
    claimed = service.job_service.get(queued.id)
    original_session = service.database.session
    ack_lost = False

    class PreparedAckProxy:
        def __init__(self, session: object) -> None:
            self._session = session

        def __getattr__(self, name: str) -> object:
            return getattr(self._session, name)

        def commit(self) -> None:
            nonlocal ack_lost
            prepared = self._session.scalar(
                select(XhsArtifactPromotionJournalRecord).where(
                    XhsArtifactPromotionJournalRecord.job_id == queued.id,
                    XhsArtifactPromotionJournalRecord.state == "prepared",
                )
            )
            self._session.commit()
            if prepared is not None and not ack_lost:
                ack_lost = True
                raise RuntimeError("prepared journal acknowledgement lost")

    @contextmanager
    def uncertain_session():
        with original_session() as session:
            yield PreparedAckProxy(session)

    service.database.session = uncertain_session
    probes = 0

    def unreadable_prepared_probe(*_args: object, **_kwargs: object) -> object:
        nonlocal probes
        probes += 1
        raise OSError("prepared journal read fault")

    monkeypatch.setattr(
        service,
        "_probe_prepared_journal_outcome",
        unreadable_prepared_probe,
    )

    with pytest.raises(ArtifactCommitUnknown, match="journal_prepare_unknown"):
        service._finalize_result(claimed, _account_result())

    assert ack_lost is True and probes >= 2
    with original_session() as session:
        journal = session.scalar(select(XhsArtifactPromotionJournalRecord))
        assert journal is not None and journal.state == "prepared"
        stage = service.runtime_dir / journal.stage_path
        final = service.runtime_dir / journal.final_path
        assert stage.is_file() and not final.exists()
        journal.recovery_lease_expires_at = datetime(2000, 1, 1)
        session.commit()
    service.database.close()

    restarted = _service(tmp_path)
    recovered = restarted.job_service.get(queued.id)
    assert recovered.state is JobState.needs_human
    assert recovered.error_category == "artifact_commit_rolled_back"
    assert not stage.exists() and not final.exists()
    restarted.database.close()


def test_commit_not_landed_demotes_exact_file_and_requires_human(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    queued = service.submit_account("round3-user", 1)
    service.job_service.claim(queued.id)
    claimed = service.job_service.get(queued.id)
    original_session = service.database.session
    rejected = False

    class PrecommitProxy:
        def __init__(self, session: object) -> None:
            self._session = session

        def __getattr__(self, name: str) -> object:
            return getattr(self._session, name)

        def commit(self) -> None:
            nonlocal rejected
            final_state = self._session.get(JobRecord, queued.id).state
            if not rejected and JobState(final_state) is JobState.succeeded:
                rejected = True
                raise RuntimeError("commit did not land")
            self._session.commit()

    @contextmanager
    def precommit_session():
        with original_session() as session:
            yield PrecommitProxy(session)

    service.database.session = precommit_session

    recovered = service._finalize_result(claimed, _account_result())

    assert rejected is True
    assert recovered is not None
    assert recovered.state is JobState.needs_human
    assert recovered.error_category == "artifact_commit_rolled_back"
    with original_session() as session:
        journal = session.scalar(select(XhsArtifactPromotionJournalRecord))
        assert journal is not None
        assert (journal.state, journal.resolution) == ("completed", "rolled_back")
        assert session.scalars(select(XhsAccountProfileRecord)).all() == []
        assert session.scalars(select(XhsAccountNoteRecord)).all() == []
    evidence_root = service.runtime_dir / "evidence" / "xhs"
    assert not [path for path in evidence_root.rglob("*") if path.is_file()]
    service.database.close()


def test_restart_restores_committed_evidence_from_exact_trusted_stage(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    queued = service.submit_account("round3-user", 1)
    completed = service.execute(queued.id)
    assert completed is not None and completed.state is JobState.succeeded
    with service.database.session() as session:
        journal = session.scalar(select(XhsArtifactPromotionJournalRecord))
        assert journal is not None
        final = service.runtime_dir / journal.final_path
        stage = service.runtime_dir / journal.stage_path
        digest = journal.sha256
    stage.parent.mkdir(parents=True, exist_ok=True)
    os.replace(final, stage)
    service.database.close()

    restarted = _service(tmp_path)

    assert restarted.job_service.get(queued.id).state is JobState.succeeded
    assert final.is_file() and not stage.exists()
    assert hashlib.sha256(final.read_bytes()).hexdigest() == digest
    restarted.database.close()


def test_restart_never_deletes_replaced_formal_file_and_is_idempotent(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    queued = service.submit_account("round3-user", 1)
    completed = service.execute(queued.id)
    assert completed is not None
    with service.database.session() as session:
        journal = session.scalar(select(XhsArtifactPromotionJournalRecord))
        assert journal is not None
        final = service.runtime_dir / journal.final_path
    service.database.close()
    final.unlink()
    replacement = b"replacement-must-survive"
    final.write_bytes(replacement)

    first = _service(tmp_path)
    assert first.job_service.get(queued.id).state is JobState.needs_human
    assert first.job_service.get(queued.id).error_category == "artifact_evidence_inconsistent"
    assert final.read_bytes() == replacement
    first.database.close()

    second = _service(tmp_path)
    assert second.job_service.get(queued.id).state is JobState.needs_human
    assert final.read_bytes() == replacement
    with second.database.session() as session:
        journal = session.scalar(select(XhsArtifactPromotionJournalRecord))
        assert journal is not None
        assert (journal.state, journal.resolution) == ("completed", "inconsistent")
    second.database.close()


def test_concurrent_restart_reconciliation_is_repeatable_and_ignores_unjournaled_json(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    queued = service.submit_account("round3-user", 1)
    completed = service.execute(queued.id)
    assert completed is not None
    with service.database.session() as session:
        journal = session.scalar(select(XhsArtifactPromotionJournalRecord))
        assert journal is not None
        final = service.runtime_dir / journal.final_path
        stage = service.runtime_dir / journal.stage_path
    orphan = service.runtime_dir / "evidence" / "xhs" / "unjournaled.json"
    orphan.write_bytes(b"not-owned-by-journal")
    os.replace(final, stage)
    service.database.close()

    database = Database(
        tmp_path / "runtime" / "workbench.sqlite3",
        runtime_dir=tmp_path / "runtime",
    )
    jobs = JobService(database, runtime_dir=tmp_path / "runtime")
    services: list[XhsCollectionService] = []
    errors: list[BaseException] = []

    def construct() -> None:
        try:
            services.append(XhsCollectionService(
                database=database,
                job_service=jobs,
                adapter=_Adapter(),
                runtime_dir=tmp_path / "runtime",
                submitter=lambda *_args: None,
            ))
        except BaseException as error:
            errors.append(error)

    threads = [Thread(target=construct), Thread(target=construct)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert errors == []
    assert len(services) == 2
    assert jobs.get(queued.id).state is JobState.succeeded
    assert final.is_file() and not stage.exists()
    assert orphan.read_bytes() == b"not-owned-by-journal"
    database.close()
