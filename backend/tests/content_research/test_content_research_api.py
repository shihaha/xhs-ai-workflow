from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from backend.app.adapters.contracts import ModelResult, StructuredModelRequest
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


def _keyword_payload(prefix: str = "七宗罪测试") -> dict[str, object]:
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
                "keyword": f"{prefix}-{index}",
                "category": category,
                "expand": index == 1,
                "scope": "benchmark",
                "target_count": 10,
            }
            for index, category in enumerate(categories, start=1)
        ]
    }


class FakeKeywordModel:
    configured = True
    provider = "fake_provider"
    model = "fake-keyword-model"

    def __init__(self, payload: dict[str, object] | None = None) -> None:
        self.payload = payload or _keyword_payload("AI关键词")
        self.requests: list[StructuredModelRequest] = []

    def generate_structured(self, request, schema):
        self.requests.append(request)
        validated = schema.model_validate(self.payload)
        return ModelResult(
            model=self.model,
            output=validated.model_dump(mode="json"),
            raw_evidence={"provider_request_id": "fake-request-1"},
            usage={"prompt_tokens": 101, "completion_tokens": 19, "total_tokens": 120},
            duration_ms=42,
        )


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
        body = keywords.json()
        assert body["count"] == 10
        assert body["source"] == "manual"
        assert body["run_id"]
        assert body["provider"] is None
        assert [item["position"] for item in body["items"]] == list(range(1, 11))
        assert all(item["run_id"] == body["run_id"] for item in body["items"])

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
    assert plan.json()["run_id"] == body["run_id"]
    assert plan.json()["items"][0]["keyword"] == "七宗罪测试-1"


@pytest.mark.anyio
async def test_keyword_plan_runs_are_append_only_and_keep_ai_provenance(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    fake = FakeKeywordModel()
    app.state.content_research_service.model_adapter = fake

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        created = await client.post(
            "/api/v1/content-research/dossiers", json=_dossier_payload()
        )
        dossier_id = created.json()["id"]

        generated = await client.post(
            f"/api/v1/content-research/dossiers/{dossier_id}/keywords/generate"
        )
        manual = await client.put(
            f"/api/v1/content-research/dossiers/{dossier_id}/keywords",
            json=_keyword_payload("人工关键词"),
        )
        history = await client.get(
            f"/api/v1/content-research/dossiers/{dossier_id}/keyword-runs"
        )
        latest = await client.get(
            f"/api/v1/content-research/dossiers/{dossier_id}/keywords"
        )

    assert generated.status_code == 200
    generated_body = generated.json()
    assert generated_body["source"] == "ai"
    assert generated_body["provider"] == "fake_provider"
    assert generated_body["model"] == "fake-keyword-model"
    assert generated_body["prompt_version"] == "tutorial-content-keyword-layout-v1"
    assert generated_body["usage"]["total_tokens"] == 120
    assert generated_body["duration_ms"] == 42
    assert generated_body["run_id"] != manual.json()["run_id"]

    assert history.status_code == 200
    history_body = history.json()
    assert len(history_body) == 2
    assert {item["run_id"] for item in history_body} == {
        generated_body["run_id"],
        manual.json()["run_id"],
    }
    assert latest.json()["run_id"] == manual.json()["run_id"]
    assert latest.json()["source"] == "manual"
    assert len(fake.requests) == 1
    assert fake.requests[0].prompt_version == "tutorial-content-keyword-layout-v1"
    assert "七宗罪测试" in fake.requests[0].user_prompt


@pytest.mark.anyio
async def test_invalid_ai_keyword_layout_fails_closed_without_new_run(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    invalid = _keyword_payload("违规关键词")
    for item in invalid["items"][:4]:
        item["category"] = "main"
        item["expand"] = True
    app.state.content_research_service.model_adapter = FakeKeywordModel(invalid)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        created = await client.post(
            "/api/v1/content-research/dossiers", json=_dossier_payload()
        )
        dossier_id = created.json()["id"]
        response = await client.post(
            f"/api/v1/content-research/dossiers/{dossier_id}/keywords/generate"
        )
        history = await client.get(
            f"/api/v1/content-research/dossiers/{dossier_id}/keyword-runs"
        )

    assert response.status_code == 502
    assert history.status_code == 200
    assert history.json() == []


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
