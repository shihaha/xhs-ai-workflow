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
    ModelResult,
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


class _CaptureExpectedAdapter(_UnusedAdapter):
    def __init__(
        self,
        jobs: JobService | None = None,
        *,
        titles: list[str] | None = None,
    ) -> None:
        self.requests: list[CollectionRequest] = []
        self.jobs = jobs
        self.titles = titles or ["连衣裙", "衬衫", "半身裙"]

    def collect_shop(self, request: CollectionRequest) -> CollectionResult:
        self.requests.append(request)
        expected = request.expected_count or 0
        job_id = str(request.parameters["job_id"])
        raw_evidence: list[dict[str, Any]] = []
        for index in range(expected):
            transition = f"product_{index + 1}_detail"
            screenshot = f"real-detail-screen-{index}".encode()
            digest = hashlib.sha256(screenshot).hexdigest()
            relative = f"evidence/android/{job_id}/{transition}.png"
            if self.jobs is not None:
                absolute = self.jobs.runtime_dir / relative
                absolute.parent.mkdir(parents=True, exist_ok=True)
                absolute.write_bytes(screenshot)
                self.jobs.attach_artifact(
                    job_id,
                    kind="android_screenshot",
                    path=relative,
                    metadata={"transition": transition, "sha256": digest},
                )
            raw_evidence.append(
                {
                    "title": self.titles[index % len(self.titles)],
                    "detail_screen": {
                        "transition": transition,
                        "screenshot_sha256": digest,
                        "artifacts": [relative],
                    },
                }
            )
        return CollectionResult(
            status="succeeded",
            items=[
                CollectionItem(
                    id=f"product-{index}",
                    kind="shop_product",
                    source_url=f"https://xhslink.com/product-{index}",
                    raw_evidence=raw_evidence[index],
                )
                for index in range(expected)
            ],
            expected_count_known=True,
            expected_count=expected,
            succeeded_count=expected,
            observed_count=expected,
            missing_items=[],
            overflow_count=0,
            complete=True,
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


def test_test_override_processes_three_products_even_when_eighteen_were_discovered(
    tmp_path: Path,
) -> None:
    """Using the discovery count as expected_count would deep-read all 18 products."""
    runtime_dir = tmp_path / "runtime"
    jobs = JobService(
        Database(runtime_dir / "workbench.sqlite3"), runtime_dir=runtime_dir
    )
    adapter = _CaptureExpectedAdapter(jobs)
    scheduled: list[tuple[Callable[..., Any], tuple[Any, ...]]] = []
    service = ShopCollectionService(
        job_service=jobs,
        device_adapter=adapter,
        submitter=_capturing_submitter(scheduled),
    )
    queued = service.enqueue(
        ShopCollectionCreate(
            account_user_id="account-1",
            account_name="账号甲",
            expected_count=18,
            available_count_observed=18,
            collection_mode="bounded_sample",
            product_sample_limit=3,
            test_override=True,
            test_override_reason="保留服装店完成一次有界E2E验证",
        )
    )

    action, args = scheduled.pop()
    result = action(*args)

    assert adapter.requests[0].expected_count == 3
    assert result is not None
    assert result.status == "succeeded"
    assert result.expected_count == 3
    assert result.sample_complete is True
    assert result.shop_complete is False
    assert result.complete is False
    assert result.collection_mode == "bounded_sample"
    assert result.available_count_observed == 18
    assert result.scope_classification == "out_of_scope_physical"
    assert result.scope_reason == "physical_goods_detected"
    job = jobs.get(queued.job_id)
    assert job.progress_current == 3
    assert job.progress_total == 3
    sample_manifest = (
        runtime_dir / "evidence" / "shops" / queued.job_id / "sample-products" / "manifest.json"
    )
    assert sample_manifest.is_file()
    manifest = json.loads(sample_manifest.read_text(encoding="utf-8"))
    assert len(manifest["products"]) == 3
    for product in manifest["products"]:
        image = sample_manifest.parent / product["image"]
        assert image.is_file()
        assert hashlib.sha256(image.read_bytes()).hexdigest() == product["sha256"]
    assert result.sample_manifest_path == sample_manifest.relative_to(runtime_dir).as_posix()
    assert result.sample_manifest_sha256 == hashlib.sha256(
        sample_manifest.read_bytes()
    ).hexdigest()
    sample_collection = sample_manifest.parent / "collection.json"
    assert result.sample_collection_path == sample_collection.relative_to(
        runtime_dir
    ).as_posix()
    assert result.sample_collection_sha256 == hashlib.sha256(
        sample_collection.read_bytes()
    ).hexdigest()
    artifact = next(
        artifact
        for artifact in jobs.get(queued.job_id).artifacts
        if artifact.kind == "shop_collection_result"
    )
    persisted = artifact.metadata["result"]
    assert persisted["sample_manifest_path"] == result.sample_manifest_path
    assert persisted["sample_manifest_sha256"] == result.sample_manifest_sha256
    sample_manifest.write_text("{}\n", encoding="utf-8")
    assert hashlib.sha256(sample_manifest.read_bytes()).hexdigest() != persisted[
        "sample_manifest_sha256"
    ]


def test_evidence_sample_persists_the_first_three_products_without_claiming_full_shop(
    tmp_path: Path,
) -> None:
    """A large in-scope shop must produce a trusted three-item sample, not fake N/N."""
    runtime_dir = tmp_path / "runtime"
    jobs = JobService(
        Database(runtime_dir / "workbench.sqlite3"), runtime_dir=runtime_dir
    )
    adapter = _CaptureExpectedAdapter(
        jobs,
        titles=["电子版兑换券一", "电子版兑换券二", "电子版兑换券三"],
    )
    scheduled: list[tuple[Callable[..., Any], tuple[Any, ...]]] = []
    service = ShopCollectionService(
        job_service=jobs,
        device_adapter=adapter,
        submitter=_capturing_submitter(scheduled),
    )
    gate = service.enqueue(
        ShopCollectionCreate(
            account_user_id="account-1",
            account_name="账号甲",
            collection_mode="preflight",
            available_count_observed=18,
        )
    )
    gate_action, gate_args = scheduled.pop()
    gate_result = gate_action(*gate_args)
    assert gate_result is not None
    assert gate_result.scope_classification == "in_scope"

    queued = service.enqueue(
        ShopCollectionCreate(
            account_user_id="account-1",
            account_name="账号甲",
            expected_count=18,
            available_count_observed=18,
            collection_mode="evidence_sample",
            product_sample_limit=3,
            scope_gate_job_id=gate.job_id,
        )
    )
    action, args = scheduled.pop()
    result = action(*args)

    assert adapter.requests[-1].expected_count == 3
    assert result is not None
    assert result.status == "succeeded"
    assert result.collection_mode == "evidence_sample"
    assert result.expected_count == 3
    assert result.succeeded_count == 3
    assert result.sample_complete is True
    assert result.shop_complete is False
    assert result.complete is False
    job = jobs.get(queued.job_id)
    assert job.state is JobState.succeeded
    assert job.progress_current == 3
    assert job.progress_total == 3
    assert job.current_stage == "shop_evidence_sample_complete"
    artifact = next(
        item for item in job.artifacts if item.kind == "shop_collection_result"
    )
    assert artifact.metadata["result"]["collection_mode"] == "evidence_sample"
    assert artifact.metadata["result"]["sample_complete"] is True
    assert artifact.metadata["result"]["shop_complete"] is False


@pytest.mark.parametrize(
    ("profile", "titles", "expected"),
    [
        ("原创服装店", ["连衣裙", "衬衫", "半身裙"], "out_of_scope_physical"),
        ("数字资料与模板", ["运营教程", "测试清单", "网站模板"], "in_scope"),
        ("个人工作室", ["方案一", "方案二", "方案三"], "needs_human"),
    ],
)
def test_scope_gate_classifies_only_three_representative_products(
    tmp_path: Path,
    profile: str,
    titles: list[str],
    expected: str,
) -> None:
    """Failing to stop at three would turn the cheap preflight gate into deep collection."""
    from backend.app.features.shops.scope import ShopScopeEvidence, classify_shop_scope

    evidence = ShopScopeEvidence(
        shop_profile=profile,
        representative_product_titles=titles + ["第四件不得查看"],
    )

    result = classify_shop_scope(evidence)

    assert result.classification == expected
    assert result.representative_product_count == 3
    assert result.deep_collection_allowed is (expected == "in_scope")


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("repor锐珀尔·猫猫枕", "out_of_scope_physical"),
        ("repor锐珀尔·筋膜枪", "out_of_scope_physical"),
        ("腰部按摩器", "out_of_scope_physical"),
        ("新款连衣裙", "out_of_scope_physical"),
        ("补水护肤品", "out_of_scope_physical"),
        ("四季猫窝", "out_of_scope_physical"),
        ("防摔手机壳", "out_of_scope_physical"),
        ("蓝牙耳机", "out_of_scope_physical"),
        ("儿童玩具", "out_of_scope_physical"),
        ("学生文具", "out_of_scope_physical"),
        ("班主任开学PPT模板", "in_scope"),
        ("Excel记账模板", "in_scope"),
        ("PDF学习资料", "in_scope"),
        ("Canva模板文件", "in_scope"),
        ("网盘资料包", "in_scope"),
        ("网站源码", "in_scope"),
        ("小程序源码", "in_scope"),
        ("本地软件工具", "in_scope"),
        ("数字教程 无需物流", "in_scope"),
        ("交付形态未知的神秘方案", "needs_human"),
        ("PDF模板 实物发货", "needs_human"),
        ("商品信息不足", "needs_human"),
    ],
)
def test_scope_gate_uses_bounded_delivery_evidence(
    title: str, expected: str
) -> None:
    from backend.app.features.shops.scope import ShopScopeEvidence, classify_shop_scope

    result = classify_shop_scope(
        ShopScopeEvidence(
            shop_profile="android_preflight_observation",
            representative_product_titles=[title],
            evidence_refs=["discovery:1"],
        )
    )

    assert result.classification == expected


def test_scope_gate_uses_the_existing_text_model_only_for_ambiguous_evidence() -> None:
    from backend.app.features.shops.scope import ShopScopeEvidence, classify_shop_scope

    class ScopeModel:
        def __init__(self) -> None:
            self.calls = 0

        def generate_structured(self, request: Any, schema: Any) -> ModelResult:
            self.calls += 1
            assert request.evidence_ids == ["discovery:1"]
            output = schema.model_validate(
                {
                    "classification": "physical",
                    "reason": "The cited item requires parcel delivery.",
                    "evidence_refs": ["discovery:1"],
                }
            )
            return ModelResult(
                model="controlled-scope-model",
                output=output.model_dump(mode="json"),
                raw_evidence={"provider": "controlled"},
            )

    model = ScopeModel()
    result = classify_shop_scope(
        ShopScopeEvidence(
            shop_profile="android_preflight_observation",
            representative_product_titles=["交付形态未知商品"],
            evidence_refs=["discovery:1"],
        ),
        model_adapter=model,
    )

    assert model.calls == 1
    assert result.classification == "out_of_scope_physical"
    assert result.decision_source == "model"
    assert result.evidence_refs == ["discovery:1"]


def test_persisted_account_scope_skips_android_after_restart(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    database = Database(runtime_dir / "workbench.sqlite3")
    jobs = JobService(database, runtime_dir=runtime_dir)
    source = jobs.create(
        job_type="android_shop_collection",
        input_data={"account_user_id": "account-physical"},
    )
    evidence_path = Path("evidence/android/source/product.json")
    (runtime_dir / evidence_path).parent.mkdir(parents=True, exist_ok=True)
    (runtime_dir / evidence_path).write_text("{}\n", encoding="utf-8")
    jobs.attach_artifact(
        source.id,
        kind="android_shop_product_discovery",
        path=evidence_path.as_posix(),
        metadata={"sha256": hashlib.sha256(b"{}\n").hexdigest()},
    )
    first = ShopCollectionService(
        job_service=jobs,
        device_adapter=_UnusedAdapter(),
        submitter=lambda *_: pytest.fail("out-of-scope account reached Android"),
    )

    recorded = first.record_account_scope_decision(
        account_user_id="account-physical",
        classification="out_of_scope_physical",
        decision_source="human",
        reason="human_confirmed_physical",
        evidence_refs=[evidence_path.as_posix()],
    )

    restarted = ShopCollectionService(
        job_service=JobService(database, runtime_dir=runtime_dir),
        device_adapter=_UnusedAdapter(),
        submitter=lambda *_: pytest.fail("out-of-scope account reached Android"),
    )
    assert restarted.get_account_scope_decision("account-physical") == recorded
    with pytest.raises(ValueError, match="out_of_scope_physical"):
        restarted.enqueue(
            ShopCollectionCreate(
                account_user_id="account-physical",
                account_name="实体账号",
            )
        )


def test_default_collection_is_real_android_preflight_capped_at_three(
    tmp_path: Path,
) -> None:
    """Defaulting to a legacy full read would bypass the required cheap scope gate."""
    runtime_dir = tmp_path / "runtime"
    jobs = JobService(
        Database(runtime_dir / "workbench.sqlite3"), runtime_dir=runtime_dir
    )
    adapter = _CaptureExpectedAdapter(jobs)
    scheduled: list[tuple[Callable[..., Any], tuple[Any, ...]]] = []
    service = ShopCollectionService(
        job_service=jobs,
        device_adapter=adapter,
        submitter=_capturing_submitter(scheduled),
    )
    queued = service.enqueue(
        ShopCollectionCreate(
            account_user_id="account-1",
            account_name="账号甲",
        )
    )

    action, args = scheduled.pop()
    result = action(*args)

    assert adapter.requests[0].expected_count == 3
    assert result is not None
    assert result.collection_mode == "preflight"
    assert result.scope_classification == "out_of_scope_physical"
    gate_artifact = next(
        artifact
        for artifact in jobs.get(queued.job_id).artifacts
        if artifact.kind == "shop_scope_gate_result"
    )
    assert gate_artifact.metadata["result"]["classification"] == "out_of_scope_physical"


def test_scope_gate_api_does_not_accept_client_supplied_evidence() -> None:
    """A public profile/title payload could forge an in-scope decision without Android."""
    from backend.app.features.shops.api import router

    assert "/api/v1/shop-scope-gates" not in {route.path for route in router.routes}


def test_bounded_sample_fails_closed_when_detail_screenshot_is_missing(
    tmp_path: Path,
) -> None:
    """Treating a link as product evidence would allow a sample without real detail images."""
    runtime_dir = tmp_path / "runtime"
    jobs = JobService(
        Database(runtime_dir / "workbench.sqlite3"), runtime_dir=runtime_dir
    )
    adapter = _CaptureExpectedAdapter()
    scheduled: list[tuple[Callable[..., Any], tuple[Any, ...]]] = []
    service = ShopCollectionService(
        job_service=jobs,
        device_adapter=adapter,
        submitter=_capturing_submitter(scheduled),
    )
    service.enqueue(
        ShopCollectionCreate(
            account_user_id="account-1",
            account_name="账号甲",
            expected_count=18,
            collection_mode="bounded_sample",
            product_sample_limit=3,
            test_override=True,
            test_override_reason="保留服装店完成一次有界E2E验证",
        )
    )

    action, args = scheduled.pop()
    result = action(*args)

    assert result is not None
    assert result.status == "needs_human"
    assert result.detail == "bounded_sample_evidence_invalid"
    assert result.sample_complete is False
    assert result.shop_complete is False
    assert result.complete is False


def test_full_shop_requires_a_trusted_in_scope_gate_job(tmp_path: Path) -> None:
    """Accepting a missing or out-of-scope gate id would bypass the physical-shop stop."""
    runtime_dir = tmp_path / "runtime"
    jobs = JobService(
        Database(runtime_dir / "workbench.sqlite3"), runtime_dir=runtime_dir
    )
    scheduled: list[tuple[Callable[..., Any], tuple[Any, ...]]] = []
    service = ShopCollectionService(
        job_service=jobs,
        device_adapter=_CaptureExpectedAdapter(jobs),
        submitter=_capturing_submitter(scheduled),
    )
    out_gate = service.enqueue(
        ShopCollectionCreate(
            account_user_id="account-1", account_name="服装店", expected_count=18
        )
    )
    action, args = scheduled.pop()
    action(*args)

    for gate_id in (out_gate.job_id, None):
        with pytest.raises(ValueError, match="in_scope"):
            service.enqueue(
                ShopCollectionCreate(
                    account_user_id="account-1",
                    account_name="账号甲",
                    expected_count=18,
                    collection_mode="full_shop",
                    scope_gate_job_id=gate_id,
                )
            )
    assert not service._futures


def test_full_shop_accepts_only_matching_trusted_in_scope_gate(tmp_path: Path) -> None:
    """Dropping the account binding would let one account reuse another account's gate."""
    runtime_dir = tmp_path / "runtime"
    jobs = JobService(
        Database(runtime_dir / "workbench.sqlite3"), runtime_dir=runtime_dir
    )
    scheduled: list[tuple[Callable[..., Any], tuple[Any, ...]]] = []
    service = ShopCollectionService(
        job_service=jobs,
        device_adapter=_CaptureExpectedAdapter(
            jobs, titles=["运营教程", "测试清单", "网站模板"]
        ),
        submitter=_capturing_submitter(scheduled),
    )
    gate = service.enqueue(
        ShopCollectionCreate(
            account_user_id="account-1",
            account_name="数字资料店",
            expected_count=18,
        )
    )
    action, args = scheduled.pop()
    action(*args)

    service.enqueue(
        ShopCollectionCreate(
            account_user_id="account-1",
            account_name="账号甲",
            expected_count=18,
            collection_mode="full_shop",
            scope_gate_job_id=gate.job_id,
        )
    )

    assert len(scheduled) == 1
    with pytest.raises(ValueError, match="account"):
        service.enqueue(
            ShopCollectionCreate(
                account_user_id="account-2",
                account_name="账号乙",
                expected_count=18,
                collection_mode="full_shop",
                scope_gate_job_id=gate.job_id,
            )
        )
    gate_artifact = next(
        artifact
        for artifact in jobs.get(gate.job_id).artifacts
        if artifact.kind == "shop_scope_gate_result"
    )
    (runtime_dir / gate_artifact.path).write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="trusted in_scope"):
        service.enqueue(
            ShopCollectionCreate(
                account_user_id="account-1",
                account_name="账号甲",
                expected_count=18,
                collection_mode="full_shop",
                scope_gate_job_id=gate.job_id,
            )
        )


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
            collection_mode="legacy_full_shop",
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
                    "collection_mode": "legacy_full_shop",
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
            collection_mode="legacy_full_shop",
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
            collection_mode="legacy_full_shop",
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
            collection_mode="legacy_full_shop",
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
            collection_mode="legacy_full_shop",
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
    retained = list(result_dir.glob("*.tmp"))
    assert len(retained) == 1
    assert retained[0].read_text(encoding="utf-8")


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
            collection_mode="legacy_full_shop",
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
    retained = list(result_dir.glob("*.tmp"))
    assert len(retained) == 1
    assert retained[0].read_text(encoding="utf-8")


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
    assert durable.artifacts[0].producer == "android_shop_worker_v1"
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
            collection_mode="legacy_full_shop",
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
    legacy_queued = jobs.create(
        job_type="shop_collection", input_data={}, progress_total=3
    )
    legacy_running = jobs.create(
        job_type="shop_collection", input_data={}, progress_total=4
    )
    jobs.claim(legacy_running.id)
    unrelated = jobs.create(job_type="radar_collection", input_data={})

    service = ShopCollectionService(
        job_service=jobs,
        device_adapter=_UnusedAdapter(),
        submitter=_capturing_submitter([]),
    )

    for job_id in (queued.id, running.id, legacy_queued.id, legacy_running.id):
        recovered = jobs.get(job_id)
        assert recovered.state is JobState.needs_human
        assert recovered.current_stage == "worker_restart_required"
        assert recovered.error_category == "worker_restart_required"
        assert any("worker restart" in log.message.casefold() for log in recovered.logs)
    assert jobs.get(unrelated.id).state is JobState.queued
    service.close()


def test_app_startup_classifies_expired_shop_workers_before_generic_lease_recovery(
    tmp_path: Path,
) -> None:
    """Generic lease recovery must not erase the physical-worker restart reason."""
    runtime_dir = tmp_path / "runtime"
    database_path = runtime_dir / "workbench.sqlite3"
    seed_database = Database(database_path)
    seed_jobs = JobService(seed_database, runtime_dir=runtime_dir)
    current_shop = seed_jobs.create(
        job_type="android_shop_collection",
        input_data={},
        progress_total=1,
        current_stage="device_pending",
    )
    legacy_shop = seed_jobs.create(
        job_type="shop_collection",
        input_data={},
        progress_total=1,
        current_stage="device_pending",
    )
    non_shop = seed_jobs.create(
        job_type="radar_collection",
        input_data={},
        current_stage="radar_running",
    )
    for job in (current_shop, legacy_shop, non_shop):
        seed_jobs.claim(job.id, lease_seconds=-1)
    seed_database.close()

    app = create_app(
        Settings(runtime_dir=runtime_dir, database_path=database_path)
    )
    try:
        for job_id in (current_shop.id, legacy_shop.id):
            recovered = app.state.job_service.get(job_id)
            assert recovered.state is JobState.needs_human
            assert recovered.current_stage == "worker_restart_required"
            assert recovered.error_category == "worker_restart_required"
            assert any(
                "worker restart" in log.message.casefold() for log in recovered.logs
            )

        generic = app.state.job_service.get(non_shop.id)
        assert generic.state is JobState.needs_human
        assert generic.current_stage == "radar_running"
        assert generic.error_category == "worker_interrupted"
        assert generic.logs[-1].message == (
            "Running lease expired; human recovery required."
        )
    finally:
        app.state.shop_service.close()
        app.state.database.close()


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
