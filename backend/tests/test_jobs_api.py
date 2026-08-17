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
            "producer": "external",
            "path": "evidence/response-001.json",
            "metadata": {"source_url": "https://example.test/search?q=tea"},
        }
    ]


@pytest.mark.parametrize(
    "reserved_job_type", ["android_shop_collection", "shop_collection"]
)
@pytest.mark.anyio
async def test_reserved_worker_jobs_are_readable_but_reject_public_claim_logs_and_artifacts(
    tmp_path: Path, reserved_job_type: str
) -> None:
    runtime_dir = tmp_path / reserved_job_type
    app = create_app(
        Settings(
            runtime_dir=runtime_dir,
            database_path=runtime_dir / "workbench.sqlite3",
        )
    )
    job = app.state.job_service.create(
        job_type=reserved_job_type,
        input_data={"account_user_id": "account-a"},
        progress_total=1,
        current_stage="device_pending",
    )
    evidence_file = runtime_dir / "evidence" / "worker-debug.json"
    evidence_file.parent.mkdir(parents=True)
    evidence_file.write_text("{}", encoding="utf-8")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        fetched = await client.get(f"/api/v1/jobs/{job.id}")
        listing = await client.get("/api/v1/jobs")
        claimed = await client.post(f"/api/v1/jobs/{job.id}/claim")
        logged = await client.post(
            f"/api/v1/jobs/{job.id}/logs",
            json={"level": "info", "message": "forged worker progress"},
        )
        attached = await client.post(
            f"/api/v1/jobs/{job.id}/artifacts",
            json={
                "kind": "worker_debug",
                "path": "evidence/worker-debug.json",
                "metadata": {"forged": True},
            },
        )

    assert fetched.status_code == 200
    assert any(item["id"] == job.id for item in listing.json())
    assert claimed.status_code == 422
    assert logged.status_code == 422
    assert attached.status_code == 422
    durable = app.state.job_service.get(job.id)
    assert durable.state is JobState.queued
    assert durable.logs == []
    assert durable.artifacts == []


@pytest.mark.parametrize(
    "reserved_job_type", ["android_shop_collection", "shop_collection"]
)
@pytest.mark.anyio
async def test_reserved_worker_public_transition_rejects_fake_success_and_cancel_field_injection(
    tmp_path: Path, reserved_job_type: str
) -> None:
    runtime_dir = tmp_path / reserved_job_type
    app = create_app(
        Settings(
            runtime_dir=runtime_dir,
            database_path=runtime_dir / "workbench.sqlite3",
        )
    )
    running = app.state.job_service.create(
        job_type=reserved_job_type, input_data={}, progress_total=1
    )
    app.state.job_service.claim(running.id)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        fake_success = await client.post(
            f"/api/v1/jobs/{running.id}/transition",
            json={
                "state": "succeeded",
                "progress_current": 1,
                "progress_total": 1,
                "current_stage": "shop_complete",
            },
        )
        injected_responses = []
        for injected in (
            {"progress_current": 1},
            {"progress_total": 1},
            {"current_stage": "shop_complete"},
            {"error_category": "forged_success"},
        ):
            cancellable = app.state.job_service.create(
                job_type=reserved_job_type, input_data={}, progress_total=1
            )
            injected_responses.append(
                await client.post(
                    f"/api/v1/jobs/{cancellable.id}/transition",
                    json={"state": "cancelled", **injected},
                )
            )

    assert fake_success.status_code == 422
    assert app.state.job_service.get(running.id).state is JobState.running
    assert [response.status_code for response in injected_responses] == [422] * 4


@pytest.mark.parametrize(
    ("reserved_job_type", "initial_state"),
    [
        (reserved_job_type, initial_state)
        for reserved_job_type in ("android_shop_collection", "shop_collection")
        for initial_state in (JobState.queued, JobState.running)
    ],
)
@pytest.mark.anyio
async def test_reserved_worker_public_pure_cancellation_sets_server_facts(
    tmp_path: Path, reserved_job_type: str, initial_state: JobState
) -> None:
    runtime_dir = tmp_path / f"{reserved_job_type}-{initial_state.value}"
    app = create_app(
        Settings(
            runtime_dir=runtime_dir,
            database_path=runtime_dir / "workbench.sqlite3",
        )
    )
    job = app.state.job_service.create(
        job_type=reserved_job_type,
        input_data={},
        progress_total=2,
        current_stage="device_pending",
    )
    if initial_state is JobState.running:
        app.state.job_service.claim(job.id)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        cancelled = await client.post(
            f"/api/v1/jobs/{job.id}/transition", json={"state": "cancelled"}
        )

    assert cancelled.status_code == 200
    body = cancelled.json()
    assert body["state"] == "cancelled"
    assert body["current_stage"] == "operator_cancelled"
    assert body["error_category"] == "operator_cancelled"
    assert body["progress_current"] == 0
    assert body["progress_total"] == 2
    assert body["artifacts"] == []
