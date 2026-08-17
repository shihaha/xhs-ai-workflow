from pathlib import Path

import httpx
import pytest

from backend.app.main import create_app
from backend.app.settings import Settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _payload(
    *,
    source_date: str,
    collected_at: str,
    board: str,
    user_id: str,
    account_name: str,
    fans: int,
    gmv: str | None,
    pay: str | None,
    read: str | None,
) -> dict[str, object]:
    return {
        "source_date": source_date,
        "collected_at": collected_at,
        "board": board,
        "dimension": "优秀账号",
        "source_url": "https://ark.xiaohongshu.com/app-datacenter/market/note-rank",
        "raw_evidence": {"response_id": f"{source_date}-{board}-{user_id}"},
        "items": [
            {
                "rank_no": 1,
                "title": "医疗减肥课程",
                "author_name": account_name,
                "gmv_range": gmv,
                "pay_rate_range": pay,
                "read_range": read,
                "note_id": f"note-{source_date}-{board}-{user_id}",
                "user_id": user_id,
                "source_url": "https://ark.xiaohongshu.com/api/rank",
                "raw_evidence": {"userFansNum": fans, "source": "qianfan"},
            }
        ],
    }


@pytest.mark.anyio
async def test_rank_snapshot_accounts_and_candidate_apis_use_persisted_evidence(
    tmp_path: Path,
) -> None:
    """Candidate output must come from stored snapshots and retain explainable score facts."""
    runtime_dir = tmp_path / "runtime"
    app = create_app(
        Settings(runtime_dir=runtime_dir, database_path=runtime_dir / "workbench.sqlite3")
    )
    snapshots = [
        _payload(
            source_date="2026-08-16",
            collected_at="2026-08-16T09:00:00+08:00",
            board="成交榜",
            user_id="account-a",
            account_name="账号甲旧名",
            fans=800,
            gmv="￥1000-5000",
            pay="15%-25%",
            read="3万-5万",
        ),
        _payload(
            source_date="2026-08-17",
            collected_at="2026-08-17T09:00:00+08:00",
            board="热卖榜",
            user_id="account-a",
            account_name="账号甲",
            fans=200,
            gmv="1万-5万",
            pay="70%-90%",
            read="10万以上",
        ),
        _payload(
            source_date="2026-08-17",
            collected_at="2026-08-17T09:05:00+08:00",
            board="引流榜",
            user_id="account-b",
            account_name="账号乙",
            fans=5000,
            gmv="￥0-1000",
            pay="0-5%",
            read="1000-3000",
        ),
    ]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        responses = [
            await client.post("/api/v1/radar/rank-snapshots", json=payload)
            for payload in snapshots
        ]
        listed = await client.get(
            "/api/v1/radar/rank-snapshots", params={"source_date": "2026-08-17"}
        )
        accounts = await client.get("/api/v1/radar/accounts")
        candidates = await client.get(
            "/api/v1/radar/candidates",
            params={"source_date": "2026-08-17", "limit": 1},
        )

    assert [response.status_code for response in responses] == [201, 201, 201]
    assert len(listed.json()) == 2
    assert [account["user_id"] for account in accounts.json()] == ["account-a", "account-b"]
    assert candidates.status_code == 200
    assert candidates.json() == [
        {
            "user_id": "account-a",
            "account_name": "账号甲",
            "score": 1.74,
            "evidence": 1.0,
            "credibility": 1.18,
            "accessibility": 1.48,
            "fans": 200,
            "gmv": "1万-5万",
            "pay": "70%-90%",
            "read": "10万以上",
            "nday": 2,
            "nboard": 2,
        }
    ]
    assert "category" not in candidates.json()[0]


@pytest.mark.anyio
async def test_candidates_require_a_real_source_date(tmp_path: Path) -> None:
    """Omitting the candidate date must not silently score an invented current run."""
    runtime_dir = tmp_path / "runtime"
    app = create_app(
        Settings(runtime_dir=runtime_dir, database_path=runtime_dir / "workbench.sqlite3")
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/api/v1/radar/candidates")

    assert response.status_code == 422


@pytest.mark.anyio
async def test_dated_candidates_do_not_use_future_score_name_or_fans(
    tmp_path: Path,
) -> None:
    """A later snapshot must not rewrite what was knowable on the requested candidate date."""
    runtime_dir = tmp_path / "runtime"
    app = create_app(
        Settings(runtime_dir=runtime_dir, database_path=runtime_dir / "workbench.sqlite3")
    )
    earlier = _payload(
        source_date="2026-08-17",
        collected_at="2026-08-17T09:00:00+08:00",
        board="成交榜",
        user_id="account-a",
        account_name="当时账号名",
        fans=900,
        gmv="￥0-1000",
        pay="0-5%",
        read="1000-3000",
    )
    future = _payload(
        source_date="2026-08-18",
        collected_at="2026-08-18T09:00:00+08:00",
        board="热卖榜",
        user_id="account-a",
        account_name="未来账号名",
        fans=1,
        gmv="1万-5万",
        pay="70%-90%",
        read="10万以上",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        assert (await client.post("/api/v1/radar/rank-snapshots", json=earlier)).status_code == 201
        assert (await client.post("/api/v1/radar/rank-snapshots", json=future)).status_code == 201
        response = await client.get(
            "/api/v1/radar/candidates", params={"source_date": "2026-08-17"}
        )

    assert response.json() == [
        {
            "user_id": "account-a",
            "account_name": "当时账号名",
            "score": 0.12,
            "evidence": 0.1,
            "credibility": 1.09,
            "accessibility": 1.06,
            "fans": 900,
            "gmv": "￥0-1000",
            "pay": "0-5%",
            "read": "1000-3000",
            "nday": 1,
            "nboard": 1,
        }
    ]


@pytest.mark.anyio
async def test_accounts_keep_read_only_and_pay_only_evidence(tmp_path: Path) -> None:
    """Missing GMV must contribute zero, not erase otherwise recognized demand evidence."""
    runtime_dir = tmp_path / "runtime"
    app = create_app(
        Settings(runtime_dir=runtime_dir, database_path=runtime_dir / "workbench.sqlite3")
    )
    read_only = _payload(
        source_date="2026-08-17",
        collected_at="2026-08-17T09:00:00+08:00",
        board="阅读榜",
        user_id="read-account",
        account_name="阅读证据账号",
        fans=1000,
        gmv=None,
        pay=None,
        read="10万以上",
    )
    pay_only = _payload(
        source_date="2026-08-17",
        collected_at="2026-08-17T09:05:00+08:00",
        board="引流榜",
        user_id="pay-account",
        account_name="转化证据账号",
        fans=1000,
        gmv=None,
        pay="70%-90%",
        read=None,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        await client.post("/api/v1/radar/rank-snapshots", json=read_only)
        await client.post("/api/v1/radar/rank-snapshots", json=pay_only)
        response = await client.get("/api/v1/radar/accounts")

    assert [(row["user_id"], row["score"]) for row in response.json()] == [
        ("pay-account", 0.38),
        ("read-account", 0.16),
    ]
    assert response.json()[0]["gmv"] == "—"
    assert response.json()[1]["pay"] == "—"


@pytest.mark.anyio
async def test_snapshot_and_account_list_pagination_is_bounded_and_validated(
    tmp_path: Path,
) -> None:
    """List responses must honor pages and reject unbounded or negative query values."""
    runtime_dir = tmp_path / "runtime"
    app = create_app(
        Settings(runtime_dir=runtime_dir, database_path=runtime_dir / "workbench.sqlite3")
    )
    payloads = [
        _payload(
            source_date="2026-08-17",
            collected_at=f"2026-08-17T09:0{index}:00+08:00",
            board=board,
            user_id=f"account-{index}",
            account_name=f"账号{index}",
            fans=1000,
            gmv="￥0-1000",
            pay="0-5%",
            read="1000-3000",
        )
        for index, board in enumerate(("阅读榜", "引流榜"))
    ]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        for payload in payloads:
            await client.post("/api/v1/radar/rank-snapshots", json=payload)
        snapshot_page = await client.get(
            "/api/v1/radar/rank-snapshots", params={"limit": 1, "offset": 1}
        )
        account_page = await client.get(
            "/api/v1/radar/accounts", params={"limit": 1, "offset": 1}
        )
        invalid = [
            await client.get(path, params=params)
            for path in ("/api/v1/radar/rank-snapshots", "/api/v1/radar/accounts")
            for params in ({"limit": 0}, {"limit": 101}, {"offset": -1})
        ]

    assert len(snapshot_page.json()) == 1
    assert [row["user_id"] for row in account_page.json()] == ["account-1"]
    assert [response.status_code for response in invalid] == [422] * 6
