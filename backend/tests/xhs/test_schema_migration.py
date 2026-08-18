import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from backend.app.db import Database, SchemaMigrationError


MIGRATION = "xhs_account_note_evidence_v1"


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
            assert all(columns[name]["nullable"] is False for name in columns)
        with database.engine.connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT COUNT(*) FROM workbench_schema_migrations "
                    "WHERE name=:name"
                ),
                {"name": MIGRATION},
            ) == 1
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


def test_marker_absent_repairs_only_an_empty_half_migration(tmp_path: Path) -> None:
    path = tmp_path / "half.sqlite3"
    database = Database(path)
    database.close()

    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM workbench_schema_migrations WHERE name=?", (MIGRATION,)
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
            "DELETE FROM workbench_schema_migrations WHERE name=?", (MIGRATION,)
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
