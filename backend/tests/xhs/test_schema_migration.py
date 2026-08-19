import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError

import backend.app.db as db_module
from backend.app.db import (
    Database,
    SchemaMigrationError,
    _rebuild_xhs_account_notes_with_permanent_ids,
    _require_xhs_account_note_canonical_id_preconditions,
    _require_xhs_account_note_identity_preconditions,
    _xhs_account_note_autoincrement_ddl_valid,
    _xhs_account_note_canonical_id_schema_valid,
    _xhs_account_note_identity_schema_valid,
    canonical_raw_evidence_digest,
)
from backend.app.features.analysis.models import AnalysisRecord
from backend.app.features.xhs.constants import (
    ACCOUNT_COLLECTION_ARTIFACT_KIND,
    ACCOUNT_COLLECTION_ARTIFACT_PRODUCER,
    ACCOUNT_COLLECTION_JOB_TYPE,
)
from backend.app.features.xhs.models import (
    XhsAccountNoteRecord,
    XhsAccountProfileRecord,
    XhsAccountProfileSnapshotRecord,
    XhsAccountSnapshotNoteRecord,
)
from backend.app.models.jobs import JobArtifactRecord, JobRecord, JobState


MIGRATION = "xhs_account_note_evidence_v1"
IDENTITY_MIGRATION = "xhs_account_note_identity_v2"
CANONICAL_ID_MIGRATION = "xhs_account_note_canonical_id_v3"
SNAPSHOT_MIGRATION = "xhs_account_snapshot_evidence_v5"
_KNOWN_IDENTITY_TEMPORARY_TABLES = (
    "xhs_account_notes_identity_v1",
    "xhs_account_notes_canonical_id_v2",
)
_IDENTITY_TEMPORARY_TABLE_CASES = (
    "xhs_account_notes_identity_v1",
    "XHS_ACCOUNT_NOTES_IDENTITY_V1",
    "XhS_aCcOuNt_NoTeS_iDeNtItY_V1",
    "xhs_account_notes_canonical_id_v2",
    "XHS_ACCOUNT_NOTES_CANONICAL_ID_V2",
    "XhS_aCcOuNt_NoTeS_cAnOnIcAl_Id_V2",
)
_CASE_VARIANT_REBUILD_CASES = (
    ("XHS_ACCOUNT_NOTES_IDENTITY_V1", "xhs_account_notes_identity_v1"),
    ("XhS_aCcOuNt_NoTeS_iDeNtItY_V1", "xhs_account_notes_identity_v1"),
    ("XHS_ACCOUNT_NOTES_CANONICAL_ID_V2", "xhs_account_notes_canonical_id_v2"),
    ("XhS_aCcOuNt_NoTeS_cAnOnIcAl_Id_V2", "xhs_account_notes_canonical_id_v2"),
)


def _insert_trusted_note(
    database: Database,
    *,
    note_id: str,
    row_id: int | None = None,
) -> int:
    now = datetime.now(UTC).replace(tzinfo=None)
    profile_raw = {"profile": {"user_id": "u1"}}
    note_raw = {"row": {"note_id": note_id, "user_id": "u1"}}
    with database.session() as session:
        profile = session.get(XhsAccountProfileRecord, "u1")
        if profile is None:
            job = JobRecord(
                type=ACCOUNT_COLLECTION_JOB_TYPE,
                input_data={"user_id": "u1", "expected_note_count": 1},
                state=JobState.succeeded.value,
                progress_current=1,
                progress_total=1,
                current_stage="xhs_collection_complete",
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            session.flush()
            artifact = JobArtifactRecord(
                job_id=job.id,
                kind=ACCOUNT_COLLECTION_ARTIFACT_KIND,
                producer=ACCOUNT_COLLECTION_ARTIFACT_PRODUCER,
                path=f"evidence/xhs/{job.id}.json",
                metadata_json={"source": "test"},
                created_at=now,
            )
            session.add(artifact)
            session.flush()
            profile = XhsAccountProfileRecord(
                user_id="u1",
                source_url="https://www.xiaohongshu.com/user/profile/u1",
                nickname="U1",
                bio=None,
                public_stats_json={},
                raw_evidence=profile_raw,
                raw_digest=str(canonical_raw_evidence_digest(profile_raw)),
                collection_job_id=job.id,
                collection_artifact_id=artifact.id,
                collected_at=now,
            )
            session.add(profile)
            session.flush()
            snapshot = XhsAccountProfileSnapshotRecord(
                user_id="u1",
                source_url="https://www.xiaohongshu.com/user/profile/u1",
                nickname="U1",
                bio=None,
                public_stats_json={},
                raw_evidence=profile_raw,
                raw_digest=str(canonical_raw_evidence_digest(profile_raw)),
                collection_job_id=job.id,
                collection_artifact_id=artifact.id,
                collected_at=now,
            )
            session.add(snapshot)
            session.flush()
            position = 0
        else:
            job = session.get(JobRecord, profile.collection_job_id)
            artifact = session.get(
                JobArtifactRecord,
                profile.collection_artifact_id,
            )
            snapshot = session.scalar(select(
                XhsAccountProfileSnapshotRecord
            ).where(
                XhsAccountProfileSnapshotRecord.collection_job_id
                == profile.collection_job_id
            ))
            assert job is not None and artifact is not None and snapshot is not None
            position = len(session.scalars(select(
                XhsAccountSnapshotNoteRecord
            ).where(
                XhsAccountSnapshotNoteRecord.snapshot_id == snapshot.id
            )).all())
        note = XhsAccountNoteRecord(
            id=row_id,
            note_id=note_id,
            user_id="u1",
            source_url=f"https://www.xiaohongshu.com/explore/{note_id}",
            title=note_id,
            summary=None,
            published_at=None,
            public_interactions_json={},
            raw_evidence=note_raw,
            raw_digest=str(canonical_raw_evidence_digest(note_raw)),
            collection_job_id=job.id,
            collection_artifact_id=artifact.id,
            collected_at=profile.collected_at,
        )
        session.add(note)
        session.flush()
        session.add(XhsAccountSnapshotNoteRecord(
            note_record_id=note.id,
            snapshot_id=snapshot.id,
            position=position,
        ))
        session.commit()
        return note.id


def _downgrade_note_identity_to_v1(path: Path, *, keep_marker: bool) -> None:
    _remove_snapshot_layer_for_legacy_migration(path)
    with sqlite3.connect(path) as connection:
        connection.create_function(
            "raw_evidence_digest",
            1,
            canonical_raw_evidence_digest,
            deterministic=True,
        )
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='xhs_account_notes'"
        ).fetchone()[0]
        assert "AUTOINCREMENT" in table_sql.upper()
        triggers = [
            row[0]
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='xhs_account_notes' AND sql IS NOT NULL"
            )
        ]
        indexes = [
            row[0]
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND tbl_name='xhs_account_notes' AND sql IS NOT NULL"
            )
        ]
        connection.execute("PRAGMA foreign_keys=OFF")
        for name, in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='xhs_account_notes'"
        ).fetchall():
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute("ALTER TABLE xhs_account_notes RENAME TO xhs_account_notes_v2")
        for name, in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='xhs_account_notes_v2' AND sql IS NOT NULL"
        ).fetchall():
            connection.execute(f'DROP INDEX "{name}"')
        connection.execute(table_sql.replace("AUTOINCREMENT", ""))
        columns = [
            row[1]
            for row in connection.execute("PRAGMA table_info(xhs_account_notes_v2)")
        ]
        rendered_columns = ", ".join(f'"{column}"' for column in columns)
        connection.execute(
            f"INSERT INTO xhs_account_notes ({rendered_columns}) "
            f"SELECT {rendered_columns} FROM xhs_account_notes_v2"
        )
        connection.execute("DROP TABLE xhs_account_notes_v2")
        for sql in indexes + triggers:
            connection.execute(sql)
        connection.execute(
            "DELETE FROM sqlite_sequence WHERE name IN "
            "('xhs_account_notes', 'xhs_account_notes_v2')"
        )
        connection.execute(
            "DELETE FROM workbench_schema_migrations WHERE name=?",
            (CANONICAL_ID_MIGRATION,),
        )
        if not keep_marker:
            connection.execute(
                "DELETE FROM workbench_schema_migrations WHERE name=?",
                (IDENTITY_MIGRATION,),
            )


def _unique_columns(database: Database, table: str) -> set[tuple[str, ...]]:
    return {
        tuple(item.get("column_names") or ())
        for item in inspect(database.engine).get_unique_constraints(table)
    }


def _identity_disk_state(path: Path) -> tuple[object, ...]:
    with sqlite3.connect(path) as connection:
        temporary_tables = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' "
            "AND name COLLATE NOCASE IN (?, ?) ORDER BY name",
            _KNOWN_IDENTITY_TEMPORARY_TABLES,
        ).fetchall()
        return (
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='xhs_account_notes'"
            ).fetchone()[0],
            connection.execute(
                "SELECT name, applied_at FROM workbench_schema_migrations "
                "WHERE name IN (?, ?, ?) ORDER BY name",
                (MIGRATION, IDENTITY_MIGRATION, CANONICAL_ID_MIGRATION),
            ).fetchall(),
            connection.execute(
                "SELECT * FROM xhs_account_notes ORDER BY id"
            ).fetchall(),
            connection.execute(
                "SELECT evidence_ids_json FROM analyses ORDER BY id"
            ).fetchall(),
            connection.execute(
                "SELECT rowid, name, seq FROM sqlite_sequence "
                "WHERE name LIKE 'xhs_account_notes%' ORDER BY rowid"
            ).fetchall(),
            temporary_tables,
            tuple(
                (
                    table_name,
                    connection.execute(
                        f'SELECT * FROM "{table_name}" ORDER BY rowid'
                    ).fetchall(),
                )
                for table_name, _table_sql in temporary_tables
            ),
        )


def _create_identity_leftover(path: Path, temporary_table: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            f'CREATE TABLE "{temporary_table}" (sentinel TEXT NOT NULL)'
        )
        connection.execute(
            f'INSERT INTO "{temporary_table}"(sentinel) VALUES (?)',
            (f"leftover:{temporary_table}",),
        )


def _downgrade_canonical_note_ids_to_v2(
    path: Path,
    *,
    keep_marker: bool,
) -> None:
    _remove_snapshot_layer_for_legacy_migration(path)
    with sqlite3.connect(path) as connection:
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='xhs_account_notes'"
        ).fetchone()[0]
        downgraded_sql = re.sub(
            r",\s*CONSTRAINT\s+ck_xhs_note_canonical_id\s+"
            r"CHECK\s*\(id\s+BETWEEN\s+1\s+AND\s+9223372036854775807\)",
            "",
            table_sql,
            flags=re.IGNORECASE,
        )
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='table' "
            "AND name='xhs_account_notes'",
            (downgraded_sql,),
        )
        connection.execute("PRAGMA writable_schema=OFF")
        connection.execute(
            "DELETE FROM workbench_schema_migrations WHERE name=?",
            (CANONICAL_ID_MIGRATION,),
        )
        if keep_marker:
            connection.execute(
                "INSERT INTO workbench_schema_migrations(name, applied_at) "
                "VALUES (?, CURRENT_TIMESTAMP)",
                (CANONICAL_ID_MIGRATION,),
            )


def _remove_snapshot_layer_for_legacy_migration(path: Path) -> None:
    """Turn a current fixture into a coherent pre-v5 disk image."""

    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        for name in db_module._XHS_ACCOUNT_SNAPSHOT_BINDING_TRIGGERS:
            connection.execute(f'DROP TRIGGER IF EXISTS "{name}"')
        for name in db_module._XHS_ACCOUNT_SNAPSHOT_IMMUTABILITY_TRIGGERS:
            connection.execute(f'DROP TRIGGER IF EXISTS "{name}"')
        connection.execute("DROP TABLE IF EXISTS xhs_account_snapshot_notes")
        connection.execute("DROP TABLE IF EXISTS xhs_account_profile_snapshots")
        connection.execute(
            "DELETE FROM workbench_schema_migrations WHERE name=?",
            (SNAPSHOT_MIGRATION,),
        )
        for definition in db_module._XHS_ACCOUNT_FACT_IMMUTABILITY_TRIGGERS.values():
            connection.execute(definition)


def _foreign_key(
    database: Database, table: str, column: str
) -> tuple[str, str, str | None] | None:
    for item in inspect(database.engine).get_foreign_keys(table):
        if tuple(item.get("constrained_columns") or ()) == (column,):
            return (
                str(item.get("referred_table")),
                str((item.get("referred_columns") or (None,))[0]),
                (item.get("options") or {}).get("ondelete"),
            )
    return None


def test_fresh_schema_has_bound_account_note_evidence_and_marker(tmp_path: Path) -> None:
    database = Database(tmp_path / "fresh.sqlite3")
    try:
        inspector = inspect(database.engine)
        assert {
            "xhs_account_profiles",
            "xhs_account_notes",
            "xhs_account_profile_snapshots",
            "xhs_account_snapshot_notes",
        } <= set(
            inspector.get_table_names()
        )
        assert _unique_columns(database, "xhs_account_notes") == {
            ("note_id", "user_id", "collection_job_id")
        }
        assert _foreign_key(database, "xhs_account_notes", "user_id") == (
            "xhs_account_profiles",
            "user_id",
            "RESTRICT",
        )
        for table in ("xhs_account_profiles", "xhs_account_notes"):
            columns = {column["name"]: column for column in inspector.get_columns(table)}
            assert {
                "source_url",
                "collection_job_id",
                "collection_artifact_id",
                "raw_digest",
                "collected_at",
            } <= set(columns)
            nullable_public_text = {
                "xhs_account_profiles": {"nickname", "bio"},
                "xhs_account_notes": {"title", "summary", "published_at"},
            }[table]
            assert all(
                columns[name]["nullable"] is False
                for name in set(columns) - nullable_public_text
            )
            assert all(columns[name]["nullable"] is True for name in nullable_public_text)
        with database.engine.connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT COUNT(*) FROM workbench_schema_migrations "
                    "WHERE name=:name"
                ),
                {"name": MIGRATION},
            ) == 1
            assert connection.scalar(
                text(
                    "SELECT COUNT(*) FROM workbench_schema_migrations "
                    "WHERE name=:name"
                ),
                {"name": SNAPSHOT_MIGRATION},
            ) == 1
            table_sql = connection.scalar(
                text(
                    "SELECT sql FROM sqlite_master WHERE type='table' "
                    "AND name='xhs_account_notes'"
                )
            )
            assert "AUTOINCREMENT" in str(table_sql).upper()
            assert connection.scalar(
                text(
                    "SELECT COUNT(*) FROM workbench_schema_migrations "
                    "WHERE name=:name"
                ),
                {"name": IDENTITY_MIGRATION},
            ) == 1
            assert connection.scalar(
                text("SELECT seq FROM sqlite_sequence WHERE name='xhs_account_notes'")
            ) == 0
    finally:
        database.close()


def test_marker_present_with_missing_constraint_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "tampered.sqlite3"
    database = Database(path)
    database.close()
    _remove_snapshot_layer_for_legacy_migration(path)

    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            ALTER TABLE xhs_account_notes RENAME TO xhs_account_notes_old;
            CREATE TABLE xhs_account_notes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              note_id VARCHAR(500) NOT NULL,
              user_id VARCHAR(500) NOT NULL,
              source_url TEXT NOT NULL,
              raw_evidence JSON NOT NULL,
              raw_digest VARCHAR(64) NOT NULL,
              collection_job_id VARCHAR(36) NOT NULL,
              collection_artifact_id INTEGER NOT NULL,
              collected_at DATETIME NOT NULL
            );
            DROP TABLE xhs_account_notes_old;
            """
        )

    with pytest.raises(SchemaMigrationError, match="account note evidence"):
        Database(path)


def test_marker_present_without_public_fact_columns_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "old-marker.sqlite3"
    database = Database(path)
    database.close()
    _remove_snapshot_layer_for_legacy_migration(path)

    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            ALTER TABLE xhs_account_profiles RENAME TO xhs_account_profiles_old;
            CREATE TABLE xhs_account_profiles (
              user_id VARCHAR(500) PRIMARY KEY,
              source_url TEXT NOT NULL,
              raw_evidence JSON NOT NULL,
              raw_digest VARCHAR(64) NOT NULL,
              collection_job_id VARCHAR(36) NOT NULL,
              collection_artifact_id INTEGER NOT NULL,
              collected_at DATETIME NOT NULL
            );
            DROP TABLE xhs_account_profiles_old;
            """
        )

    with pytest.raises(SchemaMigrationError, match="account note evidence"):
        Database(path)


def test_marker_absent_repairs_only_an_empty_half_migration(tmp_path: Path) -> None:
    path = tmp_path / "half.sqlite3"
    database = Database(path)
    database.close()
    _remove_snapshot_layer_for_legacy_migration(path)

    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM workbench_schema_migrations WHERE name IN (?, ?, ?)",
            (MIGRATION, IDENTITY_MIGRATION, CANONICAL_ID_MIGRATION),
        )
        connection.execute("DROP TABLE xhs_account_notes")

    recovered = Database(path)
    try:
        assert "xhs_account_notes" in inspect(recovered.engine).get_table_names()
        with recovered.engine.connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT COUNT(*) FROM workbench_schema_migrations WHERE name=:name"
                ),
                {"name": MIGRATION},
            ) == 1
    finally:
        recovered.close()


def test_marker_absent_populated_weakened_schema_requires_manual_migration(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    database = Database(path)
    database.close()
    _remove_snapshot_layer_for_legacy_migration(path)

    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM workbench_schema_migrations WHERE name IN (?, ?, ?)",
            (MIGRATION, IDENTITY_MIGRATION, CANONICAL_ID_MIGRATION),
        )
        connection.executescript(
            """
            DROP TABLE xhs_account_notes;
            CREATE TABLE xhs_account_notes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              note_id VARCHAR(500) NOT NULL,
              user_id VARCHAR(500) NOT NULL,
              source_url TEXT NOT NULL,
              raw_evidence JSON NOT NULL,
              raw_digest VARCHAR(64) NOT NULL,
              collection_job_id VARCHAR(36) NOT NULL,
              collection_artifact_id INTEGER NOT NULL,
              collected_at DATETIME NOT NULL
            );
            INSERT INTO xhs_account_notes VALUES (
              1, 'legacy-note', 'legacy-user', 'https://www.xiaohongshu.com/explore/legacy-note',
              '{}', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
              'legacy-job', 1, CURRENT_TIMESTAMP
            );
            """
        )

    with pytest.raises(SchemaMigrationError, match="manual migration"):
        Database(path)


def test_identity_migration_preserves_ids_and_advances_sequence(tmp_path: Path) -> None:
    path = tmp_path / "legacy-v1.sqlite3"
    database = Database(path)
    first_id = _insert_trusted_note(database, note_id="legacy-1")
    second_id = _insert_trusted_note(database, note_id="legacy-2")
    database.close()
    _downgrade_note_identity_to_v1(path, keep_marker=False)

    migrated = Database(path)
    try:
        with migrated.engine.connect() as connection:
            assert connection.execute(
                text("SELECT id, note_id FROM xhs_account_notes ORDER BY id")
            ).all() == [(first_id, "legacy-1"), (second_id, "legacy-2")]
            assert connection.scalar(
                text("SELECT seq FROM sqlite_sequence WHERE name='xhs_account_notes'")
            ) >= second_id
            assert connection.scalar(
                text(
                    "SELECT COUNT(*) FROM workbench_schema_migrations "
                    "WHERE name=:name"
                ),
                {"name": IDENTITY_MIGRATION},
            ) == 1
    finally:
        migrated.close()


def test_identity_marker_present_is_validation_only_and_rejects_old_ddl(
    tmp_path: Path,
) -> None:
    path = tmp_path / "identity-marker-tamper.sqlite3"
    database = Database(path)
    database.close()
    _downgrade_note_identity_to_v1(path, keep_marker=True)

    with pytest.raises(SchemaMigrationError, match="note identity"):
        Database(path)

    with sqlite3.connect(path) as connection:
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='xhs_account_notes'"
        ).fetchone()[0]
        assert "AUTOINCREMENT" not in table_sql.upper()
        assert connection.execute(
            "SELECT COUNT(*) FROM workbench_schema_migrations WHERE name=?",
            (IDENTITY_MIGRATION,),
        ).fetchone()[0] == 1


def test_identity_marker_absent_with_finished_ddl_is_retry_safe(tmp_path: Path) -> None:
    path = tmp_path / "identity-half.sqlite3"
    database = Database(path)
    note_id = _insert_trusted_note(database, note_id="preserved")
    database.close()
    with sqlite3.connect(path) as connection:
        sequence = connection.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='xhs_account_notes'"
        ).fetchone()[0]
        connection.execute(
            "DELETE FROM workbench_schema_migrations WHERE name=?",
            (IDENTITY_MIGRATION,),
        )

    recovered = Database(path)
    try:
        with recovered.engine.connect() as connection:
            assert connection.scalar(
                text("SELECT note_id FROM xhs_account_notes WHERE id=:id"),
                {"id": note_id},
            ) == "preserved"
            assert connection.scalar(
                text("SELECT seq FROM sqlite_sequence WHERE name='xhs_account_notes'")
            ) == sequence
    finally:
        recovered.close()


def test_identity_marker_rejects_sequence_behind_persisted_rows(tmp_path: Path) -> None:
    path = tmp_path / "identity-sequence-tamper.sqlite3"
    database = Database(path)
    _insert_trusted_note(database, note_id="sequence-1")
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE sqlite_sequence SET seq=0 WHERE name='xhs_account_notes'"
        )

    with pytest.raises(SchemaMigrationError, match="note identity"):
        Database(path)


def test_identity_migration_advances_past_historical_analysis_citations(
    tmp_path: Path,
) -> None:
    path = tmp_path / "identity-historical-citation.sqlite3"
    database = Database(path)
    first_id = _insert_trusted_note(database, note_id="current")
    historical_id = first_id + 40
    now = datetime.now(UTC).replace(tzinfo=None)
    with database.session() as session:
        session.add(AnalysisRecord(
            analysis_type="account_report",
            account_user_id="u1",
            account_user_ids_json=[],
            status="failed",
            prompt_version="historical-v1",
            provider="historical",
            model="historical",
            input_digest="0" * 64,
            evidence_ids_json=[f"account-note:{historical_id}"],
            output_json=None,
            usage_json={},
            duration_ms=None,
            attempts_json=[],
            error_category="historical",
            error_detail="historical",
            created_at=now,
        ))
        session.commit()
    database.close()
    _downgrade_note_identity_to_v1(path, keep_marker=False)

    migrated = Database(path)
    try:
        with migrated.engine.connect() as connection:
            assert connection.scalar(
                text("SELECT seq FROM sqlite_sequence WHERE name='xhs_account_notes'")
            ) >= historical_id
        next_id = _insert_trusted_note(migrated, note_id="after-history")
        assert next_id > historical_id
    finally:
        migrated.close()


def test_identity_migration_rejects_malformed_reference_history_before_writes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "identity-malformed-history.sqlite3"
    database = Database(path)
    now = datetime.now(UTC).replace(tzinfo=None)
    with database.session() as session:
        session.add(AnalysisRecord(
            analysis_type="account_report",
            account_user_id="u1",
            account_user_ids_json=[],
            status="failed",
            prompt_version="historical-v1",
            provider="historical",
            model="historical",
            input_digest="0" * 64,
            evidence_ids_json=[],
            output_json=None,
            usage_json={},
            duration_ms=None,
            attempts_json=[],
            error_category="historical",
            error_detail="historical",
            created_at=now,
        ))
        session.commit()
    database.close()
    _downgrade_note_identity_to_v1(path, keep_marker=False)
    with sqlite3.connect(path) as connection:
        connection.create_function(
            "analysis_evidence_snapshot_v1_valid",
            5,
            lambda *_values: 1,
            deterministic=True,
        )
        connection.execute("UPDATE analyses SET evidence_ids_json='not-json'")

    with pytest.raises(SchemaMigrationError, match="reference history"):
        Database(path)

    with sqlite3.connect(path) as connection:
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='xhs_account_notes'"
        ).fetchone()[0]
        assert "AUTOINCREMENT" not in table_sql.upper()
        assert connection.execute(
            "SELECT COUNT(*) FROM workbench_schema_migrations WHERE name=?",
            (IDENTITY_MIGRATION,),
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "spoof",
    [
        "note TEXT /* id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT */",
        "note TEXT -- id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT\n",
        "note TEXT DEFAULT 'idintegernotnullprimarykeyautoincrement'",
        '"idintegernotnullprimarykeyautoincrement" TEXT',
    ],
    ids=["block-comment", "line-comment", "string-literal", "quoted-identifier"],
)
def test_identity_ddl_validator_ignores_non_code_autoincrement_spoofs(
    tmp_path: Path,
    spoof: str,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'ddl-spoof.sqlite3'}")
    try:
        with engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE xhs_account_notes ("
                "id INTEGER NOT NULL PRIMARY KEY, "
                f"{spoof})"
            ))
            assert _xhs_account_note_autoincrement_ddl_valid(connection) is False
    finally:
        engine.dispose()


def test_identity_ddl_validator_accepts_quoted_real_id_column(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'quoted-id.sqlite3'}")
    try:
        with engine.begin() as connection:
            connection.execute(text(
                'CREATE TABLE xhs_account_notes ('
                '"id" INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, note TEXT)'
            ))
            assert _xhs_account_note_autoincrement_ddl_valid(connection) is True
    finally:
        engine.dispose()


def test_identity_marker_comment_spoof_is_validation_only_failure(
    tmp_path: Path,
) -> None:
    path = tmp_path / "identity-comment-spoof.sqlite3"
    database = Database(path)
    database.close()
    with sqlite3.connect(path) as connection:
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='xhs_account_notes'"
        ).fetchone()[0]
        spoofed_sql = (
            table_sql.replace("PRIMARY KEY AUTOINCREMENT", "PRIMARY KEY")
            + " /* id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT */"
        )
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='table' "
            "AND name='xhs_account_notes'",
            (spoofed_sql,),
        )
        connection.execute("PRAGMA writable_schema=OFF")
    before = _identity_disk_state(path)

    with pytest.raises(SchemaMigrationError, match="note identity"):
        Database(path)

    assert _identity_disk_state(path) == before


@pytest.mark.parametrize(
    "evidence_id",
    [
        "account-note:9223372036854775808",
        f"account-note:{'9' * 10_000}",
        "account-note:-1",
        "account-note:0",
        "account-note:01",
        "account-note:+1",
        "account-note:not-a-number",
    ],
    ids=["int64-overflow", "overlong", "negative", "zero", "leading-zero", "plus", "text"],
)
def test_identity_migration_rejects_invalid_historical_ids_before_writes(
    tmp_path: Path,
    evidence_id: str,
) -> None:
    path = tmp_path / "identity-invalid-history.sqlite3"
    database = Database(path)
    _insert_trusted_note(database, note_id="preserved")
    now = datetime.now(UTC).replace(tzinfo=None)
    with database.session() as session:
        session.add(AnalysisRecord(
            analysis_type="account_report",
            account_user_id="u1",
            account_user_ids_json=[],
            status="failed",
            prompt_version="historical-v1",
            provider="historical",
            model="historical",
            input_digest="0" * 64,
            evidence_ids_json=[evidence_id],
            output_json=None,
            usage_json={},
            duration_ms=None,
            attempts_json=[],
            error_category="historical",
            error_detail="historical",
            created_at=now,
        ))
        session.commit()
    database.close()
    _downgrade_note_identity_to_v1(path, keep_marker=False)
    before = _identity_disk_state(path)

    with pytest.raises(SchemaMigrationError, match="reference history"):
        Database(path)

    assert _identity_disk_state(path) == before


def test_identity_half_migration_with_new_ddl_and_old_table_fails_every_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "identity-half-restart.sqlite3"
    database = Database(path)
    _insert_trusted_note(database, note_id="preserved")
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM workbench_schema_migrations WHERE name=?",
            (IDENTITY_MIGRATION,),
        )
        connection.execute(
            "CREATE TABLE xhs_account_notes_identity_v1 "
            "(sentinel TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO xhs_account_notes_identity_v1(sentinel) VALUES ('old')"
        )
    before = _identity_disk_state(path)

    for _attempt in range(2):
        with pytest.raises(SchemaMigrationError, match="Interrupted"):
            Database(path)
        assert _identity_disk_state(path) == before


@pytest.mark.parametrize(
    "temporary_table",
    _IDENTITY_TEMPORARY_TABLE_CASES,
)
def test_identity_marker_validator_rejects_every_known_leftover_without_writes(
    tmp_path: Path,
    temporary_table: str,
) -> None:
    path = tmp_path / f"identity-marker-leftover-{temporary_table}.sqlite3"
    database = Database(path)
    _insert_trusted_note(database, note_id="preserved")
    _create_identity_leftover(path, temporary_table)
    before = _identity_disk_state(path)
    try:
        with database.engine.connect() as connection:
            assert _xhs_account_note_identity_schema_valid(connection) is False
    finally:
        database.close()

    for _attempt in range(2):
        with pytest.raises(SchemaMigrationError, match="note identity"):
            Database(path)
        assert _identity_disk_state(path) == before


@pytest.mark.parametrize(
    "temporary_table",
    _IDENTITY_TEMPORARY_TABLE_CASES,
)
def test_identity_marker_absent_preflight_rejects_every_known_leftover_without_writes(
    tmp_path: Path,
    temporary_table: str,
) -> None:
    path = tmp_path / f"identity-preflight-leftover-{temporary_table}.sqlite3"
    database = Database(path)
    _insert_trusted_note(database, note_id="preserved")
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM workbench_schema_migrations "
                "WHERE name=:marker"
            ),
            {"marker": IDENTITY_MIGRATION},
        )
    _create_identity_leftover(path, temporary_table)
    before = _identity_disk_state(path)
    try:
        for _attempt in range(2):
            with database.engine.connect() as connection:
                with pytest.raises(SchemaMigrationError, match="Interrupted"):
                    _require_xhs_account_note_identity_preconditions(connection)
            assert _identity_disk_state(path) == before
    finally:
        database.close()

    for _attempt in range(2):
        with pytest.raises(SchemaMigrationError, match="Interrupted"):
            Database(path)
        assert _identity_disk_state(path) == before


@pytest.mark.parametrize(
    ("temporary_table", "rebuild_target"),
    _CASE_VARIANT_REBUILD_CASES,
)
def test_rebuild_rejects_ascii_case_variant_known_leftover_without_writes(
    tmp_path: Path,
    temporary_table: str,
    rebuild_target: str,
) -> None:
    path = tmp_path / f"rebuild-case-leftover-{temporary_table}.sqlite3"
    database = Database(path)
    _insert_trusted_note(database, note_id="preserved")
    _create_identity_leftover(path, temporary_table)
    before = _identity_disk_state(path)
    try:
        with database.engine.begin() as connection:
            with pytest.raises(SchemaMigrationError, match="Interrupted"):
                _rebuild_xhs_account_notes_with_permanent_ids(
                    connection,
                    XhsAccountNoteRecord,
                    temporary_table=rebuild_target,
                )
        assert _identity_disk_state(path) == before
    finally:
        database.close()


def test_unicode_casefold_lookalike_is_not_a_sqlite_identifier_match(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unicode-casefold-control.sqlite3"
    database = Database(path)
    database.close()
    lookalike = "xhſ_account_notes_identity_v1"
    with sqlite3.connect(path) as connection:
        connection.execute(f'CREATE TABLE "{lookalike}" (sentinel TEXT NOT NULL)')
        connection.execute(
            f'INSERT INTO "{lookalike}"(sentinel) VALUES (?)',
            ("unicode-control",),
        )
        before = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND name=?",
            (lookalike,),
        ).fetchall(), connection.execute(
            f'SELECT * FROM "{lookalike}" ORDER BY rowid'
        ).fetchall()

    restarted = Database(path)
    restarted.close()

    with sqlite3.connect(path) as connection:
        after = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND name=?",
            (lookalike,),
        ).fetchall(), connection.execute(
            f'SELECT * FROM "{lookalike}" ORDER BY rowid'
        ).fetchall()
    assert after == before


def test_fresh_schema_has_canonical_note_id_check_and_v3_marker(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "fresh-canonical-id.sqlite3")
    try:
        checks = {
            item.get("name"): "".join(str(item.get("sqltext") or "").split()).lower()
            for item in inspect(database.engine).get_check_constraints(
                "xhs_account_notes"
            )
        }
        assert checks["ck_xhs_note_canonical_id"] == (
            "idbetween1and9223372036854775807"
        )
        with database.engine.connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT COUNT(*) FROM workbench_schema_migrations "
                    "WHERE name=:name"
                ),
                {"name": CANONICAL_ID_MIGRATION},
            ) == 1
    finally:
        database.close()


@pytest.mark.parametrize("row_id", [0, -1])
def test_fresh_schema_rejects_explicit_noncanonical_note_id(
    tmp_path: Path,
    row_id: int,
) -> None:
    database = Database(tmp_path / f"fresh-invalid-{row_id}.sqlite3")
    try:
        with pytest.raises(IntegrityError):
            _insert_trusted_note(database, note_id=f"invalid-{row_id}", row_id=row_id)
    finally:
        database.close()


def test_canonical_id_marker_present_is_validation_only(tmp_path: Path) -> None:
    path = tmp_path / "canonical-marker-tamper.sqlite3"
    database = Database(path)
    database.close()
    _downgrade_canonical_note_ids_to_v2(path, keep_marker=True)
    before = _identity_disk_state(path)

    with pytest.raises(SchemaMigrationError, match="canonical id"):
        Database(path)

    assert _identity_disk_state(path) == before


@pytest.mark.parametrize("row_ids", [(0,), (-1, 2)], ids=["zero", "mixed-negative"])
def test_canonical_id_preflight_rejects_populated_noncanonical_v2_without_writes(
    tmp_path: Path,
    row_ids: tuple[int, ...],
) -> None:
    path = tmp_path / "canonical-invalid-v2.sqlite3"
    database = Database(path)
    canonical_ids: list[int] = []
    for index, _row_id in enumerate(row_ids):
        canonical_ids.append(
            _insert_trusted_note(
                database,
                note_id=f"legacy-{index}",
            )
        )
    database.close()
    _downgrade_canonical_note_ids_to_v2(path, keep_marker=False)
    with sqlite3.connect(path) as connection:
        for canonical_id, row_id in zip(canonical_ids, row_ids, strict=True):
            connection.execute(
                "UPDATE xhs_account_notes SET id=? WHERE id=?",
                (row_id, canonical_id),
            )
    before = _identity_disk_state(path)

    with pytest.raises(SchemaMigrationError, match="canonical id"):
        Database(path)

    assert _identity_disk_state(path) == before


def test_canonical_id_migration_preserves_rows_and_sequence_high_water(
    tmp_path: Path,
) -> None:
    path = tmp_path / "canonical-v2-migration.sqlite3"
    database = Database(path)
    first_id = _insert_trusted_note(database, note_id="preserved")
    database.close()
    _downgrade_canonical_note_ids_to_v2(path, keep_marker=False)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE sqlite_sequence SET seq=41 WHERE name='xhs_account_notes'"
        )

    migrated = Database(path)
    try:
        with migrated.engine.connect() as connection:
            assert connection.execute(
                text("SELECT id, note_id FROM xhs_account_notes ORDER BY id")
            ).all() == [(first_id, "preserved")]
            assert connection.scalar(
                text(
                    "SELECT seq FROM sqlite_sequence "
                    "WHERE name='xhs_account_notes'"
                )
            ) == 41
            assert connection.scalar(
                text(
                    "SELECT COUNT(*) FROM workbench_schema_migrations "
                    "WHERE name=:name"
                ),
                {"name": CANONICAL_ID_MIGRATION},
            ) == 1
            checks = {
                item.get("name")
                for item in inspect(connection).get_check_constraints(
                    "xhs_account_notes"
                )
            }
            assert "ck_xhs_note_canonical_id" in checks
    finally:
        migrated.close()


def test_canonical_id_half_migration_fails_every_restart_without_changes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "canonical-half-restart.sqlite3"
    database = Database(path)
    _insert_trusted_note(database, note_id="preserved")
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM workbench_schema_migrations WHERE name=?",
            (CANONICAL_ID_MIGRATION,),
        )
        connection.execute(
            "CREATE TABLE xhs_account_notes_canonical_id_v2 "
            "(sentinel TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO xhs_account_notes_canonical_id_v2(sentinel) "
            "VALUES ('old')"
        )
    before = _identity_disk_state(path)

    for _attempt in range(2):
        with pytest.raises(SchemaMigrationError, match="Interrupted"):
            Database(path)
        assert _identity_disk_state(path) == before


def test_canonical_id_marker_rejects_leftover_half_migration_table(
    tmp_path: Path,
) -> None:
    path = tmp_path / "canonical-marker-half.sqlite3"
    database = Database(path)
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE xhs_account_notes_canonical_id_v2 "
            "(sentinel TEXT NOT NULL)"
        )
    before = _identity_disk_state(path)

    with pytest.raises(SchemaMigrationError, match="canonical id"):
        Database(path)

    assert _identity_disk_state(path) == before


@pytest.mark.parametrize(
    "temporary_table",
    _IDENTITY_TEMPORARY_TABLE_CASES,
)
def test_canonical_id_marker_validator_rejects_every_known_leftover_without_writes(
    tmp_path: Path,
    temporary_table: str,
) -> None:
    path = tmp_path / f"canonical-marker-leftover-{temporary_table}.sqlite3"
    database = Database(path)
    _insert_trusted_note(database, note_id="preserved")
    _create_identity_leftover(path, temporary_table)
    before = _identity_disk_state(path)
    try:
        for _attempt in range(2):
            with database.engine.connect() as connection:
                assert _xhs_account_note_canonical_id_schema_valid(connection) is False
            with pytest.raises(SchemaMigrationError, match="canonical id"):
                database._require_xhs_account_note_canonical_id_schema()
            assert _identity_disk_state(path) == before
    finally:
        database.close()

    for _attempt in range(2):
        with pytest.raises(SchemaMigrationError, match="Interrupted"):
            Database(path)
        assert _identity_disk_state(path) == before


@pytest.mark.parametrize(
    "temporary_table",
    _IDENTITY_TEMPORARY_TABLE_CASES,
)
def test_canonical_id_marker_absent_preflight_rejects_every_known_leftover_without_writes(
    tmp_path: Path,
    temporary_table: str,
) -> None:
    path = tmp_path / f"canonical-preflight-leftover-{temporary_table}.sqlite3"
    database = Database(path)
    _insert_trusted_note(database, note_id="preserved")
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM workbench_schema_migrations "
                "WHERE name=:marker"
            ),
            {"marker": CANONICAL_ID_MIGRATION},
        )
    _create_identity_leftover(path, temporary_table)
    before = _identity_disk_state(path)
    try:
        for _attempt in range(2):
            with database.engine.connect() as connection:
                with pytest.raises(SchemaMigrationError, match="Interrupted"):
                    _require_xhs_account_note_canonical_id_preconditions(connection)
            assert _identity_disk_state(path) == before
    finally:
        database.close()

    for _attempt in range(2):
        with pytest.raises(SchemaMigrationError, match="Interrupted"):
            Database(path)
        assert _identity_disk_state(path) == before
