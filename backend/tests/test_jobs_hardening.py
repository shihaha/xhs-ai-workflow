from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import httpx
import pytest

from backend.app.db import Database
from backend.app.main import create_app
from backend.app.models.jobs import JobState
from backend.app.services.jobs import InvalidJobTransition, JobService
from backend.app.settings import Settings


def test_only_one_simultaneous_claim_can_win(tmp_path: Path) -> None:
    """Removing the conditional claim update would allow two workers to run one job."""
    database_path = tmp_path / "workbench.sqlite3"
    created = JobService(Database(database_path)).create(
        job_type="collection", input_data={}
    )
    barrier = Barrier(2)

    def claim_once() -> bool:
        service = JobService(Database(database_path))
        barrier.wait()
        try:
            service.claim(created.id)
        except InvalidJobTransition:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: claim_once(), range(2)))

    assert results.count(True) == 1
    assert JobService(Database(database_path)).get(created.id).state is JobState.running


def test_transition_to_running_starts_an_expiring_lease(tmp_path: Path) -> None:
    """A permitted transition into running must never be invisible to recovery."""
    service = JobService(Database(tmp_path / "workbench.sqlite3"))
    job = service.create(job_type="collection", input_data={})

    running = service.transition(job.id, JobState.running)

    assert running.started_at is not None
    assert running.lease_expires_at is not None


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_artifact_endpoint_rejects_paths_outside_existing_runtime_evidence(
    tmp_path: Path,
) -> None:
    """Accepting an absolute, traversal, or missing artifact path corrupts evidence claims."""
    runtime_dir = tmp_path / "runtime"
    settings = Settings(runtime_dir=runtime_dir, database_path=runtime_dir / "workbench.sqlite3")
    outside_file = tmp_path / "outside.json"
    outside_file.write_text("outside", encoding="utf-8")
    valid_file = runtime_dir / "evidence" / "response.json"
    valid_file.parent.mkdir()
    valid_file.write_text("{}", encoding="utf-8")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings)),
        base_url="http://testserver",
    ) as client:
        created = await client.post("/api/v1/jobs", json={"type": "collection"})
        job_id = created.json()["id"]
        for path in (str(outside_file), "../outside.json", "evidence/missing.json"):
            response = await client.post(
                f"/api/v1/jobs/{job_id}/artifacts",
                json={"kind": "raw_response", "path": path},
            )
            assert response.status_code == 422
        accepted = await client.post(
            f"/api/v1/jobs/{job_id}/artifacts",
            json={"kind": "raw_response", "path": "evidence/response.json"},
        )

    assert accepted.status_code == 201


@pytest.mark.anyio
async def test_corrupt_database_keeps_health_available_and_jobs_unavailable(
    tmp_path: Path,
) -> None:
    """An initialization failure must not turn a local database problem into an API 500."""
    runtime_dir = tmp_path / "runtime"
    database_path = runtime_dir / "workbench.sqlite3"
    settings = Settings(runtime_dir=runtime_dir, database_path=database_path)
    database_path.write_bytes(b"not a sqlite database")

    try:
        app = create_app(settings)
    except Exception as error:
        pytest.fail(f"application factory raised instead of exposing database unavailability: {error}")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        health = await client.get("/api/v1/health")
        jobs = await client.get("/api/v1/jobs")

    assert health.status_code == 200
    assert health.json()["checks"]["database"]["healthy"] is False
    assert jobs.status_code == 503


@pytest.mark.anyio
async def test_runtime_database_failure_becomes_503_and_degrades_health(
    tmp_path: Path,
) -> None:
    """A database that fails after startup must not become an unhandled jobs API 500."""
    runtime_dir = tmp_path / "runtime"
    database_path = runtime_dir / "workbench.sqlite3"
    app = create_app(Settings(runtime_dir=runtime_dir, database_path=database_path))
    app.state.database.close()
    database_path.write_bytes(b"not a sqlite database")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        jobs = await client.get("/api/v1/jobs")
        health = await client.get("/api/v1/health")

    assert jobs.status_code == 503
    assert health.json()["checks"]["database"]["healthy"] is False


@pytest.mark.anyio
async def test_lifespan_releases_database_file_handle(tmp_path: Path) -> None:
    """Failing to dispose the engine prevents Windows from replacing the local database."""
    runtime_dir = tmp_path / "runtime"
    database_path = runtime_dir / "workbench.sqlite3"
    app = create_app(Settings(runtime_dir=runtime_dir, database_path=database_path))

    async with app.router.lifespan_context(app):
        assert database_path.is_file()

    replacement_path = runtime_dir / "replacement.sqlite3"
    database_path.rename(replacement_path)
    assert replacement_path.is_file()
