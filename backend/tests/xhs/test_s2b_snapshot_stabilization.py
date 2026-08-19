"""S2B acceptance coverage for append-only account evidence snapshots."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from threading import Thread
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

import backend.app.db as db_module
from backend.app.adapters.contracts import (
    CollectionItem,
    CollectionRequest,
    CollectionResult,
    ModelResult,
    StructuredModelRequest,
)
from backend.app.db import Database, SchemaMigrationError, canonical_raw_evidence_digest
from backend.app.features.analysis.schemas import AnalysisCreate
from backend.app.features.analysis.service import AnalysisService
from backend.app.features.xhs.constants import (
    ACCOUNT_COLLECTION_ARTIFACT_KIND,
    ACCOUNT_COLLECTION_JOB_TYPE,
)
from backend.app.features.xhs.models import (
    XhsAccountNoteRecord,
    XhsAccountProfileRecord,
    XhsAccountProfileSnapshotRecord,
    XhsAccountSnapshotNoteRecord,
)
from backend.app.features.xhs.schemas import (
    AccountEvidenceBinding,
    persist_exact_account_result,
)
from backend.app.features.xhs.service import XhsCollectionService
from backend.app.models.jobs import JobArtifactRecord, JobState
from backend.app.services.jobs import JobService


class _ExactAccountAdapter:
    def fetch_account(self, request: CollectionRequest) -> CollectionResult:
        user_id = str(request.parameters["user_id"])
        return CollectionResult(
            status="succeeded",
            items=[
                CollectionItem(
                    id=f"profile:{user_id}",
                    kind="profile",
                    source_url=(
                        f"https://www.xiaohongshu.com/user/profile/{user_id}"
                    ),
                    raw_evidence={"profile": {"user_id": user_id}},
                    data={
                        "user_id": user_id,
                        "nickname": "Stable account",
                        "followers_count": 7,
                    },
                ),
                CollectionItem(
                    id="note:stable-note",
                    kind="note",
                    source_url=(
                        "https://www.xiaohongshu.com/explore/stable-note"
                    ),
                    raw_evidence={
                        "row": {
                            "note_id": "stable-note",
                            "user_id": user_id,
                        }
                    },
                    data={
                        "note_id": "stable-note",
                        "user_id": user_id,
                        "title": "Stable note",
                        "liked_count": 3,
                    },
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
        raise AssertionError("search is outside this acceptance fixture")


class _Model:
    provider = "s2b-test"
    model = "s2b-test-model"
    configured = True

    def generate_structured(
        self,
        request: StructuredModelRequest,
        schema: object,
    ) -> ModelResult:
        return ModelResult(
            model=self.model,
            output={
                "claims": [{
                    "claim": "The immutable note supports this claim.",
                    "evidence_ids": list(request.evidence_ids),
                }],
                "product_clusters": [],
                "opportunities": [],
            },
            raw_evidence={"provider_request_id": "s2b"},
            usage={},
            duration_ms=1,
        )


def _service(tmp_path: Path) -> XhsCollectionService:
    runtime_dir = tmp_path / "runtime"
    database = Database(
        runtime_dir / "workbench.sqlite3",
        runtime_dir=runtime_dir,
    )
    return XhsCollectionService(
        database=database,
        job_service=JobService(database, runtime_dir=runtime_dir),
        adapter=_ExactAccountAdapter(),
        runtime_dir=runtime_dir,
        submitter=lambda *_args: None,
    )


def _collect(service: XhsCollectionService, user_id: str = "s2b-user"):
    queued = service.submit_account(user_id, 1)
    completed = service.execute(queued.id)
    assert completed is not None and completed.state is JobState.succeeded
    return completed


def _counts(database: Database) -> tuple[int, int, int, int]:
    with database.session() as session:
        return (
            session.scalar(select(func.count()).select_from(XhsAccountProfileRecord)),
            session.scalar(select(func.count()).select_from(XhsAccountProfileSnapshotRecord)),
            session.scalar(select(func.count()).select_from(XhsAccountNoteRecord)),
            session.scalar(select(func.count()).select_from(XhsAccountSnapshotNoteRecord)),
        )


def test_exact_same_job_retry_reuses_the_immutable_snapshot_and_note_id(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    completed = _collect(service)
    with service.database.session() as session:
        artifact = session.scalar(select(JobArtifactRecord).where(
            JobArtifactRecord.job_id == completed.id
        ))
        assert artifact is not None
        artifact_id = artifact.id
        collected_at = artifact.created_at
        original_note_id = session.scalar(select(XhsAccountNoteRecord.id))
    _job_input, _metadata, _payload, result = service._read_trusted_collection_result(
        job_id=completed.id,
        artifact_id=artifact_id,
        expected_job_type=ACCOUNT_COLLECTION_JOB_TYPE,
        expected_artifact_kind=ACCOUNT_COLLECTION_ARTIFACT_KIND,
        binding_name="user_id",
        binding_value="s2b-user",
    )
    before = _counts(service.database)

    for _attempt in range(2):
        with service.database.session() as session:
            persisted = persist_exact_account_result(
                session,
                result=result,
                binding=AccountEvidenceBinding(
                    collection_job_id=completed.id,
                    collection_artifact_id=artifact_id,
                    collected_at=collected_at,
                ),
            )
            session.commit()
            assert [note.id for note in persisted.notes] == [original_note_id]

    assert _counts(service.database) == before == (1, 1, 1, 1)
    service.database.close()


def test_concurrent_duplicate_exact_collections_append_distinct_snapshots(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    jobs = [
        service.submit_account("s2b-user", 1),
        service.submit_account("s2b-user", 1),
    ]
    completed: list[Any] = []
    failures: list[BaseException] = []

    def execute(job_id: str) -> None:
        try:
            completed.append(service.execute(job_id))
        except BaseException as error:  # pragma: no cover - asserted below
            failures.append(error)

    threads = [Thread(target=execute, args=(job.id,)) for job in jobs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert failures == []
    assert len(completed) == 2
    assert all(row is not None and row.state is JobState.succeeded for row in completed)
    assert _counts(service.database) == (1, 2, 2, 2)
    with service.database.session() as session:
        notes = session.scalars(select(XhsAccountNoteRecord).order_by(
            XhsAccountNoteRecord.id
        )).all()
        latest = session.scalar(select(XhsAccountProfileSnapshotRecord).order_by(
            XhsAccountProfileSnapshotRecord.id.desc()
        ))
        assert latest is not None
    assert [note.note_id for note in notes] == ["stable-note", "stable-note"]
    assert notes[0].id != notes[1].id
    assert notes[0].collection_job_id != notes[1].collection_job_id
    assert service.get_profile("s2b-user").collection_job_id == latest.collection_job_id
    assert [note.collection_job_id for note in service.list_account_notes("s2b-user")] == [
        latest.collection_job_id
    ]
    service.database.close()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE xhs_account_profiles SET nickname=nickname WHERE user_id='s2b-user'",
        "DELETE FROM xhs_account_profiles WHERE user_id='s2b-user'",
        "UPDATE xhs_account_profile_snapshots SET nickname=nickname",
        "DELETE FROM xhs_account_profile_snapshots",
        "UPDATE xhs_account_notes SET title=title",
        "DELETE FROM xhs_account_notes",
        "UPDATE xhs_account_snapshot_notes SET position=position",
        "DELETE FROM xhs_account_snapshot_notes",
    ],
)
def test_all_snapshot_fact_layers_are_physically_append_only(
    tmp_path: Path,
    statement: str,
) -> None:
    service = _service(tmp_path)
    _collect(service)

    with pytest.raises(IntegrityError):
        with service.database.engine.begin() as connection:
            connection.execute(text(statement))
    service.database.close()


def test_v5_marker_present_is_validation_only(tmp_path: Path) -> None:
    path = tmp_path / "marker.sqlite3"
    database = Database(path, runtime_dir=tmp_path)
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER ck_xhs_snapshot_note_immutable_delete")

    with pytest.raises(SchemaMigrationError, match="snapshot evidence"):
        Database(path, runtime_dir=tmp_path)

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' "
            "AND name='ck_xhs_snapshot_note_immutable_delete'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT COUNT(*) FROM workbench_schema_migrations WHERE name=?",
            (db_module.XHS_ACCOUNT_SNAPSHOT_EVIDENCE_MIGRATION,),
        ).fetchone()[0] == 1


def test_v5_marker_absent_recovers_an_empty_half_migration(tmp_path: Path) -> None:
    path = tmp_path / "empty-half.sqlite3"
    database = Database(path, runtime_dir=tmp_path)
    database.close()
    with sqlite3.connect(path) as connection:
        for name in db_module._XHS_ACCOUNT_SNAPSHOT_BINDING_TRIGGERS:
            connection.execute(f'DROP TRIGGER IF EXISTS "{name}"')
        for name in db_module._XHS_ACCOUNT_SNAPSHOT_IMMUTABILITY_TRIGGERS:
            connection.execute(f'DROP TRIGGER IF EXISTS "{name}"')
        connection.execute("DROP TABLE xhs_account_snapshot_notes")
        connection.execute(
            "DELETE FROM workbench_schema_migrations WHERE name=?",
            (db_module.XHS_ACCOUNT_SNAPSHOT_EVIDENCE_MIGRATION,),
        )

    recovered = Database(path, runtime_dir=tmp_path)
    try:
        with recovered.engine.connect() as connection:
            assert db_module._xhs_account_snapshot_schema_valid(connection)
            assert connection.scalar(text(
                "SELECT COUNT(*) FROM workbench_schema_migrations WHERE name=:name"
            ), {"name": db_module.XHS_ACCOUNT_SNAPSHOT_EVIDENCE_MIGRATION}) == 1
    finally:
        recovered.close()


def test_v5_marker_absent_populated_half_migration_fails_without_writes(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    _collect(service)
    path = service.database.database_path
    service.database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER ck_xhs_snapshot_note_immutable_delete")
        connection.execute(
            "DELETE FROM workbench_schema_migrations WHERE name=?",
            (db_module.XHS_ACCOUNT_SNAPSHOT_EVIDENCE_MIGRATION,),
        )
        before = connection.execute(
            "SELECT * FROM xhs_account_snapshot_notes ORDER BY note_record_id"
        ).fetchall()

    with pytest.raises(SchemaMigrationError, match="Ambiguous populated"):
        Database(path, runtime_dir=tmp_path / "runtime")

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT * FROM xhs_account_snapshot_notes ORDER BY note_record_id"
        ).fetchall() == before
        assert connection.execute(
            "SELECT COUNT(*) FROM workbench_schema_migrations WHERE name=?",
            (db_module.XHS_ACCOUNT_SNAPSHOT_EVIDENCE_MIGRATION,),
        ).fetchone()[0] == 0


def test_v5_case_variant_leftover_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "leftover.sqlite3"
    database = Database(path)
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE XhS_aCcOuNt_NoTeS_sNaPsHoT_V4 (sentinel TEXT)"
        )

    with pytest.raises(SchemaMigrationError, match="Interrupted XHS account snapshot"):
        Database(path)


def _downgrade_one_collection_to_v4(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.create_function(
            "raw_evidence_digest",
            1,
            canonical_raw_evidence_digest,
            deterministic=True,
        )
        connection.execute("PRAGMA foreign_keys=OFF")
        for name in db_module._XHS_ACCOUNT_SNAPSHOT_BINDING_TRIGGERS:
            connection.execute(f'DROP TRIGGER IF EXISTS "{name}"')
        for name in db_module._XHS_ACCOUNT_SNAPSHOT_IMMUTABILITY_TRIGGERS:
            connection.execute(f'DROP TRIGGER IF EXISTS "{name}"')
        connection.execute("DROP TABLE xhs_account_snapshot_notes")
        connection.execute("DROP TABLE xhs_account_profile_snapshots")
        connection.execute(
            "DELETE FROM workbench_schema_migrations WHERE name=?",
            (db_module.XHS_ACCOUNT_SNAPSHOT_EVIDENCE_MIGRATION,),
        )
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='xhs_account_notes'"
        ).fetchone()[0]
        old_sql = table_sql.replace(
            "CONSTRAINT uq_xhs_note_account_collection_identity "
            "UNIQUE (note_id, user_id, collection_job_id)",
            "CONSTRAINT uq_xhs_note_account_identity UNIQUE (note_id, user_id)",
        ).replace(
            "FOREIGN KEY(user_id) REFERENCES xhs_account_profiles (user_id) "
            "ON DELETE RESTRICT",
            "FOREIGN KEY(user_id) REFERENCES xhs_account_profiles (user_id) "
            "ON DELETE CASCADE",
        )
        indexes = [
            row[0]
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND tbl_name='xhs_account_notes' AND sql IS NOT NULL"
            )
        ]
        for name, in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='xhs_account_notes'"
        ).fetchall():
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute(
            "ALTER TABLE xhs_account_notes RENAME TO xhs_account_notes_v5"
        )
        for name, in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='xhs_account_notes_v5' AND sql IS NOT NULL"
        ).fetchall():
            connection.execute(f'DROP INDEX "{name}"')
        connection.execute(old_sql)
        columns = [
            row[1]
            for row in connection.execute("PRAGMA table_info(xhs_account_notes_v5)")
        ]
        rendered = ", ".join(f'"{column}"' for column in columns)
        connection.execute(
            f"INSERT INTO xhs_account_notes ({rendered}) "
            f"SELECT {rendered} FROM xhs_account_notes_v5"
        )
        connection.execute("DROP TABLE xhs_account_notes_v5")
        for definition in indexes:
            connection.execute(definition)
        for name, definition in db_module._XHS_ACCOUNT_NOTE_EVIDENCE_TRIGGER_SQL.items():
            connection.execute(f'DROP TRIGGER IF EXISTS "{name}"')
            connection.execute(definition)
        for definition in db_module._XHS_ACCOUNT_FACT_IMMUTABILITY_TRIGGERS.values():
            connection.execute(definition)


def test_v4_migration_preserves_canonical_id_and_historical_resolution(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    completed = _collect(service)
    path = service.database.database_path
    runtime_dir = service.runtime_dir
    with service.database.session() as session:
        note_id = session.scalar(select(XhsAccountNoteRecord.id))
    assert note_id is not None
    service.database.close()
    _downgrade_one_collection_to_v4(path)

    migrated = Database(path, runtime_dir=runtime_dir)
    try:
        with migrated.session() as session:
            preserved_note = session.get(XhsAccountNoteRecord, note_id)
            assert preserved_note is not None
            assert preserved_note.collection_job_id == completed.id
            assert session.scalar(select(func.count()).select_from(
                XhsAccountProfileSnapshotRecord
            )) == 1
            assert session.scalar(select(func.count()).select_from(
                XhsAccountSnapshotNoteRecord
            )) == 1
        analysis = AnalysisService(migrated, _Model(), runtime_dir=runtime_dir).create(
            AnalysisCreate(
                analysis_type="account_report",
                account_user_id="s2b-user",
                evidence_ids=[f"account-note:{note_id}"],
            )
        )
        assert analysis.status == "succeeded"
        assert analysis.evidence_ids == [f"account-note:{note_id}"]
    finally:
        migrated.close()
