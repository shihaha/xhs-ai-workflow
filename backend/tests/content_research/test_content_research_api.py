from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from backend.app.main import create_app
from backend.app.settings import Settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _app(tmp_path: Path):
    runtime = tmp_path / "runtime"
    return create_app(
        Settings(runtime_dir=runtime, database_path=runtime / "workbench.sqlite3")
    )


def _dossier_payload() -> dict[str, object]:
    return {
        "product_key": "seven-sins-test",
        "name": "七宗罪测试",
        "version": "1.0.0",
        "target_user": "希望通过轻量测试了解自身性格倾向的用户",
        "core_need": "用低门槛方式获得可理解、可分享的人格测试结果",
        "deliverables": ["测试题", "结果解释", "使用说明"],
        "usage_instructions": "按题目作答，完成后根据规则查看对应结果。",
        "faq": [
            {"question": "这是医学诊断吗？", "answer": "不是，仅用于一般性自我了解。"}
        ],
        "allowed_claims": ["提供七宗罪主题的人格测试体验"],
        "forbidden_claims": ["可用于心理疾病诊断"],
        "source_index": ["产品说明.md", "测试题-v1.pdf", "结果说明-v1.pdf"],
        "uat_status": "passed",
    }


def _keyword_payload() -> dict[str, object]:
    categories = [
        "main",
        "main",
        "audience",
        "pain",
        "scenario",
        "question",
        "comparison",
        "positioning",
        "selling_point",
        "visual",
    ]
    return {
        "items": [
            {
                "keyword": f"七宗罪测试-{index}",
                "category": category,
                "expand": index == 1,
                "scope": "benchmark",
                "target_count": 10,
            }
            for index, category in enumerate(categories, start=1)
        ]
    }


@pytest.mark.anyio
async def test_finished_product_dossier_and_keywords_persist_across_restart(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        created = await client.post(
            "/api/v1/content-research/dossiers", json=_dossier_payload()
        )
        assert created.status_code == 201
        dossier = created.json()
        assert dossier["product_key"] == "seven-sins-test"
        assert dossier["uat_status"] == "passed"

        keywords = await client.put(
            f"/api/v1/content-research/dossiers/{dossier['id']}/keywords",
            json=_keyword_payload(),
        )
        assert keywords.status_code == 200
        assert keywords.json()["count"] == 10
        assert [item["position"] for item in keywords.json()["items"]] == list(
            range(1, 11)
        )

    restarted = _app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=restarted), base_url="http://testserver"
    ) as client:
        dossiers = await client.get("/api/v1/content-research/dossiers")
        plan = await client.get(
            f"/api/v1/content-research/dossiers/{dossier['id']}/keywords"
        )

    assert dossiers.status_code == 200
    assert [item["id"] for item in dossiers.json()] == [dossier["id"]]
    assert plan.status_code == 200
    assert plan.json()["count"] == 10
    assert plan.json()["items"][0]["keyword"] == "七宗罪测试-1"


@pytest.mark.anyio
async def test_only_finished_products_can_enter_content_research(tmp_path: Path) -> None:
    app = _app(tmp_path)
    payload = _dossier_payload()
    payload["uat_status"] = "draft"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/content-research/dossiers", json=payload
        )

    assert response.status_code == 422
    assert await _list_dossiers(app) == []


@pytest.mark.anyio
async def test_duplicate_finished_product_version_is_a_conflict(tmp_path: Path) -> None:
    app = _app(tmp_path)
    payload = _dossier_payload()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        first = await client.post("/api/v1/content-research/dossiers", json=payload)
        second = await client.post("/api/v1/content-research/dossiers", json=payload)

    assert first.status_code == 201
    assert second.status_code == 409


@pytest.mark.anyio
async def test_keyword_plan_requires_tutorial_sized_network(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        created = await client.post(
            "/api/v1/content-research/dossiers", json=_dossier_payload()
        )
        dossier_id = created.json()["id"]
        too_short = _keyword_payload()
        too_short["items"] = too_short["items"][:9]
        response = await client.put(
            f"/api/v1/content-research/dossiers/{dossier_id}/keywords",
            json=too_short,
        )

    assert response.status_code == 422


async def _list_dossiers(app) -> list[dict[str, object]]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/api/v1/content-research/dossiers")
    assert response.status_code == 200
    return response.json()
