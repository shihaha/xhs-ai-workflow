from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from threading import Event, Timer
from typing import Any

import httpx
import pytest

from backend.app.adapters.contracts import (
    CollectionItem,
    CollectionRequest,
    CollectionResult,
    DeviceHealth,
    RejectedCollectionItem,
)
from backend.app.db import Database
from backend.app.features.shops.service import ShopCollectionCreate, ShopCollectionService
from backend.app.main import create_app
from backend.app.models.jobs import JobState
from backend.app.services.jobs import JobService
from backend.app.settings import Settings


class _UnusedAdapter:
    selector_profile_version = "xhs-android-2026-08-v1"

    def health(self) -> DeviceHealth:
        return DeviceHealth(
            status="available",
            device_id="phone-1",
            detail="ready",
            raw_evidence={"device_id": "phone-1"},
        )

    def collect_shop(self, request: CollectionRequest) -> CollectionResult:
        raise AssertionError(f"invalid request reached the device adapter: {request}")


class _BlockingUrlAdapter(_UnusedAdapter):
    def __init__(self, release: Event) -> None:
        self.release = release
        self.started = Event()

    def collect_shop(self, request: CollectionRequest) -> CollectionResult:
        self.started.set()
        self.release.wait(timeout=1)
        return CollectionResult(
            status="succeeded",
            items=[
                CollectionItem(
                    id="product-a",
                    kind="shop_product",
                    source_url="https://xhslink.com/product-a",
                    raw_evidence={"clipboard": "https://xhslink.com/product-a"},
                )
            ],
            expected_count_known=True,
            expected_count=request.expected_count,
            succeeded_count=1,
            observed_count=1,
            missing_items=[],
            overflow_count=0,
            complete=True,
        )


class _StaticUrlAdapter(_BlockingUrlAdapter):
    def __init__(self) -> None:
        super().__init__(Event())
        self.release.set()


class _OverflowAdapter(_UnusedAdapter):
    def collect_shop(self, request: CollectionRequest) -> CollectionResult:
        return CollectionResult(
            status="failed",
            detail="discovered_count_exceeds_expected",
            items=[
                CollectionItem(
                    id="product-a",
                    kind="shop_product",
                    source_url="https://xhslink.com/product-a",
                    raw_evidence={"card": 1},
                )
            ],
            rejected_items=[
                RejectedCollectionItem(
                    reference="shop_card:2",
                    reason="duplicate_source_url",
                    raw_evidence={
                        "card": 2,
                        "source_url": "https://xhslink.com/product-a",
                    },
                )
            ],
            expected_count_known=True,
            expected_count=request.expected_count,
            succeeded_count=1,
            observed_count=2,
            missing_items=[],
            overflow_count=1,
            complete=False,
        )


def _write_verified_product_evidence(account_dir: Path, source_url: str) -> None:
    product_dir = account_dir / "01_product-a"
    images_dir = product_dir / "images"
    images_dir.mkdir(parents=True)
    image = images_dir / "00.webp"
    image.write_bytes(b"verified-product-image")
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    (product_dir / "detail.json").write_text(
        json.dumps(
            {
                "link": source_url,
                "image_manifest": [
                    {"file": "images/00.webp", "sha256": digest}
                ],
            }
        ),
        encoding="utf-8",
    )
    (account_dir / "collection.json").write_text(
        json.dumps(
            {
                "unique_product_link_count": 1,
                "products": [
                    {
                        "source_url": source_url,
                        "product_dir": "01_product-a",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _capturing_submitter(
    scheduled: list[tuple[Callable[..., Any], tuple[Any, ...]]],
) -> Callable[..., None]:
    def submit(action: Callable[..., Any], *args: Any) -> None:
        scheduled.append((action, args))

    return submit


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "overrides",
    [
        {"expected_count": True},
        {"expected_count": "1"},
        {"expected_count": " "},
        {"account_name": "   "},
        {"device_id": "   "},
    ],
    ids=[
        "boolean-count",
        "numeric-string-count",
        "whitespace-count",
        "whitespace-account-name",
        "whitespace-device-id",
    ],
)
async def test_shop_collection_request_rejects_coercion_and_whitespace(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    """Removing strict/trimmed fields would let ambiguous jobs reach a real phone."""
    runtime_dir = tmp_path / "runtime"
    app = create_app(
        Settings(
            runtime_dir=runtime_dir,
            database_path=runtime_dir / "workbench.sqlite3",
        )
    )
    adapter = _UnusedAdapter()
    app.state.android_adapter = adapter
    app.state.shop_service = ShopCollectionService(
        job_service=app.state.job_service,
        device_adapter=adapter,
    )
    payload: dict[str, Any] = {
        "account_user_id": "account-1",
        "account_name": "账号甲",
        "expected_count": 1,
    }
    payload.update(overrides)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post("/api/v1/shop-collections", json=payload)

    assert response.status_code == 422
    assert app.state.job_service.list() == []


@pytest.mark.anyio
async def test_shop_collection_rejects_verification_directory_outside_runtime(
    tmp_path: Path,
) -> None:
    """An external verifier path must be rejected before a job or device action exists."""
    runtime_dir = tmp_path / "runtime"
    outside = tmp_path / "outside"
    outside.mkdir()
    app = create_app(
        Settings(
            runtime_dir=runtime_dir,
            database_path=runtime_dir / "workbench.sqlite3",
        )
    )
    adapter = _UnusedAdapter()
    app.state.android_adapter = adapter
    app.state.shop_service = ShopCollectionService(
        job_service=app.state.job_service,
        device_adapter=adapter,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/api/v1/shop-collections",
            json={
                "account_user_id": "account-1",
                "account_name": "账号甲",
                "expected_count": 1,
                "verification_dir": "../outside",
            },
        )

    assert response.status_code == 422
    assert app.state.job_service.list() == []


@pytest.mark.anyio
async def test_shop_collection_post_returns_queued_job_before_device_work_finishes(
    tmp_path: Path,
) -> None:
    """Calling the device inline would hold the POST open until the adapter unblocks."""
    runtime_dir = tmp_path / "runtime"
    app = create_app(
        Settings(
            runtime_dir=runtime_dir,
            database_path=runtime_dir / "workbench.sqlite3",
        )
    )
    release = Event()
    adapter = _BlockingUrlAdapter(release)
    app.state.android_adapter = adapter
    app.state.shop_service = ShopCollectionService(
        job_service=app.state.job_service,
        device_adapter=adapter,
    )
    timer = Timer(0.3, release.set)
    timer.start()

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            started_at = time.monotonic()
            response = await client.post(
                "/api/v1/shop-collections",
                json={
                    "account_user_id": "account-1",
                    "account_name": "账号甲",
                    "expected_count": 1,
                },
            )
            elapsed = time.monotonic() - started_at

        assert response.status_code == 202
        assert elapsed < 0.2
        assert response.json()["status"] == "queued"
        job_id = response.json()["job_id"]
        assert adapter.started.wait(timeout=1)
        release.set()
        for _ in range(100):
            job = app.state.job_service.get(job_id)
            if job.state not in {JobState.queued, JobState.running}:
                break
            await asyncio.sleep(0.01)
        assert job.state is JobState.needs_human
        assert job.error_category == "product_evidence_verification_pending"
    finally:
        release.set()
        timer.cancel()
        close = getattr(app.state.shop_service, "close", None)
        if callable(close):
            close()


def test_verified_runtime_evidence_is_required_before_job_success(
    tmp_path: Path,
) -> None:
    """Deleting the production verifier call would promote a URL-only result to success."""
    runtime_dir = tmp_path / "runtime"
    account_dir = runtime_dir / "shop-evidence" / "account-a"
    _write_verified_product_evidence(
        account_dir, "https://xhslink.com/product-a"
    )
    jobs = JobService(
        Database(runtime_dir / "workbench.sqlite3"), runtime_dir=runtime_dir
    )
    scheduled: list[tuple[Callable[..., Any], tuple[Any, ...]]] = []
    service = ShopCollectionService(
        job_service=jobs,
        device_adapter=_StaticUrlAdapter(),
        submitter=_capturing_submitter(scheduled),
    )

    queued = service.enqueue(
        ShopCollectionCreate(
            account_user_id="account-1",
            account_name="账号甲",
            expected_count=1,
            verification_dir="shop-evidence/account-a",
        )
    )
    action, args = scheduled.pop()
    result = action(*args)

    assert result is not None
    assert result.status == "succeeded"
    assert result.detail is None
    assert result.complete is True
    job = jobs.get(queued.job_id)
    assert job.state is JobState.succeeded
    result_artifacts = [
        artifact for artifact in job.artifacts if artifact.kind == "shop_collection_result"
    ]
    assert len(result_artifacts) == 1
    persisted_path = runtime_dir / result_artifacts[0].path
    persisted = json.loads(persisted_path.read_text(encoding="utf-8"))
    assert persisted["status"] == "succeeded"
    assert persisted["items"][0]["source_url"] == "https://xhslink.com/product-a"


def test_verification_must_match_the_urls_observed_by_the_device(
    tmp_path: Path,
) -> None:
    """A complete but unrelated product directory must not certify collected URL facts."""
    runtime_dir = tmp_path / "runtime"
    account_dir = runtime_dir / "shop-evidence" / "account-a"
    _write_verified_product_evidence(
        account_dir, "https://xhslink.com/different-product"
    )
    jobs = JobService(
        Database(runtime_dir / "workbench.sqlite3"), runtime_dir=runtime_dir
    )
    scheduled: list[tuple[Callable[..., Any], tuple[Any, ...]]] = []
    service = ShopCollectionService(
        job_service=jobs,
        device_adapter=_StaticUrlAdapter(),
        submitter=_capturing_submitter(scheduled),
    )

    queued = service.enqueue(
        ShopCollectionCreate(
            account_user_id="account-1",
            account_name="账号甲",
            expected_count=1,
            verification_dir="shop-evidence/account-a",
        )
    )
    action, args = scheduled.pop()
    result = action(*args)

    assert result is not None
    assert result.status == "needs_human"
    assert result.detail == "product_evidence_verification_failed"
    assert result.complete is False
    assert result.verification is not None
    assert "collected_source_url_mismatch" in result.verification.issues
    assert jobs.get(queued.job_id).state is JobState.needs_human


def test_public_and_persisted_result_keep_all_accepted_and_rejected_observations(
    tmp_path: Path,
) -> None:
    """Slicing rejection rows to the expected deficit would erase overflow evidence."""
    runtime_dir = tmp_path / "runtime"
    jobs = JobService(
        Database(runtime_dir / "workbench.sqlite3"), runtime_dir=runtime_dir
    )
    scheduled: list[tuple[Callable[..., Any], tuple[Any, ...]]] = []
    service = ShopCollectionService(
        job_service=jobs,
        device_adapter=_OverflowAdapter(),
        submitter=_capturing_submitter(scheduled),
    )

    queued = service.enqueue(
        ShopCollectionCreate(
            account_user_id="account-1",
            account_name="账号甲",
            expected_count=1,
        )
    )
    action, args = scheduled.pop()
    result = action(*args)

    assert result is not None
    assert len(result.items) == 1
    assert result.rejected_count == 1
    assert len(result.rejected_items) == 1
    assert result.rejected_items[0].reason == "duplicate_source_url"
    assert result.rejected_items[0].raw_evidence["card"] == 2
    assert result.missing_count == 0
    assert result.missing_items == []
    assert result.overflow_count == 1
    artifact = next(
        item
        for item in jobs.get(queued.job_id).artifacts
        if item.kind == "shop_collection_result"
    )
    persisted = json.loads((runtime_dir / artifact.path).read_text(encoding="utf-8"))
    assert persisted["items"][0]["raw_evidence"] == {"card": 1}
    assert persisted["rejected_items"][0]["raw_evidence"]["card"] == 2
    assert persisted["overflow_count"] == 1
    assert artifact.metadata["result"]["rejected_items"] == persisted["rejected_items"]


@pytest.mark.anyio
async def test_app_lifespan_closes_shop_background_workers(tmp_path: Path) -> None:
    """Adding a thread pool without lifecycle shutdown leaks workers past app shutdown."""
    runtime_dir = tmp_path / "runtime"
    app = create_app(
        Settings(
            runtime_dir=runtime_dir,
            database_path=runtime_dir / "workbench.sqlite3",
        )
    )
    closed = False

    class ClosingProbe:
        def close(self) -> None:
            nonlocal closed
            closed = True

    app.state.shop_service = ClosingProbe()

    async with app.router.lifespan_context(app):
        assert closed is False

    assert closed is True


def test_service_checks_cancellation_before_persisting_a_final_success(
    tmp_path: Path,
) -> None:
    """A cancellation racing the adapter return must remain the durable final fact."""
    runtime_dir = tmp_path / "runtime"
    jobs = JobService(
        Database(runtime_dir / "workbench.sqlite3"), runtime_dir=runtime_dir
    )

    class CancelAtReturnAdapter(_StaticUrlAdapter):
        def collect_shop(self, request: CollectionRequest) -> CollectionResult:
            result = super().collect_shop(request)
            jobs.transition(str(request.parameters["job_id"]), JobState.cancelled)
            return result

    scheduled: list[tuple[Callable[..., Any], tuple[Any, ...]]] = []
    service = ShopCollectionService(
        job_service=jobs,
        device_adapter=CancelAtReturnAdapter(),
        submitter=_capturing_submitter(scheduled),
    )
    queued = service.enqueue(
        ShopCollectionCreate(
            account_user_id="account-1",
            account_name="账号甲",
            expected_count=1,
        )
    )
    action, args = scheduled.pop()

    result = action(*args)

    assert result is not None
    assert result.status == "failed"
    assert result.detail == "cancelled"
    assert result.complete is False
    job = jobs.get(queued.job_id)
    assert job.state is JobState.cancelled
    persisted = next(
        artifact.metadata["result"]
        for artifact in job.artifacts
        if artifact.kind == "shop_collection_result"
    )
    assert persisted["status"] == "failed"
    assert persisted["detail"] == "cancelled"


def test_unexpected_background_failure_does_not_leave_job_running(
    tmp_path: Path,
) -> None:
    """An unobserved Future exception must not strand a durable job as running."""
    runtime_dir = tmp_path / "runtime"
    jobs = JobService(
        Database(runtime_dir / "workbench.sqlite3"), runtime_dir=runtime_dir
    )

    class PersistenceFailureService(ShopCollectionService):
        def _persist_result(self, job_id: str, result: Any) -> None:
            _ = (job_id, result)
            raise OSError("simulated result write failure")

    scheduled: list[tuple[Callable[..., Any], tuple[Any, ...]]] = []
    service = PersistenceFailureService(
        job_service=jobs,
        device_adapter=_StaticUrlAdapter(),
        submitter=_capturing_submitter(scheduled),
    )
    queued = service.enqueue(
        ShopCollectionCreate(
            account_user_id="account-1",
            account_name="账号甲",
            expected_count=1,
        )
    )
    action, args = scheduled.pop()

    result = action(*args)

    assert result is None
    job = jobs.get(queued.job_id)
    assert job.state is JobState.failed
    assert job.error_category == "shop_collection_failed"
    assert any("simulated result write failure" in log.message for log in job.logs)
