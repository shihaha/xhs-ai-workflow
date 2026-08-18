import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from backend.app.db import Database, SchemaMigrationError
from backend.app.adapters.contracts import ModelAdapterError
from backend.app.features.content.schemas import ContentItemCreate, RegenerateCreate, ReviewCreate
from backend.app.features.content.service import ContentModelFailure
from backend.tests.content.test_workflow import (
    add_output_image,
    create_product,
    seeded_service,
)


MIGRATION = "task8_content_review_outcome_v1"


def _replace_reviews_with_legacy_table(path: Path, *, include_audit_columns: bool) -> None:
    audit_columns = (
        ", outcome VARCHAR(20) NOT NULL DEFAULT 'succeeded', error_category VARCHAR(40)"
        if include_audit_columns else ""
    )
    insert_audit = ", outcome, error_category" if include_audit_columns else ""
    select_audit = ", 'succeeded', NULL" if include_audit_columns else ""
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("ALTER TABLE content_reviews RENAME TO content_reviews_old")
        connection.execute(
            f"""
            CREATE TABLE content_reviews (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                content_item_id VARCHAR(36) NOT NULL,
                revision_id VARCHAR(36) NOT NULL,
                decision VARCHAR(20) NOT NULL,
                actor VARCHAR(200) NOT NULL,
                note TEXT NOT NULL,
                visual_checks_json JSON NOT NULL,
                created_at DATETIME NOT NULL
                {audit_columns},
                CONSTRAINT fk_review_revision_same_item FOREIGN KEY(revision_id, content_item_id)
                    REFERENCES content_revisions(id, content_item_id),
                FOREIGN KEY(content_item_id) REFERENCES content_items(id) ON DELETE CASCADE,
                CONSTRAINT ck_review_decision CHECK (decision IN ('approve','reject','regenerate'))
            )
            """
        )
        connection.execute(
            "INSERT INTO content_reviews "
            "(id,content_item_id,revision_id,decision,actor,note,visual_checks_json,created_at"
            f"{insert_audit}) SELECT id,content_item_id,revision_id,decision,actor,note,"
            f"visual_checks_json,created_at{select_audit} FROM content_reviews_old"
        )
        connection.execute("DROP TABLE content_reviews_old")
        connection.execute(
            "CREATE INDEX ix_content_reviews_content_item_id ON content_reviews(content_item_id)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX uq_review_terminal_revision ON content_reviews(revision_id) "
            "WHERE decision IN ('approve','reject')"
        )
        connection.execute(
            "DELETE FROM workbench_schema_migrations WHERE name=?", (MIGRATION,)
        )
        connection.commit()


def test_fresh_review_audit_schema_is_strict_and_marked(tmp_path: Path) -> None:
    database = Database(tmp_path / "fresh.sqlite3")
    inspector = inspect(database.engine)
    columns = {column["name"]: column for column in inspector.get_columns("content_reviews")}
    checks = {check["name"] for check in inspector.get_check_constraints("content_reviews")}

    assert columns["outcome"]["nullable"] is False
    assert columns["error_category"]["nullable"] is True
    assert {"ck_review_outcome", "ck_review_error_category"}.issubset(checks)
    with database.engine.connect() as connection:
        assert connection.scalar(text(
            "SELECT COUNT(*) FROM workbench_schema_migrations WHERE name=:name"
        ), {"name": MIGRATION}) == 1


def test_legacy_reviews_backfill_succeeded_and_migration_is_repeatable(tmp_path: Path) -> None:
    service, opportunity_id, evidence_id = seeded_service(tmp_path)
    product_id = create_product(service, opportunity_id)
    image = add_output_image(service, product_id, tmp_path)
    item = service.create_content_item(ContentItemCreate(
        product_id=product_id, opportunity_id=opportunity_id,
        template_key="list-v1", evidence_ids=[evidence_id],
        image_material_ids=[image.id], cover_material_id=image.id,
        research_facts=[{"fact": "真实事实", "evidence_ids": [evidence_id]}],
    ))
    service.review(item.id, ReviewCreate(
        decision="reject", actor="operator", note="需要修改",
        expected_revision_id=item.current_revision.id, visual_checks=[],
    ))
    path = service.database.database_path
    service.database.close()
    _replace_reviews_with_legacy_table(path, include_audit_columns=False)

    migrated = Database(path)
    with migrated.engine.connect() as connection:
        row = connection.execute(text(
            "SELECT outcome,error_category FROM content_reviews"
        )).one()
        assert row == ("succeeded", None)
        with pytest.raises(IntegrityError):
            connection.execute(text(
                "UPDATE content_reviews SET outcome='failed', error_category=NULL"
            ))
    migrated.close()
    repeated = Database(path)
    repeated.close()


def test_legacy_regenerate_backfill_requires_a_proving_successor_revision(
    tmp_path: Path,
) -> None:
    service, opportunity_id, evidence_id = seeded_service(tmp_path)
    product_id = create_product(service, opportunity_id)
    image = add_output_image(service, product_id, tmp_path)

    def rejected_item() -> object:
        item = service.create_content_item(ContentItemCreate(
            product_id=product_id, opportunity_id=opportunity_id,
            template_key="list-v1", evidence_ids=[evidence_id],
            image_material_ids=[image.id], cover_material_id=image.id,
            research_facts=[{"fact": "真实事实", "evidence_ids": [evidence_id]}],
        ))
        return service.review(item.id, ReviewCreate(
            decision="reject", actor="operator", note="需要修改",
            expected_revision_id=item.current_revision.id, visual_checks=[],
        ))

    successful = rejected_item()
    service.regenerate(successful.id, RegenerateCreate(
        expected_revision_id=successful.current_revision.id,
    ))
    failed = rejected_item()

    def model_error(request: object, schema: object):
        raise ModelAdapterError("legacy provider failure", category="network")

    service.model_adapter.generate_structured = model_error  # type: ignore[attr-defined,method-assign]
    with pytest.raises(ContentModelFailure):
        service.regenerate(failed.id, RegenerateCreate(
            expected_revision_id=failed.current_revision.id,
        ))

    path = service.database.database_path
    service.database.close()
    _replace_reviews_with_legacy_table(path, include_audit_columns=False)

    migrated = Database(path)
    with migrated.engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT content_item_id,outcome,error_category FROM content_reviews "
            "WHERE decision='regenerate' ORDER BY content_item_id"
        )).all()
    migrated.close()

    outcomes = {row.content_item_id: (row.outcome, row.error_category) for row in rows}
    assert outcomes[successful.id] == ("succeeded", None)
    assert outcomes[failed.id] == ("failed", "state_changed")


def test_present_review_audit_marker_with_weak_schema_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "weak.sqlite3"
    database = Database(path)
    database.close()
    _replace_reviews_with_legacy_table(path, include_audit_columns=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO workbench_schema_migrations(name,applied_at) VALUES (?,CURRENT_TIMESTAMP)",
            (MIGRATION,),
        )
        connection.commit()

    with pytest.raises(SchemaMigrationError, match="review audit schema"):
        Database(path)
