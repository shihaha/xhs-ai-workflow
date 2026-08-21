import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from backend.app.features.shops.service import ShopCollectionCreate
from backend.app.main import create_app
from backend.app.models.jobs import JobArtifactRecord, JobRecord, JobState
from backend.app.settings import Settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _snapshot(items: list[dict[str, object]]) -> dict[str, object]:
    return {
        "source_date": "2026-08-21",
        "collected_at": "2026-08-21T08:00:00+08:00",
        "board": "成交榜",
        "dimension": "优秀账号",
        "source_url": "https://ark.xiaohongshu.com/app-datacenter/market/note-rank",
        "raw_evidence": {"response_id": "scope-funnel"},
        "items": items,
    }


def _item(rank: int, user_id: str, title: str) -> dict[str, object]:
    return {
        "rank_no": rank,
        "title": title,
        "author_name": f"账号{rank}",
        "gmv_range": "1万-5万",
        "pay_rate_range": "15%-25%",
        "read_range": "3万-5万",
        "note_id": f"note-{user_id}",
        "user_id": user_id,
        "source_url": f"https://ark.xiaohongshu.com/api/rank/{user_id}",
        "raw_evidence": {"userFansNum": 2000, "source": "qianfan"},
    }


def _app(tmp_path: Path):
    runtime_dir = tmp_path / "runtime"
    return create_app(Settings(
        runtime_dir=runtime_dir,
        database_path=runtime_dir / "workbench.sqlite3",
        xhs_cli_state_dir=runtime_dir / "xhs-cli-state",
    ))


async def _ingest_and_prescreen(client: httpx.AsyncClient, items: list[dict[str, object]]):
    assert (await client.post("/api/v1/radar/rank-snapshots", json=_snapshot(items))).status_code == 201
    return await client.post(
        "/api/v1/radar/candidate-prescreens",
        json={"source_date": "2026-08-21", "limit": len(items)},
    )


@pytest.mark.anyio
async def test_prescreen_persists_clear_physical_and_ambiguous_public_evidence(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    items = [
        _item(1, "physical", "现货包邮厨房锅具，下单后物流发货"),
        _item(2, "ambiguous", "今天分享一些最近的生活记录"),
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await _ingest_and_prescreen(client, items)

    assert response.status_code == 201
    by_id = {row["user_id"]: row for row in response.json()}
    assert by_id["physical"]["prescreen_classification"] == "clearly_physical"
    assert by_id["physical"]["status"] == "clearly_physical_skipped"
    assert by_id["ambiguous"]["prescreen_classification"] == "uncertain"
    assert by_id["ambiguous"]["status"] == "uncertain_waiting_preflight"
    assert all(row["prescreen_evidence_ids"] for row in by_id.values())

    with app.state.database.session() as session:
        artifacts = session.scalars(
            select(JobArtifactRecord)
            .where(JobArtifactRecord.kind == "radar_scope_prescreen_result")
            .order_by(JobArtifactRecord.id)
        ).all()
    assert len(artifacts) == 2
    for artifact in artifacts:
        evidence_path = app.state.settings.runtime_dir / artifact.path
        assert evidence_path.is_file()
        assert artifact.metadata_json["sha256"] == hashlib.sha256(
            evidence_path.read_bytes()
        ).hexdigest()


@pytest.mark.anyio
async def test_android_scope_result_remains_authoritative_over_prescreen(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await _ingest_and_prescreen(
            client, [_item(1, "physical", "现货包邮厨房锅具，下单后物流发货")]
        )
        assert response.json()[0]["status"] == "clearly_physical_skipped"

        job = app.state.job_service.create(
            job_type="android_shop_collection",
            input_data={
                "account_user_id": "physical",
                "collection_mode": "preflight",
            },
        )
        result = {
            "job_id": job.id,
            "account_user_id": "physical",
            "classification": "in_scope",
        }
        relative = f"evidence/shops/{job.id}/scope-gate.json"
        absolute = app.state.settings.runtime_dir / relative
        absolute.parent.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(result, sort_keys=True) + "\n").encode("utf-8")
        absolute.write_bytes(encoded)
        app.state.job_service.attach_artifact(
            job.id,
            kind="shop_scope_gate_result",
            path=relative,
            metadata={"result": result, "sha256": hashlib.sha256(encoded).hexdigest()},
        )
        app.state.job_service.transition(job.id, JobState.running)
        app.state.job_service.transition(job.id, JobState.succeeded)
        funnel = await client.get(
            "/api/v1/radar/candidate-funnel",
            params={"source_date": "2026-08-21", "limit": 1000},
        )

    candidate = funnel.json()[0]
    assert candidate["prescreen_classification"] == "clearly_physical"
    assert candidate["android_scope_classification"] == "in_scope"
    assert candidate["status"] == "in_scope"


@pytest.mark.anyio
async def test_likely_digital_still_waits_for_android_and_is_not_analysis_evidence(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    app.state.bailian_adapter = SimpleNamespace(configured=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await _ingest_and_prescreen(
            client,
            [
                _item(1, "digital", "网盘交付PDF考试题库和可编辑模板"),
                _item(2, "digital-two", "电子资料和在线课程模板"),
            ],
        )
        analysis = await client.post(
            "/api/v1/analyses",
            json={
                "analysis_type": "account_opportunity",
                "account_user_ids": ["digital", "digital-two"],
                "evidence_ids": [
                    evidence_id
                    for candidate in response.json()
                    for evidence_id in candidate["prescreen_evidence_ids"]
                ],
            },
        )

    candidate = response.json()[0]
    assert candidate["prescreen_classification"] == "likely_digital"
    assert candidate["android_scope_classification"] == "unknown"
    assert candidate["status"] == "likely_digital_waiting_preflight"
    assert analysis.status_code == 201
    assert analysis.json()["status"] == "needs_human"
    assert analysis.json()["output"] is None


@pytest.mark.anyio
async def test_candidate_replacement_continues_beyond_top_twenty_in_score_order(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    items = [
        _item(rank, f"physical-{rank:02d}", f"现货包邮实体餐具第{rank}款")
        for rank in range(1, 21)
    ] + [
        _item(21, "digital-21", "电子版简历模板，网盘交付"),
        _item(22, "digital-22", "在线课程资料和PDF练习册"),
    ]
    queued: list[ShopCollectionCreate] = []

    class _ShopQueue:
        def enqueue(self, payload: ShopCollectionCreate):
            queued.append(payload)
            return SimpleNamespace(job_id="queued-next", status="queued")

    app.state.shop_service = _ShopQueue()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await _ingest_and_prescreen(client, items)
        advance = await client.post(
            "/api/v1/radar/candidate-funnel/advance",
            json={"source_date": "2026-08-21"},
        )

    assert [row["candidate_position"] for row in response.json()] == list(range(1, 23))
    assert advance.status_code == 202
    assert advance.json()["account_user_id"] == "digital-21"
    assert len(queued) == 1
    assert queued[0].account_user_id == "digital-21"
    assert queued[0].collection_mode == "preflight"
    assert queued[0].expected_count == 3
    assert queued[0].product_sample_limit is None


@pytest.mark.anyio
async def test_replacement_never_prefers_a_later_matching_direction(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    items = [
        _item(1, "coupon-first", "餐饮电子优惠券和数字兑换码"),
        _item(2, "exam-second", "考公PDF题库和网盘课程资料"),
        _item(3, "coupon-third", "咖啡电子优惠券和数字权益"),
    ]
    queued: list[ShopCollectionCreate] = []

    class _ShopQueue:
        def enqueue(self, payload: ShopCollectionCreate):
            queued.append(payload)
            return SimpleNamespace(job_id="queued-next", status="queued")

    app.state.shop_service = _ShopQueue()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        await _ingest_and_prescreen(client, items)
        now = datetime.now(UTC).replace(tzinfo=None)
        with app.state.database.session() as session:
            session.add(JobRecord(
                type="android_shop_collection",
                input_data={
                    "account_user_id": "coupon-first",
                    "account_name": "账号1",
                    "collection_mode": "preflight",
                },
                state=JobState.failed,
                progress_current=0,
                progress_total=3,
                current_stage="shop_failed",
                error_category="device_unavailable",
                created_at=now,
                updated_at=now,
                completed_at=now,
            ))
            session.commit()
        advance = await client.post(
            "/api/v1/radar/candidate-funnel/advance",
            json={"source_date": "2026-08-21"},
        )

    assert advance.status_code == 202
    assert advance.json()["account_user_id"] == "exam-second"
    assert queued[0].account_user_id == "exam-second"


@pytest.mark.anyio
async def test_prescreen_failure_fails_open_to_uncertain_with_real_evidence(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)

    def broken_classifier(_facts):
        raise RuntimeError("classifier unavailable")

    app.state.radar_service.prescreen_classifier = broken_classifier
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await _ingest_and_prescreen(
            client,
            [_item(1, "unavailable", "公开内容仍然存在")],
        )

    [candidate] = response.json()
    assert candidate["prescreen_classification"] == "uncertain"
    assert "预筛规则不可用" in candidate["prescreen_reason"]
    assert candidate["prescreen_evidence_ids"]


@pytest.mark.anyio
async def test_pending_or_running_higher_rank_blocks_parallel_advance(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    items = [
        _item(1, "first", "PDF模板网盘交付"),
        _item(2, "second", "软件工具数字服务"),
    ]
    app.state.shop_service = SimpleNamespace(
        enqueue=lambda _payload: pytest.fail("must not queue a lower ranked account")
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        await _ingest_and_prescreen(client, items)
        now = datetime.now(UTC).replace(tzinfo=None)
        with app.state.database.session() as session:
            session.add(JobRecord(
                type="android_shop_collection",
                input_data={
                    "account_user_id": "first",
                    "account_name": "账号1",
                    "collection_mode": "preflight",
                },
                state=JobState.running,
                progress_current=0,
                progress_total=3,
                current_stage="device_ready",
                error_category=None,
                created_at=now,
                updated_at=now,
                started_at=now,
            ))
            session.commit()
        advance = await client.post(
            "/api/v1/radar/candidate-funnel/advance",
            json={"source_date": "2026-08-21"},
        )

    assert advance.status_code == 409
    assert "higher-ranked preflight is still active" in advance.json()["detail"]
