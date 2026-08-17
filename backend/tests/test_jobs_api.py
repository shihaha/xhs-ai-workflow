from pathlib import Path

import httpx
import pytest

from backend.app.main import create_app
from backend.app.settings import Settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_job_progress_logs_and_evidence_survive_a_new_app_instance(
    tmp_path: Path,
) -> None:
    """A new process must return persisted task facts rather than an in-memory view."""
    runtime_dir = tmp_path / "runtime"
    settings = Settings(
        runtime_dir=runtime_dir,
        database_path=runtime_dir / "workbench.sqlite3",
    )
    evidence_file = runtime_dir / "evidence" / "response-001.json"
    evidence_file.parent.mkdir()
    evidence_file.write_text('{"result": "tea"}', encoding="utf-8")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings)),
        base_url="http://testserver",
    ) as first_client:
        created = await first_client.post(
            "/api/v1/jobs",
            json={
                "type": "collection",
                "input": {"query": "tea"},
                "progress_current": 1,
                "progress_total": 3,
                "current_stage": "searching",
            },
        )
        assert created.status_code == 201
        job_id = created.json()["id"]

        claimed = await first_client.post(f"/api/v1/jobs/{job_id}/claim")
        assert claimed.status_code == 200
        logged = await first_client.post(
            f"/api/v1/jobs/{job_id}/logs",
            json={"level": "info", "message": "Collected first result."},
        )
        assert logged.status_code == 201
        evidence = await first_client.post(
            f"/api/v1/jobs/{job_id}/artifacts",
            json={
                "kind": "raw_response",
                "path": "evidence/response-001.json",
                "metadata": {"source_url": "https://example.test/search?q=tea"},
            },
        )
        assert evidence.status_code == 201

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings)),
        base_url="http://testserver",
    ) as restarted_client:
        response = await restarted_client.get(f"/api/v1/jobs/{job_id}")

    assert response.status_code == 200
    persisted = response.json()
    assert persisted["state"] == "running"
    assert persisted["progress_current"] == 1
    assert persisted["progress_total"] == 3
    assert persisted["current_stage"] == "searching"
    assert persisted["logs"] == [
        {"level": "info", "message": "Collected first result."}
    ]
    assert persisted["artifacts"] == [
        {
            "kind": "raw_response",
            "path": "evidence/response-001.json",
            "metadata": {"source_url": "https://example.test/search?q=tea"},
        }
    ]
