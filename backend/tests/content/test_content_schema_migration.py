import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import inspect

from backend.app.db import Database, SchemaMigrationError


def _old_task8(path: Path, *, populated: bool) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE content_products (id TEXT PRIMARY KEY, opportunity_id TEXT, name TEXT, target_user TEXT, created_at TEXT);
            CREATE TABLE content_product_materials (id TEXT PRIMARY KEY, product_id TEXT, logical_name TEXT, version INTEGER, path TEXT, sha256 TEXT, size_bytes INTEGER, media_type TEXT, created_at TEXT);
            CREATE TABLE content_items (id TEXT PRIMARY KEY, product_id TEXT, opportunity_id TEXT, template_key TEXT, status TEXT, evidence_ids_json JSON, material_ids_json JSON, research_facts_json JSON, current_revision_id TEXT, created_at TEXT, updated_at TEXT);
            CREATE TABLE content_revisions (id TEXT PRIMARY KEY, content_item_id TEXT, number INTEGER, title TEXT, body TEXT, claims_json JSON, source_evidence_ids_json JSON, model_provider TEXT, model_name TEXT, prompt_version TEXT, usage_json JSON, attempts_json JSON, created_at TEXT);
            CREATE TABLE content_reviews (id INTEGER PRIMARY KEY, content_item_id TEXT, revision_id TEXT, decision TEXT, actor TEXT, note TEXT, created_at TEXT);
            CREATE TABLE content_packages (id TEXT PRIMARY KEY, content_item_id TEXT, revision_id TEXT, status TEXT, path TEXT, sha256 TEXT, size_bytes INTEGER, created_at TEXT);
            """
        )
        if populated:
            connection.execute(
                "INSERT INTO content_products VALUES (?,?,?,?,?)",
                ("p1", "o1", "legacy", "user", "2026-08-18"),
            )


def test_empty_old_task8_schema_is_conservatively_rebuilt_and_repeatable(tmp_path: Path) -> None:
    path = tmp_path / "old-empty.sqlite3"
    _old_task8(path, populated=False)
    first = Database(path)
    first.close()
    second = Database(path)
    inspector = inspect(second.engine)
    assert "kind" in {item["name"] for item in inspector.get_columns("content_product_materials")}
    assert "uq_review_terminal_revision" in {item["name"] for item in inspector.get_indexes("content_reviews")}
    second.close()


def test_nonempty_old_or_malformed_task8_schema_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "old-populated.sqlite3"
    _old_task8(path, populated=True)
    with pytest.raises(SchemaMigrationError, match="manual migration"):
        Database(path)
