import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from backend.app.db import (
    Database,
    SchemaMigrationError,
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
)
from backend.app.models.jobs import JobArtifactRecord, JobRecord, JobState


MIGRATION = "xhs_account_note_evidence_v1"
IDENTITY_MIGRATION = "xhs_account_note_identity_v2"


def _insert_trusted_note(database: Database, *, note_id: str) -> int:
    now = datetime.now(UTC).replace(tzinfo=None)
    profile_raw = {"profile": {"user_id": "u1"}}
    note_raw = {"row": {"note_id": note_id, "user_id": "u1"}}
    with database.session() as session:
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
        profile = session.get(XhsAccountProfileRecord, "u1")
        if profile is None:
            profile = XhsAccountProfileRecord(user_id="u1")
            session.add(profile)
        profile.source_url = "https://www.xiaohongshu.com/user/profile/u1"
        profile.nickname = "U1"
        profile.bio = None
        profile.public_stats_json = {}
        profile.raw_evidence = profile_raw
        profile.raw_digest = str(canonical_raw_evidence_digest(profile_raw))
        profile.collection_job_id = job.id
        profile.collection_artifact_id = artifact.id
        profile.collected_at = now
        note = XhsAccountNoteRecord(
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
            collected_at=now,
        )
        session.add(note)
        session.commit()
        return note.id


def _downgrade_note_identity_to_v1(path: Path, *, keep_marker: bool) -> None:
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
        assert {"xhs_account_profiles", "xhs_account_notes"} <= set(
            inspector.get_table_names()
        )
        assert _unique_columns(database, "xhs_account_notes") == {("note_id", "user_id")}
        assert _foreign_key(database, "xhs_account_notes", "user_id") == (
            "xhs_account_profiles",
            "user_id",
            "CASCADE",
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

    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM workbench_schema_migrations WHERE name IN (?, ?)",
            (MIGRATION, IDENTITY_MIGRATION),
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

    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM workbench_schema_migrations WHERE name IN (?, ?)",
            (MIGRATION, IDENTITY_MIGRATION),
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
