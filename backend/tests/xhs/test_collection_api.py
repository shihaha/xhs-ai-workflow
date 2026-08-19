import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from backend.app.adapters.contracts import CollectionItem, CollectionResult
from backend.app.main import create_app
from backend.app.settings import Settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_account_collection_returns_202_and_durable_queued_job(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    app = create_app(Settings(runtime_dir=runtime_dir, database_path=runtime_dir / "db.sqlite3"))
    app.state.xhs_collection_service._submitter = lambda *_args: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post("/api/v1/accounts/user-1/collections", json={"expected_note_count": 2})
        job = await client.get(f"/api/v1/jobs/{response.json()['job_id']}")
    assert response.status_code == 202
    assert job.json()["state"] == "queued"
    assert job.json()["type"] == "xhs_account_collection"


@pytest.mark.anyio
async def test_collection_payloads_are_strict_and_database_unavailability_is_503(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    app = create_app(Settings(runtime_dir=runtime_dir, database_path=runtime_dir / "db.sqlite3"))
    app.state.xhs_collection_service._submitter = lambda *_args: None
    unavailable = create_app(Settings(runtime_dir=tmp_path / "bad", database_path=tmp_path / "bad"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        extra = await client.post("/api/v1/accounts/user-1/collections", json={"expected_note_count": 1, "executable": "evil"})
        bool_count = await client.post("/api/v1/notes/search-collections", json={"keyword": "收纳", "expected_count": True})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=unavailable), base_url="http://testserver") as client:
        unavailable_response = await client.post("/api/v1/accounts/user-1/collections", json={"expected_note_count": 1})
    assert extra.status_code == 422
    assert bool_count.status_code == 422
    assert unavailable_response.status_code == 503


@pytest.mark.anyio
async def test_missing_normalized_reads_are_404(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    app = create_app(Settings(runtime_dir=runtime_dir, database_path=runtime_dir / "db.sqlite3"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        profile = await client.get("/api/v1/accounts/missing/profile")
        notes = await client.get("/api/v1/accounts/missing/notes")
        search = await client.get("/api/v1/note-search-results", params={"job_id": "missing"})
    assert profile.status_code == 404
    assert notes.status_code == 404
    assert search.status_code == 404


@pytest.mark.anyio
async def test_normalized_read_apis_never_return_raw_evidence(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    app = create_app(Settings(runtime_dir=runtime_dir, database_path=runtime_dir / "db.sqlite3"))
    app.state.xhs_collection_service._submitter = lambda *_args: None

    class Adapter:
        def fetch_account(self, request):
            user_id = request.parameters["user_id"]
            return CollectionResult(
                status="succeeded",
                items=[
                    CollectionItem(
                        id=f"profile:{user_id}", kind="profile",
                        source_url=f"https://www.xiaohongshu.com/user/profile/{user_id}",
                        raw_evidence={"profile": {"user_id": user_id}},
                        data={"user_id": user_id, "nickname": "Alice"},
                    ),
                    CollectionItem(
                        id="note:n1", kind="note",
                        source_url="https://www.xiaohongshu.com/explore/n1",
                        raw_evidence={"row": {"note_id": "n1", "user_id": user_id}},
                        data={"note_id": "n1", "user_id": user_id, "title": "First"},
                    ),
                ],
                expected_count_known=True, expected_count=2, succeeded_count=2,
                observed_count=2, overflow_count=0, complete=True,
            )

    app.state.xhs_collection_service.adapter = Adapter()
    queued = app.state.xhs_collection_service.submit_account("user-1", 1)
    app.state.xhs_collection_service.execute(queued.id)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        profile = await client.get("/api/v1/accounts/user-1/profile")
        notes = await client.get("/api/v1/accounts/user-1/notes")

    assert profile.status_code == notes.status_code == 200
    assert profile.json()["nickname"] == "Alice"
    assert notes.json()[0]["note_id"] == "n1"
    assert "raw_evidence" not in profile.json()
    assert "raw_evidence" not in notes.json()[0]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("table", "column", "key_column", "key", "payload", "endpoint"),
    [
        (
            "xhs_account_profiles",
            "public_stats_json",
            "user_id",
            "counter-user",
            {"followers_count": 1.0},
            "/api/v1/accounts/counter-user/profile",
        ),
        (
            "xhs_account_notes",
            "public_interactions_json",
            "note_id",
            "counter-note",
            {"liked_count": True},
            "/api/v1/accounts/counter-user/notes",
        ),
    ],
)
async def test_public_counter_json_type_drift_is_never_returned_by_api(
    tmp_path: Path,
    table: str,
    column: str,
    key_column: str,
    key: str,
    payload: dict[str, object],
    endpoint: str,
) -> None:
    runtime_dir = tmp_path / "runtime"
    app = create_app(Settings(
        runtime_dir=runtime_dir,
        database_path=runtime_dir / "db.sqlite3",
    ))
    app.state.xhs_collection_service._submitter = lambda *_args: None

    class Adapter:
        def fetch_account(self, request):
            user_id = request.parameters["user_id"]
            return CollectionResult(
                status="succeeded",
                items=[
                    CollectionItem(
                        id=f"profile:{user_id}",
                        kind="profile",
                        source_url=(
                            f"https://www.xiaohongshu.com/user/profile/{user_id}"
                        ),
                        raw_evidence={"profile": {"user_id": user_id}},
                        data={
                            "user_id": user_id,
                            "nickname": "Counter",
                            "followers_count": 1,
                        },
                    ),
                    CollectionItem(
                        id="note:counter-note",
                        kind="note",
                        source_url=(
                            "https://www.xiaohongshu.com/explore/counter-note"
                        ),
                        raw_evidence={
                            "row": {
                                "note_id": "counter-note",
                                "user_id": user_id,
                            }
                        },
                        data={
                            "note_id": "counter-note",
                            "user_id": user_id,
                            "title": "Counter",
                            "liked_count": 1,
                        },
                    ),
                ],
                expected_count_known=True,
                expected_count=2,
                succeeded_count=2,
                observed_count=2,
                overflow_count=0,
                complete=True,
            )

    service = app.state.xhs_collection_service
    service.adapter = Adapter()
    queued = service.submit_account("counter-user", 1)
    service.execute(queued.id)
    with service.database.engine.begin() as connection:
        connection.execute(text("DROP TRIGGER ck_xhs_profile_immutable_update"))
        connection.execute(text("DROP TRIGGER ck_xhs_note_immutable_update"))
        connection.execute(
            text(
                f"UPDATE {table} SET {column}=:value "
                f"WHERE {key_column}=:key"
            ),
            {
                "value": json.dumps(payload),
                "key": key,
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get(endpoint)

    assert response.status_code == 404
    service.database.close()
