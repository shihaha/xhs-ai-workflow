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
    gmv: str,
    pay: str,
    read: str,
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
