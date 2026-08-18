"""Regression tests for S2A fix round 4/5."""

from __future__ import annotations

import json
import inspect as python_inspect
import os
from pathlib import Path
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from threading import Event, Thread
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from backend.app.adapters.contracts import CollectionItem, CollectionRequest, CollectionResult
import backend.app.db as db_module
from backend.app.db import Database, SchemaMigrationError
from backend.app.features.xhs.models import XhsArtifactPromotionJournalRecord
import backend.app.features.xhs.staging_cleanup as staging_module
from backend.app.features.xhs.service import (
    ArtifactCommitUnknown,
    CollectionFactNotFound,
    XhsCollectionService,
)
from backend.app.features.xhs.staging_cleanup import UnsafeXhsArtifactStore
from backend.app.models.jobs import JobArtifactRecord, JobState
from backend.app.services.jobs import JobService


class _Adapter:
    def fetch_account(self, request: CollectionRequest) -> CollectionResult:
        user_id = str(request.parameters["user_id"])
        return CollectionResult(
            status="succeeded",
            items=[
                CollectionItem(
                    id=f"profile:{user_id}",
                    kind="profile",
                    source_url=f"https://www.xiaohongshu.com/user/profile/{user_id}",
                    raw_evidence={"profile": {"user_id": user_id}},
                    data={"user_id": user_id, "nickname": "Round 4"},
                ),
                CollectionItem(
                    id="note:round4-note",
                    kind="note",
                    source_url="https://www.xiaohongshu.com/explore/round4-note",
                    raw_evidence={
                        "row": {"note_id": "round4-note", "user_id": user_id}
                    },
                    data={"note_id": "round4-note", "user_id": user_id},
                ),
            ],
            expected_count_known=True,
            expected_count=2,
            succeeded_count=2,
            observed_count=2,
            overflow_count=0,
            complete=True,
        )

    def search_notes(self, request: CollectionRequest) -> CollectionResult:
        return CollectionResult(
            status="succeeded",
            items=[
                CollectionItem(
                    id="note:round4-search",
                    kind="note",
                    source_url="https://www.xiaohongshu.com/explore/round4-search",
                    raw_evidence={"row": {"note_id": "round4-search"}},
                    data={"note_id": "round4-search", "title": "Round 4"},
                )
            ],
            expected_count_known=True,
            expected_count=1,
            succeeded_count=1,
            observed_count=1,
            overflow_count=0,
            complete=True,
        )


def _service(
    tmp_path: Path,
    *,
    clock=None,
) -> XhsCollectionService:
    runtime = tmp_path / "runtime"
    database = Database(runtime / "workbench.sqlite3", runtime_dir=runtime)
    return XhsCollectionService(
        database=database,
        job_service=JobService(database, runtime_dir=runtime),
        adapter=_Adapter(),
        runtime_dir=runtime,
        submitter=lambda *_args: None,
        clock=clock,
    )


def _completed_account(service: XhsCollectionService, user_id: str = "round4-user"):
    queued = service.submit_account(user_id, 1)
    completed = service.execute(queued.id)
    assert completed is not None and completed.state is JobState.succeeded
    return completed


def _completed_search(service: XhsCollectionService):
    queued = service.submit_search("round4", 1)
    completed = service.execute(queued.id)
    assert completed is not None and completed.state is JobState.succeeded
    return completed


def _tamper_artifact_metadata(
    service: XhsCollectionService,
    artifact_id: int,
    metadata: dict[str, object],
) -> None:
    """Simulate pre-hardening/on-disk corruption, then restore the physical guard."""

    trigger_name = "ck_xhs_artifact_journal_artifact_update"
    with service.database.engine.begin() as connection:
        definition = connection.scalar(
            text(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=:name"
            ),
            {"name": trigger_name},
        )
        if definition is not None:
            connection.execute(text(f"DROP TRIGGER {trigger_name}"))
        connection.execute(
            text(
                "UPDATE job_artifacts SET metadata_json=:metadata WHERE id=:artifact_id"
            ),
            {
                "artifact_id": artifact_id,
                "metadata": json.dumps(metadata, separators=(",", ":")),
            },
        )
        if definition is not None:
            connection.execute(text(str(definition)))


def test_account_reads_fail_closed_after_durable_evidence_becomes_inconsistent(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    completed = _completed_account(service)
    with service.database.session() as session:
        journal = session.scalar(select(XhsArtifactPromotionJournalRecord))
        assert journal is not None
        journal_id = journal.id

    service._mark_artifact_inconsistent(journal_id)

    assert service.job_service.get(completed.id).state is JobState.needs_human
    with pytest.raises(CollectionFactNotFound):
        service.get_profile("round4-user")
    with pytest.raises(CollectionFactNotFound):
        service.list_account_notes("round4-user")
    with service.database.session() as session:
        assert session.execute(text("SELECT COUNT(*) FROM xhs_account_profiles")).scalar_one() == 1
        assert session.execute(text("SELECT COUNT(*) FROM xhs_account_notes")).scalar_one() == 1
    service.database.close()


def test_account_reads_require_the_handle_bound_formal_file_identity_and_hash(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    completed = _completed_account(service)
    formal = service.runtime_dir / completed.artifacts[0].path
    original = formal.read_bytes()
    formal.unlink()
    formal.write_bytes(b'{"replacement":"not-the-journaled-identity"}')

    with pytest.raises(CollectionFactNotFound):
        service.get_profile("round4-user")
    with pytest.raises(CollectionFactNotFound):
        service.list_account_notes("round4-user")
    assert formal.read_bytes() != original
    service.database.close()


@pytest.mark.parametrize("mode", ["account", "search"])
def test_formal_reads_share_one_strict_artifact_metadata_gate(
    tmp_path: Path,
    mode: str,
) -> None:
    service = _service(tmp_path)
    completed = (
        _completed_account(service)
        if mode == "account"
        else _completed_search(service)
    )
    artifact_id = int(completed.artifacts[0].metadata["artifact_id"])
    with service.database.session() as session:
        artifact = session.get(JobArtifactRecord, artifact_id)
        assert artifact is not None
        metadata = dict(artifact.metadata_json)
        metadata.pop("sha256")
    _tamper_artifact_metadata(service, artifact_id, metadata)

    if mode == "account":
        with pytest.raises(CollectionFactNotFound):
            service.get_profile("round4-user")
        with pytest.raises(CollectionFactNotFound):
            service.list_account_notes("round4-user")
    else:
        with pytest.raises(CollectionFactNotFound):
            service.get_search_results(completed.id)
    service.database.close()


def test_parent_job_and_artifact_guards_cover_every_trust_predicate(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    completed = _completed_account(service)
    artifact_id = int(completed.artifacts[0].metadata["artifact_id"])
    other_job = service.job_service.create(
        job_type="xhs_account_collection",
        input_data={"user_id": "other", "expected_note_count": 1},
    )
    mutations = [
        ("UPDATE jobs SET id=:value WHERE id=:job_id", str(uuid4())),
        ("UPDATE jobs SET type=:value WHERE id=:job_id", "wrong-type"),
        ("UPDATE jobs SET state=:value WHERE id=:job_id", "needs_human"),
        (
            "UPDATE jobs SET input_data=:value WHERE id=:job_id",
            json.dumps({"user_id": "wrong", "expected_note_count": 1}),
        ),
        ("UPDATE job_artifacts SET id=:value WHERE id=:artifact_id", artifact_id + 1000),
        (
            "UPDATE job_artifacts SET job_id=:value WHERE id=:artifact_id",
            other_job.id,
        ),
        (
            "UPDATE job_artifacts SET kind=:value WHERE id=:artifact_id",
            "wrong-kind",
        ),
        (
            "UPDATE job_artifacts SET producer=:value WHERE id=:artifact_id",
            "wrong-producer",
        ),
        (
            "UPDATE job_artifacts SET path=:value WHERE id=:artifact_id",
            "evidence/xhs/wrong.json",
        ),
        (
            "UPDATE job_artifacts SET metadata_json=:value WHERE id=:artifact_id",
            "{}",
        ),
    ]

    for statement, value in mutations:
        parameters = {
            "value": value,
            "job_id": completed.id,
            "artifact_id": artifact_id,
        }
        with pytest.raises(IntegrityError):
            with service.database.engine.begin() as connection:
                connection.execute(text(statement), parameters)

    for statement, parameters in [
        ("DELETE FROM job_artifacts WHERE id=:id", {"id": artifact_id}),
        ("DELETE FROM jobs WHERE id=:id", {"id": completed.id}),
    ]:
        with pytest.raises(IntegrityError):
            with service.database.engine.begin() as connection:
                connection.execute(text(statement), parameters)
    service.database.close()


def test_current_marker_requires_exact_bidirectional_trigger_set_without_repair(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    _completed_account(service)
    path = service.database.database_path
    with service.database.engine.connect() as connection:
        names = set(
            connection.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "AND name LIKE 'ck_xhs_artifact_journal_%'"
                )
            ).scalars()
        )
    assert names == {
        "ck_xhs_artifact_journal_binding_insert",
        "ck_xhs_artifact_journal_binding_update",
        "ck_xhs_artifact_journal_no_delete",
        "ck_xhs_artifact_journal_job_update",
        "ck_xhs_artifact_journal_job_delete",
        "ck_xhs_artifact_journal_artifact_update",
        "ck_xhs_artifact_journal_artifact_delete",
    }
    service.database.close()
    reopened = Database(path, runtime_dir=tmp_path / "runtime")
    with reopened.engine.begin() as connection:
        connection.execute(text("DROP TRIGGER ck_xhs_artifact_journal_job_update"))
    reopened.close()

    with pytest.raises(SchemaMigrationError, match="journal schema validation"):
        Database(path, runtime_dir=tmp_path / "runtime")


@pytest.mark.parametrize(
    "poison",
    [
        {},
        {"artifact_id": None, "sha256": None, "size_bytes": None},
        {"artifact_id": "1", "sha256": 7, "size_bytes": "1"},
    ],
)
def test_null_or_wrong_typed_artifact_metadata_fails_restart_data_scan(
    tmp_path: Path,
    poison: dict[str, object],
) -> None:
    service = _service(tmp_path)
    completed = _completed_account(service)
    path = service.database.database_path
    artifact_id = int(completed.artifacts[0].metadata["artifact_id"])
    _tamper_artifact_metadata(service, artifact_id, poison)
    with service.database.engine.connect() as connection:
        assert not db_module._xhs_artifact_promotion_journal_data_valid(connection)
    service.database.close()

    with pytest.raises(SchemaMigrationError, match="journal schema validation"):
        Database(path, runtime_dir=tmp_path / "runtime")


def test_v2_journal_installs_allocation_and_database_lease_constraints(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    completed = _completed_account(service)
    inspector = db_module.inspect(service.database.engine)
    columns = {
        column["name"]: column
        for column in inspector.get_columns("xhs_artifact_promotion_journal")
    }
    assert columns["file_dev"]["nullable"] is True
    assert columns["file_ino"]["nullable"] is True
    assert columns["file_mtime_ns"]["nullable"] is True
    assert columns["owner_token"]["nullable"] is True
    assert columns["recovery_lease_expires_at"]["nullable"] is True
    with service.database.engine.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT COUNT(*) FROM workbench_schema_migrations "
                "WHERE name='xhs_artifact_promotion_journal_v2'"
            )
        ) == 1
    with pytest.raises(IntegrityError):
        with service.database.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE xhs_artifact_promotion_journal "
                    "SET owner_token=:owner WHERE job_id=:job_id"
                ),
                {"owner": str(uuid4()), "job_id": completed.id},
            )
    service.database.close()


def test_v2_journal_identity_constraint_rejects_partial_nulls(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    completed = _completed_account(service)

    with pytest.raises(IntegrityError):
        with service.database.engine.begin() as connection:
            connection.execute(text(
                "UPDATE xhs_artifact_promotion_journal SET file_dev=NULL "
                "WHERE job_id=:job_id"
            ), {"job_id": completed.id})

    service.database.close()


def test_v2_restart_data_scan_rejects_bypassed_partial_identity(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    _completed_account(service)
    path = service.database.database_path
    with service.database.engine.begin() as connection:
        connection.execute(text("PRAGMA ignore_check_constraints=ON"))
        connection.execute(text(
            "UPDATE xhs_artifact_promotion_journal SET file_dev=NULL"
        ))
        connection.execute(text("PRAGMA ignore_check_constraints=OFF"))
    with service.database.engine.connect() as connection:
        assert not db_module._xhs_artifact_promotion_journal_data_valid(connection)
    service.database.close()

    with pytest.raises(SchemaMigrationError, match="journal schema validation"):
        Database(path, runtime_dir=tmp_path / "runtime")


def test_allocation_intent_is_committed_before_the_stage_is_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    queued = service.submit_account("round4-user", 1)
    observed: list[tuple[object, ...]] = []
    real_create = staging_module.TrustedXhsArtifactStore.create_stage

    def observed_create(store, name: str, payload: bytes):
        with service.database.session() as session:
            journal = session.scalar(
                select(XhsArtifactPromotionJournalRecord).where(
                    XhsArtifactPromotionJournalRecord.job_id == queued.id
                )
            )
            observed.append(
                (
                    None if journal is None else journal.state,
                    None if journal is None else journal.file_dev,
                    None if journal is None else journal.owner_token,
                    None if journal is None else journal.recovery_lease_expires_at,
                )
            )
        return real_create(store, name, payload)

    monkeypatch.setattr(
        staging_module.TrustedXhsArtifactStore,
        "create_stage",
        observed_create,
    )

    completed = service.execute(queued.id)

    assert completed is not None and completed.state is JobState.succeeded
    assert len(observed) == 1
    state, file_dev, owner, lease = observed[0]
    assert state == "allocating"
    assert file_dev is None
    assert isinstance(owner, str) and owner
    assert lease is not None
    service.database.close()


def test_side_effecting_stage_creation_failure_uses_held_handle_and_is_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    queued = service.submit_account("round4-user", 1)

    def fail_after_open(_descriptor: int) -> None:
        raise OSError("controlled stage creation failure")

    monkeypatch.setattr(
        staging_module,
        "_stage_creation_after_open_hook",
        fail_after_open,
        raising=False,
    )

    completed = service.execute(queued.id)

    assert completed is not None and completed.state is JobState.needs_human
    with service.database.session() as session:
        journal = session.scalar(select(XhsArtifactPromotionJournalRecord))
        assert journal is not None
        if os.name == "nt":
            assert (journal.state, journal.resolution) == ("completed", "rolled_back")
            assert completed.error_category == "artifact_stage_create_failed"
        else:
            assert (journal.state, journal.resolution) == ("completed", "inconsistent")
            assert completed.error_category == "artifact_stage_create_inconsistent"
    stages = list((service.runtime_dir / "evidence" / "xhs" / ".staging").glob("*.stage"))
    assert (stages == []) if os.name == "nt" else bool(stages)
    service.database.close()


def test_prepared_commit_unknown_keeps_a_durable_allocation_visible_to_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    queued = service.submit_account("round4-user", 1)
    service.job_service.claim(queued.id)
    claimed = service.job_service.get(queued.id)
    original_session = service.database.session
    rejected = False

    class PreparedPrecommitProxy:
        def __init__(self, session: object) -> None:
            self._session = session

        def __getattr__(self, name: str) -> object:
            return getattr(self._session, name)

        def commit(self) -> None:
            nonlocal rejected
            state = self._session.execute(
                text(
                    "SELECT state FROM xhs_artifact_promotion_journal "
                    "WHERE job_id=:job_id"
                ),
                {"job_id": queued.id},
            ).scalar_one_or_none()
            if state == "prepared" and not rejected:
                rejected = True
                raise RuntimeError("prepared commit did not land")
            self._session.commit()

    @contextmanager
    def uncertain_session():
        with original_session() as session:
            yield PreparedPrecommitProxy(session)

    service.database.session = uncertain_session
    monkeypatch.setattr(
        service,
        "_probe_prepared_journal_outcome",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("probe unavailable")),
    )

    with pytest.raises(ArtifactCommitUnknown, match="journal_prepare_unknown"):
        service._finalize_result(claimed, _Adapter().fetch_account(
            CollectionRequest(
                capability="fetch_account",
                parameters={"user_id": "round4-user"},
                expected_count=2,
            )
        ))

    assert rejected is True
    with original_session() as session:
        journal = session.scalar(select(XhsArtifactPromotionJournalRecord))
        assert journal is not None and journal.state == "allocating"
        stage = service.runtime_dir / journal.stage_path
        assert stage.is_file()
        session.execute(
            text(
                "UPDATE xhs_artifact_promotion_journal "
                "SET recovery_lease_expires_at='2000-01-01 00:00:00' "
                "WHERE id=:id"
            ),
            {"id": journal.id},
        )
        session.commit()
    service.database.close()

    restarted = _service(tmp_path)
    recovered = restarted.job_service.get(queued.id)
    assert recovered.state is JobState.needs_human
    assert recovered.error_category == "artifact_evidence_inconsistent"
    assert stage.is_file()
    restarted.database.close()


def test_active_finalizer_lease_survives_concurrent_restart_and_clock_skew(
    tmp_path: Path,
) -> None:
    first = _service(tmp_path)
    queued = first.submit_account("round4-user", 1)
    entered, release = Event(), Event()
    real_promote = first._promote_staged_artifact
    outcomes: list[object] = []

    def paused_promote(staged) -> None:
        entered.set()
        assert release.wait(10)
        real_promote(staged)

    first._promote_staged_artifact = paused_promote
    worker = Thread(target=lambda: outcomes.append(first.execute(queued.id)))
    worker.start()
    assert entered.wait(5)

    second = _service(tmp_path, clock=lambda: datetime(2099, 1, 1))

    assert second.job_service.get(queued.id).state is JobState.running
    with second.database.session() as session:
        journal = session.scalar(select(XhsArtifactPromotionJournalRecord))
        assert journal is not None
        assert journal.owner_token is not None
        assert journal.recovery_lease_expires_at is not None
    release.set()
    worker.join(10)
    assert not worker.is_alive()
    assert len(outcomes) == 1
    assert outcomes[0] is not None and outcomes[0].state is JobState.succeeded
    first.database.close()
    second.database.close()


def test_expired_finalizer_lease_is_claimed_and_reconciled_once(
    tmp_path: Path,
) -> None:
    first = _service(tmp_path)
    queued = first.submit_account("round4-user", 1)
    first.job_service.claim(queued.id)
    staged = first._stage_artifact(
        queued.id,
        b'{"durable":"expired"}',
        artifact_kind="xhs_account_collection_raw",
        target_state=JobState.succeeded,
    )
    with first.database.session() as session:
        session.execute(
            text(
                "UPDATE xhs_artifact_promotion_journal "
                "SET recovery_lease_expires_at='2000-01-01 00:00:00' "
                "WHERE job_id=:job_id"
            ),
            {"job_id": queued.id},
        )
        session.commit()

    recovered_services: list[XhsCollectionService] = []
    errors: list[BaseException] = []

    def recover() -> None:
        try:
            recovered_services.append(_service(tmp_path))
        except BaseException as error:
            errors.append(error)

    workers = [Thread(target=recover), Thread(target=recover)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)

    assert errors == []
    assert len(recovered_services) == 2
    assert recovered_services[0].job_service.get(queued.id).state is JobState.needs_human
    with recovered_services[0].database.session() as session:
        journal = session.scalar(select(XhsArtifactPromotionJournalRecord))
        assert journal is not None
        assert (journal.state, journal.resolution) == ("completed", "rolled_back")
        assert journal.owner_token is None
        assert journal.recovery_lease_expires_at is None
    assert staged.store.inspect_stage(staged.stage_name).status == "missing"
    staged.store.close()
    first.database.close()
    for recovered in recovered_services:
        recovered.database.close()


def test_expired_recovery_lease_has_one_cross_process_cas_winner(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    queued = service.submit_account("round4-user", 1)
    service.job_service.claim(queued.id)
    staged = service._stage_artifact(
        queued.id,
        b'{"durable":"cross-process"}',
        artifact_kind="xhs_account_collection_raw",
        target_state=JobState.succeeded,
    )
    with service.database.session() as session:
        journal = session.scalar(select(XhsArtifactPromotionJournalRecord))
        assert journal is not None
        journal_id = journal.id
        journal.recovery_lease_expires_at = datetime(2000, 1, 1)
        session.commit()
    database_path = service.database.database_path
    runtime = service.runtime_dir
    staged.store.close()
    service.database.close()
    barrier = tmp_path / "claim-barrier"
    ready_paths = [tmp_path / f"ready-{index}" for index in range(2)]
    script = """
import sys
import time
from pathlib import Path
from uuid import uuid4
from sqlalchemy import text
from backend.app.db import Database

database = Database(Path(sys.argv[1]), runtime_dir=Path(sys.argv[2]))
Path(sys.argv[4]).write_text('ready', encoding='utf-8')
deadline = time.monotonic() + 10
while not Path(sys.argv[3]).exists():
    if time.monotonic() >= deadline:
        raise TimeoutError('claim barrier')
    time.sleep(0.01)
with database.engine.begin() as connection:
    changed = connection.execute(text(\"\"\"
        UPDATE xhs_artifact_promotion_journal
        SET owner_token=:owner,
            recovery_lease_expires_at=datetime('now','+5 minutes')
        WHERE id=:journal_id AND (
            owner_token IS NULL OR recovery_lease_expires_at IS NULL
            OR recovery_lease_expires_at <= CURRENT_TIMESTAMP
        )
    \"\"\"), {'owner': str(uuid4()), 'journal_id': sys.argv[5]})
print(changed.rowcount, flush=True)
database.close()
"""
    repo_root = Path(__file__).resolve().parents[3]
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(database_path),
                str(runtime),
                str(barrier),
                str(ready),
                journal_id,
            ],
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for ready in ready_paths
    ]
    deadline = time.monotonic() + 10
    while not all(path.exists() for path in ready_paths):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    barrier.write_text("claim", encoding="utf-8")
    results = [process.communicate(timeout=15) for process in processes]

    assert [process.returncode for process in processes] == [0, 0], results
    assert sorted(int(stdout.strip()) for stdout, _stderr in results) == [0, 1]
    inspected = Database(database_path, runtime_dir=runtime)
    with inspected.session() as session:
        journal = session.get(XhsArtifactPromotionJournalRecord, journal_id)
        assert journal is not None and journal.owner_token is not None
        assert journal.recovery_lease_expires_at is not None
    inspected.close()


def test_unjournaled_legacy_stage_is_retained_across_restart(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service.database.close()
    stage = (
        tmp_path
        / "runtime"
        / "evidence"
        / "xhs"
        / ".staging"
        / f"{uuid4()}-{uuid4().hex}.stage"
    )
    stage.parent.mkdir(parents=True, exist_ok=True)
    stage.write_bytes(b"unjournaled-legacy-stage")

    restarted = _service(tmp_path)

    assert stage.read_bytes() == b"unjournaled-legacy-stage"
    restarted.database.close()


def test_populated_v1_journal_is_mechanically_migrated_to_v2(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    completed = _completed_account(service)
    with service.database.engine.begin() as connection:
        v2_ddl = connection.scalar(text(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='xhs_artifact_promotion_journal'"
        ))
        assert isinstance(v2_ddl, str)
        for trigger in db_module._XHS_ARTIFACT_JOURNAL_TRIGGERS:
            connection.execute(text(f"DROP TRIGGER {trigger}"))
        for index in (
            "ix_xhs_artifact_journal_state",
            "ix_xhs_artifact_journal_artifact_id",
            "ix_xhs_artifact_journal_recovery_lease",
        ):
            connection.execute(text(f"DROP INDEX {index}"))
        connection.execute(text(
            "ALTER TABLE xhs_artifact_promotion_journal "
            "RENAME TO xhs_artifact_promotion_journal_v2_source"
        ))
        v1_ddl = (
            v2_ddl
            .replace("file_dev INTEGER,", "file_dev INTEGER NOT NULL,")
            .replace("file_ino INTEGER,", "file_ino INTEGER NOT NULL,")
            .replace("file_mtime_ns INTEGER,", "file_mtime_ns INTEGER NOT NULL,")
            .replace("\n\towner_token VARCHAR(36), ", "")
            .replace("\n\trecovery_lease_expires_at DATETIME, ", "")
            .replace(
                "size_bytes BETWEEN 0 AND 20971520 AND ((file_dev IS NULL "
                "AND file_ino IS NULL AND file_mtime_ns IS NULL AND "
                "(state = 'allocating' OR (state = 'completed' AND resolution IN "
                "('rolled_back','inconsistent')))) OR (file_dev IS NOT NULL "
                "AND file_ino IS NOT NULL AND file_mtime_ns IS NOT NULL "
                "AND file_dev >= 0 AND file_ino >= 0 AND file_mtime_ns >= 0 "
                "AND state != 'allocating'))",
                "size_bytes BETWEEN 0 AND 20971520 AND file_dev >= 0 "
                "AND file_ino >= 0 AND file_mtime_ns >= 0",
            )
            .replace(
                "state IN ('allocating','prepared','promoted','completed')",
                "state IN ('prepared','promoted','completed')",
            )
            .replace(
                "\n\tCONSTRAINT ck_xhs_artifact_journal_owner_lease CHECK "
                "(((owner_token IS NULL AND recovery_lease_expires_at IS NULL) "
                "OR (is_canonical_uuid(owner_token) = 1 AND "
                "datetime(recovery_lease_expires_at) IS NOT NULL))), ",
                "",
            )
        )
        assert "owner_token" not in v1_ddl and "allocating" not in v1_ddl
        connection.execute(text(v1_ddl))
        legacy_columns = (
            "id, job_id, artifact_kind, producer, stage_path, final_path, "
            "sha256, size_bytes, file_dev, file_ino, file_mtime_ns, "
            "target_state, state, resolution, artifact_id, created_at, "
            "updated_at, completed_at"
        )
        connection.execute(text(
            "INSERT INTO xhs_artifact_promotion_journal "
            f"({legacy_columns}) SELECT {legacy_columns} FROM "
            "xhs_artifact_promotion_journal_v2_source"
        ))
        connection.execute(text(
            "DROP TABLE xhs_artifact_promotion_journal_v2_source"
        ))
        connection.execute(text(
            "CREATE INDEX ix_xhs_artifact_journal_state ON "
            "xhs_artifact_promotion_journal (state)"
        ))
        connection.execute(text(
            "CREATE INDEX ix_xhs_artifact_journal_artifact_id ON "
            "xhs_artifact_promotion_journal (artifact_id)"
        ))
        for definition in db_module._XHS_ARTIFACT_JOURNAL_V1_TRIGGERS.values():
            connection.execute(text(definition))
        connection.execute(text(
            "DELETE FROM workbench_schema_migrations "
            "WHERE name='xhs_artifact_promotion_journal_v2'"
        ))
        connection.execute(text(
            "INSERT INTO workbench_schema_migrations(name, applied_at) "
            "VALUES ('xhs_artifact_promotion_journal_v1', CURRENT_TIMESTAMP)"
        ))
    path = service.database.database_path
    service.database.close()

    migrated = _service(tmp_path)

    assert migrated.get_profile("round4-user").collection_job_id == completed.id
    with migrated.database.session() as session:
        journal = session.scalar(select(XhsArtifactPromotionJournalRecord))
        assert journal is not None
        assert journal.owner_token is None
        assert journal.recovery_lease_expires_at is None
        assert (journal.state, journal.resolution) == ("completed", "committed")
    with migrated.database.engine.connect() as connection:
        assert connection.scalar(text(
            "SELECT COUNT(*) FROM workbench_schema_migrations "
            "WHERE name='xhs_artifact_promotion_journal_v2'"
        )) == 1
    assert migrated.database.database_path == path
    migrated.database.close()


def test_posix_backend_never_uses_overwriting_rename_or_pathname_unlink() -> None:
    source = python_inspect.getsource(staging_module._PosixArtifactStore)
    module_source = python_inspect.getsource(staging_module)

    assert "os.rename(" not in source
    assert "os.unlink(" not in source
    assert "_rename_posix_noreplace(" in source
    assert "os.unlink(" not in module_source


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-bound regression")
def test_windows_promotion_collision_never_overwrites_formal_evidence(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    job_id = str(uuid4())
    journal_id = str(uuid4())
    stage_name = f"{job_id}-{journal_id.replace('-', '')}.stage"
    final_name = f"{job_id}.json"
    with staging_module.TrustedXhsArtifactStore(runtime, max_bytes=4096) as store:
        identity = store.create_stage(stage_name, b"trusted-stage")
        formal = runtime / "evidence" / "xhs" / final_name
        formal.write_bytes(b"preexisting-formal")

        with pytest.raises(UnsafeXhsArtifactStore):
            store.promote(stage_name, final_name, identity)

        assert formal.read_bytes() == b"preexisting-formal"
        assert store.read_stage(stage_name, identity) == b"trusted-stage"
