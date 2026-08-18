from pathlib import Path

import httpx
import pytest

from backend.app.main import create_app
from backend.app.models.jobs import JobState
from backend.app.settings import Settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_fresh_runtime_starts_empty_without_demo_business_records(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "fresh-runtime"
    app = create_app(
        Settings(runtime_dir=runtime, database_path=runtime / "workbench.sqlite3")
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        responses = {
            "jobs": await client.get("/api/v1/jobs"),
            "accounts": await client.get("/api/v1/radar/accounts"),
            "analyses": await client.get("/api/v1/analyses"),
            "opportunities": await client.get("/api/v1/opportunities"),
            "products": await client.get("/api/v1/products"),
            "content": await client.get("/api/v1/content-items"),
            "cleanup": await client.get("/api/v1/artifact-cleanups"),
        }

    assert {name: response.status_code for name, response in responses.items()} == {
        name: 200 for name in responses
    }
    assert {name: response.json() for name, response in responses.items()} == {
        name: [] for name in responses
    }
    app.state.database.close()


def test_process_restart_exposes_expired_running_job_as_needs_human(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "restart-runtime"
    first = create_app(
        Settings(runtime_dir=runtime, database_path=runtime / "workbench.sqlite3")
    )
    created = first.state.job_service.create(job_type="operator_task", input_data={})
    first.state.job_service.claim(created.id, lease_seconds=0)
    first.state.database.close()

    restarted = create_app(
        Settings(runtime_dir=runtime, database_path=runtime / "workbench.sqlite3")
    )
    recovered = restarted.state.job_service.get(created.id)

    assert recovered.state is JobState.needs_human
    assert recovered.error_category == "worker_interrupted"
    assert recovered.logs[-1].message == "Running lease expired; human recovery required."
    restarted.state.database.close()
