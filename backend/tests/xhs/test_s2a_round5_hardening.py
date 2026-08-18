"""Regression tests for S2A fix round 5/5."""

from __future__ import annotations

import json
import hashlib
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier, Event, Thread
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from backend.app.adapters.contracts import (
    CollectionItem,
    CollectionRequest,
    CollectionResult,
)
from backend.app.db import Database, SchemaMigrationError, canonical_raw_evidence_digest
import backend.app.features.xhs.staging_cleanup as staging_module
from backend.app.features.xhs.models import (
    XhsAccountNoteRecord,
    XhsAccountProfileRecord,
    XhsArtifactPromotionJournalRecord,
)
from backend.app.features.xhs.service import (
    ArtifactCommitUnknown,
    CollectionFactNotFound,
    XhsCollectionService,
)
from backend.app.models.jobs import JobState
from backend.app.services.jobs import JobService


_FACT_CONTENT_BINDING_MIGRATION = "xhs_account_fact_content_binding_v4"
_FACT_IMMUTABILITY_TRIGGERS = {
    "ck_xhs_profile_immutable_update": """
        CREATE TRIGGER ck_xhs_profile_immutable_update
        BEFORE UPDATE ON xhs_account_profiles
        FOR EACH ROW
        WHEN EXISTS (
            SELECT 1 FROM job_artifacts AS artifact
            WHERE artifact.id=OLD.collection_artifact_id
            AND artifact.job_id=OLD.collection_job_id
            AND json_valid(artifact.metadata_json) IS 1
            AND json_type(artifact.metadata_json) IS 'object'
            AND json_type(artifact.metadata_json, '$.artifact_id') IS 'integer'
            AND json_extract(artifact.metadata_json, '$.artifact_id') IS artifact.id
            AND json_type(artifact.metadata_json, '$.job_id') IS 'text'
            AND json_extract(artifact.metadata_json, '$.job_id') IS artifact.job_id
            AND json_type(artifact.metadata_json, '$.sha256') IS 'text'
            AND json_type(artifact.metadata_json, '$.size_bytes') IS 'integer'
        )
        BEGIN
            SELECT RAISE(ABORT, 'xhs account profile facts are immutable');
        END
    """,
    "ck_xhs_note_immutable_update": """
        CREATE TRIGGER ck_xhs_note_immutable_update
        BEFORE UPDATE ON xhs_account_notes
        FOR EACH ROW
        WHEN EXISTS (
            SELECT 1 FROM job_artifacts AS artifact
            WHERE artifact.id=OLD.collection_artifact_id
            AND artifact.job_id=OLD.collection_job_id
            AND json_valid(artifact.metadata_json) IS 1
            AND json_type(artifact.metadata_json) IS 'object'
            AND json_type(artifact.metadata_json, '$.artifact_id') IS 'integer'
            AND json_extract(artifact.metadata_json, '$.artifact_id') IS artifact.id
            AND json_type(artifact.metadata_json, '$.job_id') IS 'text'
            AND json_extract(artifact.metadata_json, '$.job_id') IS artifact.job_id
            AND json_type(artifact.metadata_json, '$.sha256') IS 'text'
            AND json_type(artifact.metadata_json, '$.size_bytes') IS 'integer'
        )
        BEGIN
            SELECT RAISE(ABORT, 'xhs account note facts are immutable');
        END
    """,
}


class _VersionedAccountAdapter:
    def __init__(self) -> None:
        self.version = 0

    def fetch_account(self, request: CollectionRequest) -> CollectionResult:
        self.version += 1
        version = self.version
        user_id = str(request.parameters["user_id"])
        notes = [
            CollectionItem(
                id=f"note:note-{version}-{index}",
                kind="note",
                source_url=(
                    f"https://www.xiaohongshu.com/explore/note-{version}-{index}"
                ),
                raw_evidence={
                    "row": {
                        "note_id": f"note-{version}-{index}",
                        "user_id": user_id,
                        "version": version,
                    }
                },
                data={
                    "note_id": f"note-{version}-{index}",
                    "user_id": user_id,
                    "title": f"Title {version}-{index}",
                    "summary": f"Summary {version}-{index}",
                    "published_at": f"2026-08-{18 + version:02d}T10:00:00Z",
                    "liked_count": version * 10 + index,
                    "collect_count": version * 20 + index,
                    "comment_count": version * 30 + index,
                },
            )
            for index in range(2)
        ]
        return CollectionResult(
            status="succeeded",
            items=[
                CollectionItem(
                    id=f"profile:{user_id}",
                    kind="profile",
                    source_url=(
                        f"https://www.xiaohongshu.com/user/profile/{user_id}"
                    ),
                    raw_evidence={
                        "profile": {
                            "user_id": user_id,
                            "nickname": f"Name {version}",
                            "version": version,
                        }
                    },
                    data={
                        "user_id": user_id,
                        "nickname": f"Name {version}",
                        "bio": f"Bio {version}",
                        "followers_count": version * 100,
                        "following_count": version * 10,
                        "liked_count": version * 1000,
                    },
                ),
                *notes,
            ],
            expected_count_known=True,
            expected_count=3,
            succeeded_count=3,
            observed_count=3,
            overflow_count=0,
            complete=True,
        )


def _service(tmp_path: Path) -> XhsCollectionService:
    runtime = tmp_path / "runtime"
    database = Database(runtime / "workbench.sqlite3", runtime_dir=runtime)
    return XhsCollectionService(
        database=database,
        job_service=JobService(database, runtime_dir=runtime),
        adapter=_VersionedAccountAdapter(),
        runtime_dir=runtime,
        submitter=lambda *_args: None,
    )


def _collect(service: XhsCollectionService, user_id: str = "round5-user"):
    queued = service.submit_account(user_id, 2)
    completed = service.execute(queued.id)
    assert completed is not None and completed.state is JobState.succeeded
    return completed


def _drop_fact_freeze_triggers(service: XhsCollectionService) -> None:
    with service.database.engine.begin() as connection:
        for name in _FACT_IMMUTABILITY_TRIGGERS:
            connection.execute(text(f"DROP TRIGGER IF EXISTS {name}"))


def _restore_fact_freeze_triggers(service: XhsCollectionService) -> None:
    with service.database.engine.begin() as connection:
        for definition in _FACT_IMMUTABILITY_TRIGGERS.values():
            connection.execute(text(definition))


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("source_url", "https://www.xiaohongshu.com/user/profile/forged"),
        ("nickname", "forged nickname"),
        ("bio", "forged bio"),
        ("public_stats_json", json.dumps({"followers_count": 999999})),
        (
            "raw_evidence",
            json.dumps({"profile": {"user_id": "round5-user", "forged": True}}),
        ),
        ("raw_digest", "0" * 64),
        ("collected_at", "2099-01-01 00:00:00"),
    ],
)
def test_profile_read_compares_every_persisted_content_field_to_artifact(
    tmp_path: Path,
    column: str,
    value: object,
) -> None:
    service = _service(tmp_path)
    _collect(service)
    _drop_fact_freeze_triggers(service)
    with service.database.engine.begin() as connection:
        connection.execute(text("PRAGMA ignore_check_constraints=ON"))
        if column == "raw_evidence":
            replacement = json.loads(str(value))
            connection.execute(
                text(
                    "UPDATE xhs_account_profiles SET raw_evidence=:value, "
                    "raw_digest=:digest WHERE user_id='round5-user'"
                ),
                {
                    "value": value,
                    "digest": canonical_raw_evidence_digest(replacement),
                },
            )
        else:
            connection.execute(
                text(
                    f"UPDATE xhs_account_profiles SET {column}=:value "
                    "WHERE user_id='round5-user'"
                ),
                {"value": value},
            )
        connection.execute(text("PRAGMA ignore_check_constraints=OFF"))

    with pytest.raises(CollectionFactNotFound):
        service.get_profile("round5-user")
    with pytest.raises(CollectionFactNotFound):
        service.list_account_notes("round5-user")
    service.database.close()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("note_id", "forged-note-id"),
        ("source_url", "https://www.xiaohongshu.com/explore/forged"),
        ("title", "forged title"),
        ("summary", "forged summary"),
        ("published_at", "2099-01-01T00:00:00Z"),
        ("public_interactions_json", json.dumps({"liked_count": 999999})),
        (
            "raw_evidence",
            json.dumps(
                {
                    "row": {
                        "note_id": "note-1-0",
                        "user_id": "round5-user",
                        "forged": True,
                    }
                }
            ),
        ),
        ("raw_digest", "0" * 64),
        ("collected_at", "2099-01-01 00:00:00"),
    ],
)
def test_note_reads_compare_every_persisted_content_field_to_artifact(
    tmp_path: Path,
    column: str,
    value: object,
) -> None:
    service = _service(tmp_path)
    _collect(service)
    _drop_fact_freeze_triggers(service)
    with service.database.engine.begin() as connection:
        connection.execute(text("PRAGMA ignore_check_constraints=ON"))
        if column == "raw_evidence":
            replacement = json.loads(str(value))
            connection.execute(
                text(
                    "UPDATE xhs_account_notes SET raw_evidence=:value, "
                    "raw_digest=:digest WHERE note_id='note-1-0'"
                ),
                {
                    "value": value,
                    "digest": canonical_raw_evidence_digest(replacement),
                },
            )
        else:
            connection.execute(
                text(
                    f"UPDATE xhs_account_notes SET {column}=:value "
                    "WHERE note_id='note-1-0'"
                ),
                {"value": value},
            )
        connection.execute(text("PRAGMA ignore_check_constraints=OFF"))

    with pytest.raises(CollectionFactNotFound):
        service.get_profile("round5-user")
    with pytest.raises(CollectionFactNotFound):
        service.list_account_notes("round5-user")
    service.database.close()


def test_account_reads_bind_exact_note_owner_order_and_count(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _collect(service)
    _drop_fact_freeze_triggers(service)
    with service.database.engine.begin() as connection:
        connection.execute(
            text("DELETE FROM xhs_account_notes WHERE note_id='note-1-0'")
        )

    with pytest.raises(CollectionFactNotFound):
        service.get_profile("round5-user")
    with pytest.raises(CollectionFactNotFound):
        service.list_account_notes("round5-user")
    service.database.close()


@pytest.mark.parametrize(
    ("table", "assignment", "parameters"),
    [
        (
            "xhs_account_profiles",
            "nickname=:value",
            {"value": "forged", "key": "round5-user"},
        ),
        (
            "xhs_account_profiles",
            "raw_evidence=:value, raw_digest=:digest",
            {
                "value": json.dumps({"profile": {"user_id": "round5-user"}}),
                "digest": canonical_raw_evidence_digest(
                    {"profile": {"user_id": "round5-user"}}
                ),
                "key": "round5-user",
            },
        ),
        (
            "xhs_account_notes",
            "title=:value",
            {"value": "forged", "key": "note-1-0"},
        ),
        (
            "xhs_account_notes",
            "public_interactions_json=:value",
            {
                "value": json.dumps({"liked_count": 999999}),
                "key": "note-1-0",
            },
        ),
    ],
)
def test_physical_update_triggers_freeze_profile_and_note_content(
    tmp_path: Path,
    table: str,
    assignment: str,
    parameters: dict[str, object],
) -> None:
    service = _service(tmp_path)
    _collect(service)
    key_column = "user_id" if table == "xhs_account_profiles" else "note_id"

    with pytest.raises(IntegrityError):
        with service.database.engine.begin() as connection:
            connection.execute(
                text(f"UPDATE {table} SET {assignment} WHERE {key_column}=:key"),
                parameters,
            )
    service.database.close()


@pytest.mark.parametrize(
    ("table", "key_column", "key", "column"),
    [
        *[
            ("xhs_account_profiles", "user_id", "round5-user", column)
            for column in (
                "user_id",
                "source_url",
                "nickname",
                "bio",
                "public_stats_json",
                "raw_evidence",
                "raw_digest",
                "collection_job_id",
                "collection_artifact_id",
                "collected_at",
            )
        ],
        *[
            ("xhs_account_notes", "note_id", "note-1-0", column)
            for column in (
                "note_id",
                "user_id",
                "source_url",
                "title",
                "summary",
                "published_at",
                "public_interactions_json",
                "raw_evidence",
                "raw_digest",
                "collection_job_id",
                "collection_artifact_id",
                "collected_at",
            )
        ],
    ],
)
def test_physical_freeze_rejects_updates_to_every_normalized_fact_field(
    tmp_path: Path,
    table: str,
    key_column: str,
    key: str,
    column: str,
) -> None:
    """A narrowed trigger must not leave provenance or timestamps mutable."""

    service = _service(tmp_path)
    _collect(service)

    with pytest.raises(IntegrityError):
        with service.database.engine.begin() as connection:
            connection.execute(
                text(
                    f"UPDATE {table} SET {column}={column} "
                    f"WHERE {key_column}=:key"
                ),
                {"key": key},
            )
    service.database.close()


def test_content_binding_marker_requires_exact_freeze_triggers(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    _collect(service)
    path = service.database.database_path
    runtime = service.runtime_dir
    with service.database.engine.connect() as connection:
        names = set(
            connection.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "AND name IN ('ck_xhs_profile_immutable_update', "
                    "'ck_xhs_note_immutable_update')"
                )
            ).scalars()
        )
        assert names == set(_FACT_IMMUTABILITY_TRIGGERS)
        assert connection.scalar(
            text(
                "SELECT COUNT(*) FROM workbench_schema_migrations "
                "WHERE name=:name"
            ),
            {"name": _FACT_CONTENT_BINDING_MIGRATION},
        ) == 1
    service.database.close()
    reopened = Database(path, runtime_dir=runtime)
    with reopened.engine.begin() as connection:
        connection.execute(text("DROP TRIGGER ck_xhs_note_immutable_update"))
    reopened.close()

    with pytest.raises(SchemaMigrationError, match="content binding"):
        Database(path, runtime_dir=runtime)


def test_restart_scan_rejects_historical_fact_content_tamper(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    _collect(service)
    path = service.database.database_path
    runtime = service.runtime_dir
    _drop_fact_freeze_triggers(service)
    with service.database.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE xhs_account_notes SET title='historical-forgery' "
                "WHERE note_id='note-1-0'"
            )
        )
    _restore_fact_freeze_triggers(service)
    service.database.close()

    with pytest.raises(SchemaMigrationError, match="content binding"):
        Database(path, runtime_dir=runtime)


@pytest.mark.parametrize(
    "corruption",
    ["raw-evidence", "note-order", "note-owner", "note-count"],
)
def test_restart_scan_rejects_raw_owner_order_and_count_tamper(
    tmp_path: Path,
    corruption: str,
) -> None:
    service = _service(tmp_path)
    _collect(service)
    path = service.database.database_path
    runtime = service.runtime_dir
    _drop_fact_freeze_triggers(service)
    with service.database.engine.begin() as connection:
        if corruption == "raw-evidence":
            replacement = {
                "row": {
                    "note_id": "note-1-0",
                    "user_id": "round5-user",
                    "forged": True,
                }
            }
            connection.execute(
                text(
                    "UPDATE xhs_account_notes SET raw_evidence=:raw, "
                    "raw_digest=:digest WHERE note_id='note-1-0'"
                ),
                {
                    "raw": json.dumps(replacement),
                    "digest": canonical_raw_evidence_digest(replacement),
                },
            )
        elif corruption == "note-order":
            ids = connection.execute(
                text(
                    "SELECT id FROM xhs_account_notes "
                    "WHERE user_id='round5-user' ORDER BY id"
                )
            ).scalars().all()
            assert len(ids) == 2
            temporary = max(ids) + 100
            connection.execute(
                text("UPDATE xhs_account_notes SET id=:new WHERE id=:old"),
                {"new": temporary, "old": ids[0]},
            )
            connection.execute(
                text("UPDATE xhs_account_notes SET id=:new WHERE id=:old"),
                {"new": ids[0], "old": ids[1]},
            )
            connection.execute(
                text("UPDATE xhs_account_notes SET id=:new WHERE id=:old"),
                {"new": ids[1], "old": temporary},
            )
        elif corruption == "note-owner":
            connection.execute(text(
                "INSERT INTO xhs_account_profiles ("
                "user_id, source_url, nickname, bio, public_stats_json, "
                "raw_evidence, raw_digest, collection_job_id, "
                "collection_artifact_id, collected_at) "
                "SELECT 'round5-other', "
                "'https://www.xiaohongshu.com/user/profile/round5-other', "
                "nickname, bio, public_stats_json, raw_evidence, raw_digest, "
                "collection_job_id, collection_artifact_id, collected_at "
                "FROM xhs_account_profiles WHERE user_id='round5-user'"
            ))
            connection.execute(text(
                "UPDATE xhs_account_notes SET user_id='round5-other' "
                "WHERE note_id='note-1-0'"
            ))
        else:
            connection.execute(
                text("DELETE FROM xhs_account_notes WHERE note_id='note-1-0'")
            )
    _restore_fact_freeze_triggers(service)
    service.database.close()

    with pytest.raises(SchemaMigrationError, match="content binding"):
        Database(path, runtime_dir=runtime)


def test_normal_recollection_replaces_latest_facts_without_mutating_old_rows(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    first = _collect(service)
    first_note_ids = [row.note_id for row in service.list_account_notes("round5-user")]
    second = _collect(service)

    profile = service.get_profile("round5-user")
    notes = service.list_account_notes("round5-user")

    assert first.id != second.id
    assert profile.nickname == "Name 2"
    assert profile.collection_job_id == second.id
    assert [row.note_id for row in notes] == ["note-2-0", "note-2-1"]
    assert first_note_ids == ["note-1-0", "note-1-1"]
    service.database.close()


def _install_initial_commit_ack_fault(
    service: XhsCollectionService,
    monkeypatch: pytest.MonkeyPatch,
    *,
    landed: bool,
    commit_error: BaseException | None = None,
) -> list[bool]:
    original_session = service.database.session
    fired = [False]

    class AllocationCommitProxy:
        def __init__(self, session: object) -> None:
            self._session = session
            self._allocation_insert = False

        def __getattr__(self, name: str) -> object:
            return getattr(self._session, name)

        def execute(self, statement, *args, **kwargs):
            if "INSERT INTO xhs_artifact_promotion_journal" in str(statement):
                self._allocation_insert = True
            return self._session.execute(statement, *args, **kwargs)

        def commit(self) -> None:
            if self._allocation_insert and not fired[0]:
                fired[0] = True
                if landed:
                    self._session.commit()
                else:
                    self._session.rollback()
                raise commit_error or RuntimeError(
                    "opaque initial allocation acknowledgement"
                )
            self._session.commit()

    @contextmanager
    def faulting_session():
        with original_session() as session:
            yield AllocationCommitProxy(session)

    monkeypatch.setattr(service.database, "session", faulting_session)
    return fired


def test_initial_allocation_commit_ack_landed_continues_same_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    queued = service.submit_account("allocation-landed", 2)
    fired = _install_initial_commit_ack_fault(service, monkeypatch, landed=True)

    completed = service.execute(queued.id)

    assert fired == [True]
    assert completed is not None and completed.state is JobState.succeeded
    with service.database.session() as session:
        journals = session.scalars(
            select(XhsArtifactPromotionJournalRecord).where(
                XhsArtifactPromotionJournalRecord.job_id == queued.id
            )
        ).all()
        assert len(journals) == 1
        assert (journals[0].state, journals[0].resolution) == (
            "completed",
            "committed",
        )
    service.database.close()


def test_initial_allocation_commit_ack_not_landed_fails_without_second_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    queued = service.submit_account("allocation-not-landed", 2)
    fired = _install_initial_commit_ack_fault(service, monkeypatch, landed=False)

    assert service.execute(queued.id) is None

    assert fired == [True]
    recovered = service.job_service.get(queued.id)
    assert recovered.state is JobState.needs_human
    assert recovered.error_category == "artifact_journal_allocation_rolled_back"
    with service.database.session() as session:
        assert session.scalar(
            select(XhsArtifactPromotionJournalRecord).where(
                XhsArtifactPromotionJournalRecord.job_id == queued.id
            )
        ) is None
    assert list((service.runtime_dir / "evidence" / "xhs" / ".staging").glob("*.stage")) == []
    service.database.close()


def test_initial_allocation_sqlalchemy_error_is_absorbed_at_service_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    queued = service.submit_account("allocation-sqla-error", 2)
    fired = _install_initial_commit_ack_fault(
        service,
        monkeypatch,
        landed=False,
        commit_error=OperationalError(
            "COMMIT",
            {},
            RuntimeError("opaque database acknowledgement"),
        ),
    )

    assert service.execute(queued.id) is None

    assert fired == [True]
    recovered = service.job_service.get(queued.id)
    assert recovered.state is JobState.needs_human
    assert recovered.error_category == "artifact_journal_allocation_rolled_back"
    with service.database.session() as session:
        assert session.scalar(
            select(XhsArtifactPromotionJournalRecord).where(
                XhsArtifactPromotionJournalRecord.job_id == queued.id
            )
        ) is None
    service.database.close()


def test_initial_allocation_commit_ack_read_fault_converges_without_leaking_db_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    queued = service.submit_account("allocation-read-fault", 2)
    _install_initial_commit_ack_fault(service, monkeypatch, landed=True)
    monkeypatch.setattr(
        service,
        "_probe_initial_allocation_outcome",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("opaque fresh read fault")
        ),
    )

    assert service.execute(queued.id) is None

    recovered = service.job_service.get(queued.id)
    assert recovered.state is JobState.needs_human
    assert recovered.error_category == "artifact_journal_allocation_unknown"
    with service.database.session() as session:
        journal = session.scalar(
            select(XhsArtifactPromotionJournalRecord).where(
                XhsArtifactPromotionJournalRecord.job_id == queued.id
            )
        )
        assert journal is not None
        assert (journal.state, journal.resolution) == ("completed", "rolled_back")
        assert journal.owner_token is None
        assert journal.recovery_lease_expires_at is None
    service.database.close()


def test_duplicate_allocation_preserves_the_single_existing_journal(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    queued = service.submit_account("allocation-duplicate", 2)
    service.job_service.claim(queued.id)
    staged = service._stage_artifact(
        queued.id,
        b'{"first":"allocation"}',
        artifact_kind="xhs_account_collection_raw",
        target_state=JobState.succeeded,
    )

    with pytest.raises(ArtifactCommitUnknown, match="allocation_unknown"):
        service._stage_artifact(
            queued.id,
            b'{"second":"allocation"}',
            artifact_kind="xhs_account_collection_raw",
            target_state=JobState.succeeded,
        )

    with service.database.session() as session:
        journals = session.scalars(
            select(XhsArtifactPromotionJournalRecord).where(
                XhsArtifactPromotionJournalRecord.job_id == queued.id
            )
        ).all()
        assert [journal.id for journal in journals] == [staged.journal_id]
        assert journals[0].state == "prepared"
    service._resolve_rolled_back(staged)
    staged.store.close()
    service.database.close()


def test_concurrent_initial_allocators_have_one_durable_winner(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    queued = service.submit_account("allocation-concurrent", 2)
    service.job_service.claim(queued.id)
    barrier = Barrier(2)
    staged_results: list[object] = []
    errors: list[BaseException] = []

    def allocate(payload: bytes) -> None:
        barrier.wait()
        try:
            staged_results.append(service._stage_artifact(
                queued.id,
                payload,
                artifact_kind="xhs_account_collection_raw",
                target_state=JobState.succeeded,
            ))
        except BaseException as error:
            errors.append(error)

    workers = [
        Thread(target=allocate, args=(b'{"worker":1}',)),
        Thread(target=allocate, args=(b'{"worker":2}',)),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)

    assert all(not worker.is_alive() for worker in workers)
    assert len(staged_results) == 1
    assert len(errors) == 1 and isinstance(errors[0], ArtifactCommitUnknown)
    with service.database.session() as session:
        assert len(session.scalars(
            select(XhsArtifactPromotionJournalRecord).where(
                XhsArtifactPromotionJournalRecord.job_id == queued.id
            )
        ).all()) == 1
    staged = staged_results[0]
    service._resolve_rolled_back(staged)
    staged.store.close()
    service.database.close()


def _insert_allocating_without_stage(
    service: XhsCollectionService,
    job_id: str,
    *,
    expired: bool,
) -> str:
    journal_id = str(uuid4())
    payload = b'{"never":"staged"}'
    lease = "2000-01-01 00:00:00" if expired else "2099-01-01 00:00:00"
    with service.database.engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO xhs_artifact_promotion_journal (
                id, job_id, artifact_kind, producer, stage_path, final_path,
                sha256, size_bytes, file_dev, file_ino, file_mtime_ns,
                target_state, state, resolution, artifact_id, created_at,
                updated_at, completed_at, owner_token,
                recovery_lease_expires_at
            ) VALUES (
                :id, :job_id, 'xhs_account_collection_raw',
                'xhs_cli_read_worker_v1', :stage_path, :final_path,
                :sha256, :size_bytes, NULL, NULL, NULL, 'succeeded',
                'allocating', NULL, NULL, CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP, NULL, :owner, :lease
            )
        """), {
            "id": journal_id,
            "job_id": job_id,
            "stage_path": (
                f"evidence/xhs/.staging/{job_id}-{journal_id.replace('-', '')}.stage"
            ),
            "final_path": f"evidence/xhs/{job_id}.json",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
            "owner": str(uuid4()),
            "lease": lease,
        })
    return journal_id


@pytest.mark.parametrize("expired", [False, True], ids=["active-lease", "expired-lease"])
def test_restart_converges_allocating_without_stage(
    tmp_path: Path,
    expired: bool,
) -> None:
    first = _service(tmp_path)
    queued = first.submit_account(f"allocation-restart-{expired}", 2)
    first.job_service.claim(queued.id)
    journal_id = _insert_allocating_without_stage(
        first,
        queued.id,
        expired=expired,
    )
    first.database.close()

    restarted = _service(tmp_path)

    recovered = restarted.job_service.get(queued.id)
    assert recovered.state is JobState.needs_human
    assert recovered.error_category == "artifact_journal_allocation_interrupted"
    with restarted.database.session() as session:
        journal = session.get(XhsArtifactPromotionJournalRecord, journal_id)
        assert journal is not None
        assert (journal.state, journal.resolution) == ("completed", "rolled_back")
        assert journal.owner_token is None
        assert journal.recovery_lease_expires_at is None
    restarted.database.close()


def test_recovery_race_before_stage_creation_does_not_orphan_a_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live allocator that loses recovery ownership must clean its late stage."""

    first = _service(tmp_path)
    queued = first.submit_account("allocation-recovery-race", 2)
    first.job_service.claim(queued.id)
    entered = Event()
    release = Event()
    real_create_stage = staging_module.TrustedXhsArtifactStore.create_stage
    results: list[object] = []
    errors: list[BaseException] = []

    def paused_create_stage(store, name: str, payload: bytes):
        entered.set()
        assert release.wait(10)
        return real_create_stage(store, name, payload)

    monkeypatch.setattr(
        staging_module.TrustedXhsArtifactStore,
        "create_stage",
        paused_create_stage,
    )

    def allocate() -> None:
        try:
            results.append(first._stage_artifact(
                queued.id,
                b'{"late":"stage"}',
                artifact_kind="xhs_account_collection_raw",
                target_state=JobState.succeeded,
            ))
        except BaseException as error:
            errors.append(error)

    worker = Thread(target=allocate)
    worker.start()
    assert entered.wait(5)

    restarted = _service(tmp_path)
    with restarted.database.session() as session:
        journal = session.scalar(
            select(XhsArtifactPromotionJournalRecord).where(
                XhsArtifactPromotionJournalRecord.job_id == queued.id
            )
        )
        assert journal is not None
        assert (journal.state, journal.resolution) == (
            "completed",
            "rolled_back",
        )

    release.set()
    worker.join(10)

    assert not worker.is_alive()
    assert results == []
    assert len(errors) == 1 and isinstance(errors[0], ArtifactCommitUnknown)
    assert list(
        (first.runtime_dir / "evidence" / "xhs" / ".staging").glob("*.stage")
    ) == []
    first.database.close()
    restarted.database.close()
