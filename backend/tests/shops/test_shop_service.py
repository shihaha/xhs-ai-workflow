from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Timer, get_ident
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
from backend.app.services.jobs import InvalidJobTransition, JobService
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
                ),
                CollectionItem(
                    id="product-b",
                    kind="shop_product",
                    source_url="https://xhslink.com/product-b",
                    raw_evidence={"card": 3},
                ),
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
            succeeded_count=2,
            observed_count=3,
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
        {"selector_profile_version": "   "},
    ],
    ids=[
        "boolean-count",
        "numeric-string-count",
        "whitespace-count",
        "whitespace-account-name",
        "whitespace-device-id",
        "whitespace-selector-profile",
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


def test_url_only_collection_reports_collected_links_but_zero_verified_products(
    tmp_path: Path,
) -> None:
    """Reusing URL collection counts as deep N/N success would publish unverified progress."""
    runtime_dir = tmp_path / "runtime"
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
        )
    )
    action, args = scheduled.pop()

    result = action(*args)

    assert result is not None
    assert result.status == "needs_human"
    assert result.detail == "product_evidence_verification_pending"
    assert result.collected_count == 1
    assert len(result.items) == 1
    assert result.succeeded_count == 0
    assert result.missing_count == 1
    assert result.missing_items[0].reason == "product_evidence_verification_pending"
    assert result.complete is False
    job = jobs.get(queued.job_id)
    assert job.type == "android_shop_collection"
    assert job.progress_current == 0
    assert job.progress_total == 1


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
    assert result.collected_count == 1
    assert result.succeeded_count == 1
    assert result.missing_count == 0
    assert result.complete is True
    job = jobs.get(queued.job_id)
    assert job.state is JobState.succeeded
    assert job.progress_current == 1
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
    assert result.collected_count == 1
    assert result.succeeded_count == 0
    assert result.missing_count == 1
    assert result.missing_items[0].reason == "collected_source_url_mismatch"
    job = jobs.get(queued.job_id)
    assert job.state is JobState.needs_human
    assert job.progress_current == 0


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
    assert len(result.items) == 2
    assert result.discovered_count == 2
    assert result.raw_observation_count == 3
    assert result.duplicate_observation_count == 1
    assert result.rejected_count == 1
    assert len(result.rejected_items) == 1
    assert result.rejected_items[0].reason == "duplicate_source_url"
    assert result.rejected_items[0].raw_evidence["card"] == 2
    assert result.collected_count == 2
    assert result.succeeded_count == 0
    assert result.missing_count == 1
    assert result.missing_items[0].reason == "discovered_count_exceeds_expected"
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
    assert not any(
        artifact.kind == "shop_collection_result" for artifact in job.artifacts
    )
    result_dir = runtime_dir / "evidence" / "shops" / queued.job_id
    assert not list(result_dir.glob("*.json"))
    assert not list(result_dir.glob("*.tmp"))


@pytest.mark.parametrize("race_boundary", ["persist", "finalize"])
def test_cancellation_wins_atomically_against_shop_result_finalization(
    tmp_path: Path, race_boundary: str
) -> None:
    """A cancellation winning either boundary must leave no succeeded result artifact."""
    runtime_dir = tmp_path / "runtime"
    account_dir = runtime_dir / "shop-evidence" / "account-a"
    _write_verified_product_evidence(
        account_dir, "https://xhslink.com/product-a"
    )
    jobs = JobService(
        Database(runtime_dir / "workbench.sqlite3"), runtime_dir=runtime_dir
    )
    boundary_entered = Event()
    cancellation_committed = Event()

    class PausedFinalizationService(ShopCollectionService):
        def _pause(self, boundary: str) -> None:
            if race_boundary != boundary:
                return
            boundary_entered.set()
            assert cancellation_committed.wait(timeout=1)

        def _persist_result(self, job_id: str, result: Any) -> Any:
            self._pause("persist")
            return super()._persist_result(job_id, result)

        def _finalize(self, job_id: str, result: Any, *pending: Any) -> Any:
            self._pause("finalize")
            return super()._finalize(job_id, result, *pending)

    scheduled: list[tuple[Callable[..., Any], tuple[Any, ...]]] = []
    service = PausedFinalizationService(
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

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(action, *args)
        assert boundary_entered.wait(timeout=1)
        jobs.transition(
            queued.job_id,
            JobState.cancelled,
            current_stage="cancelled_by_test",
            error_category="cancelled",
        )
        cancellation_committed.set()
        result = future.result(timeout=2)

    assert result is not None
    assert result.status == "failed"
    assert result.detail == "cancelled"
    job = jobs.get(queued.job_id)
    assert job.state is JobState.cancelled
    assert not any(
        artifact.kind == "shop_collection_result" for artifact in job.artifacts
    )
    result_dir = runtime_dir / "evidence" / "shops" / queued.job_id
    assert not list(result_dir.glob("*.json"))
    assert not list(result_dir.glob("*.tmp"))


def test_atomic_finalization_cannot_be_overwritten_by_a_stale_cancellation(
    tmp_path: Path,
) -> None:
    """An ORM cancellation that read `running` first must lose after finalization commits."""
    runtime_dir = tmp_path / "runtime"
    cancellation_read_running = Event()
    allow_cancellation_update = Event()

    class PausedCancellationJobService(JobService):
        cancellation_thread_id: int | None = None
        paused = False

        def _record(self, session: Any, job_id: str) -> Any:
            record = super()._record(session, job_id)
            if (
                get_ident() == self.cancellation_thread_id
                and not self.paused
                and JobState(record.state) is JobState.running
            ):
                self.paused = True
                cancellation_read_running.set()
                assert allow_cancellation_update.wait(timeout=1)
            return record

    jobs = PausedCancellationJobService(
        Database(runtime_dir / "workbench.sqlite3"), runtime_dir=runtime_dir
    )
    job = jobs.create(
        job_type="android_shop_collection", input_data={}, progress_total=1
    )
    jobs.claim(job.id)
    result_dir = runtime_dir / "evidence" / "shops" / job.id
    result_dir.mkdir(parents=True)
    temp_relative = Path("evidence") / "shops" / job.id / ".result.tmp"
    final_relative = Path("evidence") / "shops" / job.id / "result.json"
    (runtime_dir / temp_relative).write_text('{"status":"succeeded"}\n', encoding="utf-8")

    def cancel_from_stale_read() -> str:
        jobs.cancellation_thread_id = get_ident()
        try:
            jobs.transition(job.id, JobState.cancelled)
        except InvalidJobTransition:
            return "lost"
        return "won"

    with ThreadPoolExecutor(max_workers=1) as executor:
        cancellation = executor.submit(cancel_from_stale_read)
        assert cancellation_read_running.wait(timeout=1)
        finalized = jobs.finalize_running_with_artifact(
            job.id,
            state=JobState.succeeded,
            progress_current=1,
            progress_total=1,
            current_stage="shop_complete",
            error_category=None,
            kind="shop_collection_result",
            temp_path=temp_relative.as_posix(),
            path=final_relative.as_posix(),
            metadata={"result": {"status": "succeeded"}},
        )
        allow_cancellation_update.set()
        cancellation_outcome = cancellation.result(timeout=2)

    assert finalized is not None
    assert cancellation_outcome == "lost"
    durable = jobs.get(job.id)
    assert durable.state is JobState.succeeded
    assert durable.progress_current == 1
    assert len(durable.artifacts) == 1
    assert durable.artifacts[0].path == final_relative.as_posix()
    assert (runtime_dir / final_relative).is_file()
    assert not (runtime_dir / temp_relative).exists()


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


def test_service_startup_recovers_prior_android_shop_workers_as_needs_human(
    tmp_path: Path,
) -> None:
    """Physical navigation from an earlier process must never remain queued or running."""
    runtime_dir = tmp_path / "runtime"
    jobs = JobService(
        Database(runtime_dir / "workbench.sqlite3"), runtime_dir=runtime_dir
    )
    queued = jobs.create(
        job_type="android_shop_collection", input_data={}, progress_total=1
    )
    running = jobs.create(
        job_type="android_shop_collection", input_data={}, progress_total=2
    )
    jobs.claim(running.id)
    unrelated = jobs.create(job_type="radar_collection", input_data={})

    service = ShopCollectionService(
        job_service=jobs,
        device_adapter=_UnusedAdapter(),
        submitter=_capturing_submitter([]),
    )

    for job_id in (queued.id, running.id):
        recovered = jobs.get(job_id)
        assert recovered.state is JobState.needs_human
        assert recovered.current_stage == "worker_restart_required"
        assert recovered.error_category == "worker_restart_required"
        assert any("worker restart" in log.message.casefold() for log in recovered.logs)
    assert jobs.get(unrelated.id).state is JobState.queued
    service.close()


def test_closed_service_rejects_enqueue_before_creating_a_job(tmp_path: Path) -> None:
    """Shutdown must stop admission before a durable job can become unschedulable."""
    runtime_dir = tmp_path / "runtime"
    jobs = JobService(
        Database(runtime_dir / "workbench.sqlite3"), runtime_dir=runtime_dir
    )
    service = ShopCollectionService(
        job_service=jobs,
        device_adapter=_UnusedAdapter(),
    )
    service.close()

    with pytest.raises(RuntimeError, match="closed"):
        service.enqueue(
            ShopCollectionCreate(
                account_user_id="account-1",
                account_name="账号甲",
                expected_count=1,
            )
        )

    assert jobs.list() == []


@pytest.mark.anyio
async def test_closed_shop_collection_api_returns_service_unavailable(
    tmp_path: Path,
) -> None:
    """A request arriving after admission closes must get a factual retriable response."""
    runtime_dir = tmp_path / "runtime"
    app = create_app(
        Settings(
            runtime_dir=runtime_dir,
            database_path=runtime_dir / "workbench.sqlite3",
        )
    )
    app.state.shop_service.close()

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
            },
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "Shop collection service is closed."
    assert app.state.job_service.list() == []


@pytest.mark.anyio
async def test_lifespan_shutdown_is_nonblocking_and_cancels_running_and_queued_work(
    tmp_path: Path,
) -> None:
    """Waiting synchronously for a blocked phone worker would hang application shutdown."""
    runtime_dir = tmp_path / "runtime"
    app = create_app(
        Settings(
            runtime_dir=runtime_dir,
            database_path=runtime_dir / "workbench.sqlite3",
        )
    )
    original_service = app.state.shop_service
    assert original_service is not None
    original_service.close()
    release = Event()
    first_started = Event()
    first_finished = Event()

    class ShutdownAwareBlockingAdapter(_BlockingUrlAdapter):
        def __init__(self) -> None:
            super().__init__(release)
            self.calls = 0
            self.cancel_callback: Callable[[], bool] = lambda: False

        def collect_shop(self, request: CollectionRequest) -> CollectionResult:
            self.calls += 1
            callback = request.parameters.get("is_cancelled")
            if callable(callback):
                self.cancel_callback = callback
            first_started.set()
            try:
                return super().collect_shop(request)
            finally:
                first_finished.set()

    adapter = ShutdownAwareBlockingAdapter()
    service = ShopCollectionService(
        job_service=app.state.job_service,
        device_adapter=adapter,
        max_workers=1,
    )
    app.state.android_adapter = adapter
    app.state.shop_service = service
    timer = Timer(0.35, release.set)
    timer.start()
    first_id = ""
    second_id = ""
    started_at = 0.0
    try:
        async with app.router.lifespan_context(app):
            first_id = service.enqueue(
                ShopCollectionCreate(
                    account_user_id="account-1",
                    account_name="账号甲",
                    expected_count=1,
                )
            ).job_id
            assert first_started.wait(timeout=1)
            second_id = service.enqueue(
                ShopCollectionCreate(
                    account_user_id="account-2",
                    account_name="账号乙",
                    expected_count=1,
                )
            ).job_id
            started_at = time.monotonic()
        elapsed = time.monotonic() - started_at

        assert elapsed < 0.2
        assert adapter.cancel_callback() is True
        assert app.state.job_service.get(first_id).state is JobState.cancelled
        assert app.state.job_service.get(second_id).state is JobState.cancelled
        release.set()
        assert first_finished.wait(timeout=1)
        await asyncio.sleep(0.05)
        assert adapter.calls == 1
    finally:
        release.set()
        timer.cancel()
