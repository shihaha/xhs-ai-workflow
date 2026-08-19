from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from backend.app.api.jobs import router as jobs_router
from backend.app.features.media.api import router as media_router
from backend.app.features.media.service import ContentMediaService
from backend.app.features.media.worker import ContentMediaWorker
from backend.tests.media.test_generation_service import (
    FakeImageAdapter,
    UnusedVisionAdapter,
    _media_service,
)
from backend.tests.media.test_visual_service import _visual_service


class UnconfiguredImageAdapter(FakeImageAdapter):
    configured = False


def _api_app(service: ContentMediaService) -> tuple[FastAPI, ContentMediaWorker]:
    app = FastAPI()
    worker = ContentMediaWorker(service, poll_seconds=0.01)
    app.state.content_media_service = service
    app.state.content_media_worker = worker
    app.state.job_service = service.job_service
    app.include_router(media_router)
    app.include_router(jobs_router)
    return app, worker


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_generation_post_accepts_only_ids_and_exposes_read_only_run(
    tmp_path: Path,
) -> None:
    """Dropping strict request validation or durable list/detail reads breaks this flow."""

    service, item, original = _media_service(tmp_path)
    app, worker = _api_app(service)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        rejected = await client.post(
            f"/api/v1/content-items/{item.id}/image-generations",
            json={
                "expected_revision_id": item.current_revision.id,
                "image_plan_entry_id": original.id,
                "model": "attacker-model",
                "endpoint": "https://attacker.invalid",
                "path": "C:/outside.png",
            },
        )
        accepted = await client.post(
            f"/api/v1/content-items/{item.id}/image-generations",
            json={
                "expected_revision_id": item.current_revision.id,
                "image_plan_entry_id": original.id,
            },
        )
        assert accepted.status_code == 202
        run_id = accepted.json()["id"]
        assert worker.wait_idle(2)
        detail = await client.get(f"/api/v1/content-media-runs/{run_id}")
        listed = await client.get(f"/api/v1/content-items/{item.id}/media-runs")

    assert rejected.status_code == 422
    assert detail.status_code == 200
    assert detail.json()["id"] == run_id
    assert listed.status_code == 200
    assert [value["id"] for value in listed.json()] == [run_id]
    worker.close()


@pytest.mark.anyio
async def test_generic_jobs_api_cannot_mutate_or_attach_to_media_job(
    tmp_path: Path,
) -> None:
    """Removing media types from the reserved sets would let callers forge completion."""

    service, item, original = _media_service(tmp_path)
    app, worker = _api_app(service)
    run = service.submit_generation(
        item.id,
        expected_revision_id=item.current_revision.id,
        image_plan_entry_id=original.id,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        claim = await client.post(f"/api/v1/jobs/{run.job_id}/claim")
        transition = await client.post(
            f"/api/v1/jobs/{run.job_id}/transition", json={"state": "succeeded"}
        )
        artifact = await client.post(
            f"/api/v1/jobs/{run.job_id}/artifacts",
            json={"kind": "anything", "path": "anything.json"},
        )

    assert claim.status_code == 422
    assert transition.status_code == 422
    assert artifact.status_code == 422
    worker.close()


@pytest.mark.anyio
async def test_analysis_post_persists_advice_without_approval_mutation(
    tmp_path: Path,
) -> None:
    """Removing the analysis route or routing it through approval must fail this test."""

    service, item, original = _visual_service(tmp_path)
    before = service.content_service.get_content_item(item.id).status
    app, worker = _api_app(service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        accepted = await client.post(
            f"/api/v1/content-items/{item.id}/image-analyses",
            json={
                "expected_revision_id": item.current_revision.id,
                "material_ids": [original.id],
            },
        )
        assert accepted.status_code == 202
        assert worker.wait_idle(2)
        detail = await client.get(
            f"/api/v1/content-media-runs/{accepted.json()['id']}"
        )

    assert detail.json()["status"] == "succeeded"
    assert detail.json()["analysis_artifact_id"] is not None
    assert service.content_service.get_content_item(item.id).status == before == "review"
    worker.close()


@pytest.mark.anyio
async def test_media_api_reports_unconfigured_and_missing_without_creating_success(
    tmp_path: Path,
) -> None:
    """An unavailable provider or missing identity must not look accepted or successful."""

    base, item, original = _media_service(tmp_path)
    service = ContentMediaService(
        base.database,
        content_service=base.content_service,
        image_adapter=UnconfiguredImageAdapter(),
        vision_adapter=UnusedVisionAdapter(),
        runtime_dir=tmp_path,
    )
    app, worker = _api_app(service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        unavailable = await client.post(
            f"/api/v1/content-items/{item.id}/image-generations",
            json={
                "expected_revision_id": item.current_revision.id,
                "image_plan_entry_id": original.id,
            },
        )
        missing_run = await client.get(
            "/api/v1/content-media-runs/00000000-0000-4000-8000-000000000000"
        )
        missing_item = await client.get(
            "/api/v1/content-items/00000000-0000-4000-8000-000000000000/media-runs"
        )

    assert unavailable.status_code == 503
    assert missing_run.status_code == 404
    assert missing_item.status_code == 404
    assert service.list_runs(item.id) == []
    worker.close()
