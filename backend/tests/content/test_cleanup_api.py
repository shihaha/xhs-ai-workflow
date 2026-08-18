from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from backend.app.features.content.cleanup import ArtifactCleanupCandidate
from backend.app.main import create_app
from backend.app.settings import Settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _app(tmp_path: Path):
    runtime = tmp_path / "runtime"
    return create_app(
        Settings(runtime_dir=runtime, database_path=runtime / "workbench.sqlite3")
    )


def _enqueue_future_cleanup(app) -> str:
    payload = b"pending cleanup"
    relative_path = "orphaned/pending.bin"
    artifact = app.state.settings.runtime_dir / relative_path
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(payload)
    record = app.state.artifact_cleanup_service.enqueue(
        ArtifactCleanupCandidate(
            owner_type="material",
            owner_id=str(uuid4()),
            relative_path=relative_path,
            expected_sha256=sha256(payload).hexdigest(),
            expected_size_bytes=len(payload),
            reason="api_test",
            not_before=datetime.now(UTC) + timedelta(days=1),
        )
    )
    return record.id


@pytest.mark.anyio
async def test_cleanup_api_lists_and_gets_strict_persisted_records(tmp_path: Path) -> None:
    """Removing either read route would hide durable cleanup and human-action facts."""
    app = _app(tmp_path)
    cleanup_id = _enqueue_future_cleanup(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        listed = await client.get("/api/v1/artifact-cleanups")
        detail = await client.get(f"/api/v1/artifact-cleanups/{cleanup_id}")

    assert listed.status_code == 200
    assert [record["id"] for record in listed.json()] == [cleanup_id]
    assert detail.status_code == 200
    assert detail.json() == listed.json()[0]
    assert detail.json()["state"] == "pending"


@pytest.mark.anyio
async def test_cleanup_api_is_empty_and_read_only(tmp_path: Path) -> None:
    """Adding a force mutation route would bypass the approved fail-closed lifecycle."""
    app = _app(tmp_path)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        empty = await client.get("/api/v1/artifact-cleanups")
        responses = [
            await client.request(method, "/api/v1/artifact-cleanups")
            for method in ("POST", "PATCH", "DELETE")
        ]

    assert empty.status_code == 200
    assert empty.json() == []
    assert [response.status_code for response in responses] == [405, 405, 405]


@pytest.mark.anyio
async def test_cleanup_api_reports_unavailable_database_and_unknown_record(
    tmp_path: Path,
) -> None:
    """Unavailable storage and an absent record must never become false empty success."""
    app = _app(tmp_path)
    app.state.artifact_cleanup_service = None

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        unavailable_list = await client.get("/api/v1/artifact-cleanups")
        unavailable_detail = await client.get(
            f"/api/v1/artifact-cleanups/{uuid4()}"
        )

    assert unavailable_list.status_code == 503
    assert unavailable_detail.status_code == 503
    assert unavailable_list.json() == {"detail": "SQLite database is unavailable."}

    healthy = _app(tmp_path / "healthy")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=healthy), base_url="http://testserver"
    ) as client:
        unknown = await client.get(f"/api/v1/artifact-cleanups/{uuid4()}")
    assert unknown.status_code == 404
    assert unknown.json() == {
        "detail": "Artifact cleanup record does not exist."
    }
