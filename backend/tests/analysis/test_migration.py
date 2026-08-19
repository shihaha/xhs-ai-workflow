import hashlib
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from backend.app.db import Database, SchemaMigrationError
from backend.app.main import create_app
from backend.app.settings import Settings


def _create_f0_schema(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE analyses (
          id VARCHAR(36) PRIMARY KEY,
          analysis_type VARCHAR(50) NOT NULL,
          account_user_id VARCHAR(500),
          status VARCHAR(32) NOT NULL,
          prompt_version VARCHAR(100) NOT NULL,
          provider VARCHAR(100) NOT NULL,
          model VARCHAR(300) NOT NULL,
          input_digest VARCHAR(64) NOT NULL,
          evidence_ids_json JSON NOT NULL,
          output_json JSON,
          usage_json JSON NOT NULL,
          duration_ms INTEGER,
          attempts_json JSON NOT NULL,
          error_category VARCHAR(100),
          error_detail TEXT,
          created_at DATETIME NOT NULL
        );
        CREATE TABLE opportunities (
          id VARCHAR(36) PRIMARY KEY,
          analysis_id VARCHAR(36) NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
          title TEXT NOT NULL,
          status VARCHAR(20) NOT NULL,
          summary TEXT NOT NULL,
          evidence_ids_json JSON NOT NULL,
          next_action TEXT NOT NULL,
          created_at DATETIME NOT NULL
        );
        """
    )
    common = (
        "account_report",
        "account-a",
        "tutorial-demand-radar-grounded-v1",
        "alibaba_bailian",
        "deepseek-v4-flash",
        "d" * 64,
        json.dumps(["artifact:1"]),
        json.dumps({"claims": [{"claim": "old", "evidence_ids": ["artifact:1"]}], "product_clusters": [], "opportunities": []}),
        json.dumps({"total_tokens": 1}),
        json.dumps([]),
        "2026-08-17 12:00:00.000000",
    )
    connection.execute(
        "INSERT INTO analyses (id,analysis_type,account_user_id,status,prompt_version,provider,model,input_digest,evidence_ids_json,output_json,usage_json,duration_ms,attempts_json,error_category,error_detail,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("old-success", common[0], common[1], "succeeded", *common[2:9], 1, common[9], None, None, common[10]),
    )
    connection.execute(
        "INSERT INTO analyses (id,analysis_type,account_user_id,status,prompt_version,provider,model,input_digest,evidence_ids_json,output_json,usage_json,duration_ms,attempts_json,error_category,error_detail,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("old-failed", common[0], common[1], "failed", *common[2:7], None, common[8], None, common[9], "model_output_invalid", "old failure", common[10]),
    )
    connection.execute(
        "INSERT INTO opportunities VALUES (?,?,?,?,?,?,?,?)",
        (
            "old-opportunity",
            "old-success",
            "unsafe old card",
            "已验证",
            "old",
            json.dumps(["artifact:1"]),
            "act",
            common[10],
        ),
    )
    connection.commit()
    connection.close()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_f0_database_migrates_idempotently_and_quarantines_old_success(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    database_path = runtime / "db.sqlite3"
    _create_f0_schema(database_path)

    for pass_number in range(2):
        app = create_app(Settings(runtime_dir=runtime, database_path=database_path))
        assert app.state.database is not None
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            analyses = await client.get("/api/v1/analyses")
            opportunities = await client.get("/api/v1/opportunities")
            post = await client.post(
                "/api/v1/analyses",
                json={
                    "analysis_type": "account_report",
                    "account_user_id": "account-a",
                    "evidence_ids": ["artifact:999"],
                },
            )
        assert analyses.status_code == 200
        by_id = {row["id"]: row for row in analyses.json()}
        assert by_id["old-success"]["status"] == "needs_human"
        assert by_id["old-success"]["output"] is None
        assert by_id["old-success"]["error_category"] == "grounding_reverification_required"
        assert by_id["old-success"]["account_user_ids"] == []
        assert by_id["old-failed"]["status"] == "failed"
        if pass_number == 1:
            assert by_id["post-migration-success"]["status"] == "succeeded"
        assert opportunities.json() == []
        assert post.status_code in {422, 503}
        app.state.shop_service.close()
        app.state.database.close()
        if pass_number == 0:
            facts = [{"evidence_id": "rank-item:1", "kind": "rank_item"}]
            trust = [{"evidence_id": "rank-item:1", "row": facts[0]}]
            fingerprint_payload = {
                "account_scope": ["account-a"],
                "allowed_ids": ["rank-item:1"],
                "facts": facts,
                "trust": trust,
                "artifact_bindings": [],
            }
            trust_fingerprint = hashlib.sha256(json.dumps(
                fingerprint_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            evidence_snapshot = {
                "schema_version": 1,
                "trust_fingerprint": trust_fingerprint,
                **fingerprint_payload,
                "input_digest": "e" * 64,
            }
            fixture_database = Database(database_path)
            with fixture_database.engine.begin() as connection:
                connection.exec_driver_sql(
                    "INSERT INTO analyses (id,analysis_type,account_user_id,account_user_ids_json,status,prompt_version,provider,model,input_digest,evidence_ids_json,evidence_snapshot_json,output_json,usage_json,duration_ms,attempts_json,error_category,error_detail,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "post-migration-success",
                        "account_report",
                        "account-a",
                        json.dumps([]),
                        "succeeded",
                        "v2",
                        "replacement",
                        "model",
                        "e" * 64,
                        json.dumps(["rank-item:1"]),
                        json.dumps(evidence_snapshot, ensure_ascii=False),
                        json.dumps({"claims": [{"claim": "new", "evidence_ids": ["rank-item:1"]}], "product_clusters": [], "opportunities": []}),
                        json.dumps({}),
                        1,
                        json.dumps([]),
                        None,
                        None,
                        "2026-08-17 13:00:00.000000",
                    ),
                )
            fixture_database.close()


def test_broken_legacy_analysis_schema_fails_during_startup(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    database_path = runtime / "db.sqlite3"
    connection = sqlite3.connect(database_path)
    connection.execute("CREATE TABLE analyses (id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()

    app = create_app(Settings(runtime_dir=runtime, database_path=database_path))

    assert app.state.database is None
    assert app.state.database_error == "SQLite database is unavailable."


def test_evidence_snapshot_marker_is_validation_only_for_missing_trigger(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "snapshot-marker.sqlite3"
    database = Database(database_path)
    database.close()
    trigger = "ck_analysis_evidence_snapshot_immutable_update"
    with sqlite3.connect(database_path) as connection:
        connection.execute(f"DROP TRIGGER {trigger}")

    with pytest.raises(
        SchemaMigrationError,
        match="evidence snapshot schema validation",
    ):
        Database(database_path)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
            (trigger,),
        ).fetchone() is None


def test_markerless_exact_evidence_snapshot_shape_recovers_missing_trigger(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "snapshot-retry.sqlite3"
    database = Database(database_path)
    database.close()
    trigger = "ck_analysis_evidence_snapshot_immutable_update"
    with sqlite3.connect(database_path) as connection:
        connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute(
            "DELETE FROM workbench_schema_migrations WHERE name=?",
            ("analysis_evidence_snapshot_v3",),
        )

    retried = Database(database_path)
    retried.close()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
            (trigger,),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM workbench_schema_migrations WHERE name=?",
            ("analysis_evidence_snapshot_v3",),
        ).fetchone() == (1,)


def test_v2_success_is_preserved_as_explicit_legacy_unsealed_snapshot(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "analysis-v2.sqlite3"
    _create_f0_schema(database_path)
    connection = _add_scope_column_and_marker(database_path)
    connection.close()

    first = Database(database_path)
    first.close()
    second = Database(database_path)
    second.close()

    with sqlite3.connect(database_path) as connection:
        snapshot_json = connection.execute(
            "SELECT evidence_snapshot_json FROM analyses WHERE id='old-success'"
        ).fetchone()[0]
        marker_count = connection.execute(
            "SELECT COUNT(*) FROM workbench_schema_migrations WHERE name=?",
            ("analysis_evidence_snapshot_v3",),
        ).fetchone()[0]
    assert json.loads(snapshot_json) == {
        "schema_version": 0,
        "status": "legacy_unsealed",
    }
    assert marker_count == 1


@pytest.mark.parametrize("scope_column", ["missing", "nullable_without_default"])
def test_migration_marker_never_overrides_broken_target_schema(
    tmp_path: Path, scope_column: str
) -> None:
    runtime = tmp_path / scope_column
    runtime.mkdir()
    database_path = runtime / "db.sqlite3"
    _create_f0_schema(database_path)
    connection = sqlite3.connect(database_path)
    if scope_column == "nullable_without_default":
        connection.execute("ALTER TABLE analyses ADD COLUMN account_user_ids_json JSON")
    connection.execute(
        "CREATE TABLE workbench_schema_migrations (name VARCHAR(200) PRIMARY KEY, applied_at VARCHAR(40) NOT NULL)"
    )
    connection.execute(
        "INSERT INTO workbench_schema_migrations VALUES ('task7_trusted_grounding_v2','2026-08-17')"
    )
    connection.commit()
    connection.close()

    app = create_app(Settings(runtime_dir=runtime, database_path=database_path))

    assert app.state.database is None
    assert app.state.database_error == "SQLite database is unavailable."


def test_legacy_artifact_provenance_is_migrated_as_external(tmp_path: Path) -> None:
    runtime = tmp_path / "legacy-artifact"
    runtime.mkdir()
    database_path = runtime / "db.sqlite3"
    _create_f0_schema(database_path)
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE jobs (
          id VARCHAR(36) PRIMARY KEY, type VARCHAR(100) NOT NULL, input_data JSON NOT NULL,
          state VARCHAR(32) NOT NULL, progress_current INTEGER NOT NULL,
          progress_total INTEGER, current_stage VARCHAR(255), error_category VARCHAR(100),
          retry_count INTEGER NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
          started_at DATETIME, completed_at DATETIME, lease_expires_at DATETIME
        );
        CREATE TABLE job_artifacts (
          id INTEGER PRIMARY KEY AUTOINCREMENT, job_id VARCHAR(36) NOT NULL REFERENCES jobs(id),
          kind VARCHAR(100) NOT NULL, path TEXT NOT NULL, metadata_json JSON NOT NULL,
          created_at DATETIME NOT NULL
        );
        INSERT INTO jobs VALUES (
          'legacy-job','android_shop_collection','{}','succeeded',1,1,'shop_complete',NULL,0,
          '2026-08-17 12:00:00','2026-08-17 12:00:00',NULL,NULL,NULL
        );
        INSERT INTO job_artifacts (job_id,kind,path,metadata_json,created_at) VALUES (
          'legacy-job','shop_collection_result','evidence/shops/legacy-job/result.json','{}','2026-08-17 12:00:00'
        );
        """
    )
    connection.commit()
    connection.close()

    database = Database(database_path)
    with database.session() as session:
        row = session.execute(
            __import__("sqlalchemy").text("SELECT producer FROM job_artifacts WHERE id=1")
        ).one()
    assert row[0] == "external"
    database.close()


def _add_scope_column_and_marker(
    database_path: Path, *, default_sql: str = "'[]'"
) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.execute(
        "ALTER TABLE analyses ADD COLUMN account_user_ids_json JSON "
        f"NOT NULL DEFAULT {default_sql}"
    )
    connection.execute("UPDATE analyses SET account_user_ids_json='[]'")
    connection.execute(
        "CREATE TABLE workbench_schema_migrations ("
        "name VARCHAR(200) PRIMARY KEY, applied_at VARCHAR(40) NOT NULL)"
    )
    connection.execute(
        "INSERT INTO workbench_schema_migrations VALUES "
        "('task7_trusted_grounding_v2','2026-08-17')"
    )
    connection.commit()
    return connection


@pytest.mark.anyio
async def test_migration_marker_rejects_default_that_only_contains_empty_array_text(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "evil-default"
    runtime.mkdir()
    database_path = runtime / "db.sqlite3"
    _create_f0_schema(database_path)
    connection = _add_scope_column_and_marker(
        database_path, default_sql="(( 'evil[]' ))"
    )
    connection.close()

    app = create_app(Settings(runtime_dir=runtime, database_path=database_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        health = await client.get("/api/v1/health")
        analyses = await client.get("/api/v1/analyses")

    assert app.state.database is None
    assert health.json()["checks"]["database"]["healthy"] is False
    assert analyses.status_code == 503


def test_migration_marker_accepts_parenthesized_exact_empty_array_literal(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "parenthesized-default"
    runtime.mkdir()
    database_path = runtime / "db.sqlite3"
    _create_f0_schema(database_path)
    connection = _add_scope_column_and_marker(
        database_path, default_sql="((( '[]' )))"
    )
    connection.close()

    database = Database(database_path)

    with database.session() as session:
        assert session.execute(
            __import__("sqlalchemy").text("SELECT COUNT(*) FROM analyses")
        ).scalar_one() == 2
    database.close()


@pytest.mark.parametrize(
    "invalid_scope",
    [
        "not-json",
        json.dumps({"account": "account-a"}),
        json.dumps([""]),
        json.dumps(["   "]),
        json.dumps(["account-a", "account-a"]),
        json.dumps(["x" * 501]),
    ],
    ids=[
        "malformed-json",
        "not-a-list",
        "empty-item",
        "blank-item",
        "duplicate-item",
        "oversized-item",
    ],
)
@pytest.mark.anyio
async def test_migration_marker_rejects_invalid_persisted_account_scope_immediately(
    tmp_path: Path, invalid_scope: str
) -> None:
    runtime = tmp_path / "invalid-scope"
    runtime.mkdir()
    database_path = runtime / "db.sqlite3"
    _create_f0_schema(database_path)
    connection = _add_scope_column_and_marker(database_path)
    connection.execute(
        "UPDATE analyses SET account_user_ids_json=? WHERE id='old-success'",
        (invalid_scope,),
    )
    connection.commit()
    connection.close()

    app = create_app(Settings(runtime_dir=runtime, database_path=database_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        health = await client.get("/api/v1/health")
        analyses = await client.get("/api/v1/analyses")

    assert app.state.database is None
    assert health.json()["checks"]["database"]["healthy"] is False
    assert analyses.status_code == 503


def test_half_applied_scope_column_is_backfilled_and_quarantined_on_restart(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "half-applied"
    runtime.mkdir()
    database_path = runtime / "db.sqlite3"
    _create_f0_schema(database_path)
    connection = sqlite3.connect(database_path)
    # Simulate an interrupted prior startup where ALTER survived but its DML and
    # migration marker did not.
    connection.execute(
        "ALTER TABLE analyses ADD COLUMN account_user_ids_json JSON "
        "NOT NULL DEFAULT '[]'"
    )
    connection.execute(
        "UPDATE analyses SET analysis_type='product_cluster' "
        "WHERE id='old-success'"
    )
    connection.commit()
    connection.close()

    first_recovery = Database(database_path)
    first_recovery.close()
    second_recovery = Database(database_path)
    second_recovery.close()

    connection = sqlite3.connect(database_path)
    rows = {
        row[0]: row[1:]
        for row in connection.execute(
            "SELECT id,status,account_user_ids_json,output_json,error_category "
            "FROM analyses ORDER BY id"
        )
    }
    opportunity_count = connection.execute(
        "SELECT COUNT(*) FROM opportunities"
    ).fetchone()[0]
    marker_count = connection.execute(
        "SELECT COUNT(*) FROM workbench_schema_migrations "
        "WHERE name='task7_trusted_grounding_v2'"
    ).fetchone()[0]
    connection.close()

    assert rows["old-success"] == (
        "needs_human",
        json.dumps(["account-a"]),
        None,
        "grounding_reverification_required",
    )
    assert rows["old-failed"][0] == "failed"
    assert json.loads(rows["old-failed"][1]) == []
    assert opportunity_count == 0
    assert marker_count == 1
