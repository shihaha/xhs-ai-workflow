from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import inspect

from backend.app.db import Database, SchemaMigrationError


MARKER = "content_media_runs_v1"


def test_fresh_and_legacy_database_install_media_schema_idempotently(tmp_path: Path) -> None:
    for name in ("fresh.sqlite3", "legacy.sqlite3"):
        path = tmp_path / name
        if name.startswith("legacy"):
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE legacy_business_fact (id INTEGER PRIMARY KEY)")
                connection.execute("INSERT INTO legacy_business_fact VALUES (1)")
        first = Database(path)
        first.close()
        second = Database(path)
        assert "content_media_runs" in inspect(second.engine).get_table_names()
        with sqlite3.connect(path) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM workbench_schema_migrations WHERE name=?", (MARKER,)
            ).fetchone() == (1,)
        second.close()


def test_marker_present_with_weakened_index_fails_closed_without_repair(tmp_path: Path) -> None:
    path = tmp_path / "weakened.sqlite3"
    database = Database(path)
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX uq_content_media_open_generation")

    with pytest.raises(SchemaMigrationError, match="content media run schema"):
        Database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='uq_content_media_open_generation'"
        ).fetchone() is None


def test_markerless_half_migration_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "half.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE content_media_runs (id TEXT PRIMARY KEY, status TEXT NOT NULL)")

    with pytest.raises(SchemaMigrationError, match="content media run schema"):
        Database(path)


def test_marker_present_with_weakened_check_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "weakened-check.sqlite3"
    database = Database(path)
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("ALTER TABLE content_media_runs RENAME TO content_media_runs_old")
        connection.execute("CREATE TABLE content_media_runs AS SELECT * FROM content_media_runs_old")
        connection.execute("DROP TABLE content_media_runs_old")
        connection.execute("PRAGMA foreign_keys=ON")

    with pytest.raises(SchemaMigrationError, match="content media run schema"):
        Database(path)
