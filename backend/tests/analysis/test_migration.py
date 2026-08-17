import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from backend.app.db import Database
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
            connection = sqlite3.connect(database_path)
            connection.execute(
                "INSERT INTO analyses (id,analysis_type,account_user_id,account_user_ids_json,status,prompt_version,provider,model,input_digest,evidence_ids_json,output_json,usage_json,duration_ms,attempts_json,error_category,error_detail,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    json.dumps({"claims": [{"claim": "new", "evidence_ids": ["rank-item:1"]}], "product_clusters": [], "opportunities": []}),
                    json.dumps({}),
                    1,
                    json.dumps([]),
                    None,
                    None,
                    "2026-08-17 13:00:00.000000",
                ),
            )
            connection.commit()
            connection.close()


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
