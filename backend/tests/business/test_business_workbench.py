from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest

from backend.app.db import Database
from backend.app.features.analysis.models import AnalysisRecord, OpportunityRecord
from backend.app.features.business.service import BusinessWorkbenchService
from backend.app.features.content.models import ProductRecord
from backend.app.main import create_app
from backend.app.settings import Settings


NOW = datetime(2026, 8, 24, 10, 0, 0)


def _analysis(*, product_id: str = "product-a") -> AnalysisRecord:
    source_url = "https://www.xiaohongshu.com/goods/product-a"
    facts = [
        {
            "evidence_id": "artifact:1",
            "trusted_shop_result": {
                "items": [
                    {
                        "id": product_id,
                        "source_url": source_url,
                        "data": {"title": "七宗罪人格测试", "price": "¥19.9", "sold": "1.2万+"},
                        "raw_evidence": {},
                    }
                ]
            },
        }
    ]
    trust = [{"evidence_id": "artifact:1"}]
    fingerprint_value = json.dumps(
        {
            "account_scope": ["a", "b", "c"],
            "allowed_ids": ["artifact:1"],
            "facts": facts,
            "trust": trust,
            "artifact_bindings": [],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return AnalysisRecord(
        analysis_type="account_opportunity",
        account_user_id=None,
        account_user_ids_json=["a", "b", "c"],
        status="succeeded",
        prompt_version="test-v1",
        provider="chatgpt",
        model="test-model",
        input_digest="a" * 64,
        evidence_ids_json=["artifact:1"],
        evidence_snapshot_json={
            "schema_version": 1,
            "trust_fingerprint": hashlib.sha256(fingerprint_value.encode("utf-8")).hexdigest(),
            "account_scope": ["a", "b", "c"],
            "allowed_ids": ["artifact:1"],
            "facts": facts,
            "trust": trust,
            "artifact_bindings": [],
            "input_digest": "a" * 64,
        },
        output_json={"cross_account_conclusion": {"has_specific_shared_demand": True}},
        usage_json={},
        attempts_json=[],
        created_at=NOW,
    )


def _opportunity(
    analysis: AnalysisRecord,
    *,
    title: str,
    review_status: str,
    evidence_level: str = "validated_candidate",
) -> OpportunityRecord:
    reviewed_at = NOW if review_status != "pending_review" else None
    return OpportunityRecord(
        analysis=analysis,
        title=title,
        status="已验证" if evidence_level == "validated_candidate" else "升温",
        summary="多个独立账号反复销售相近的虚拟人格测试。",
        evidence_ids_json=["artifact:1"],
        review_status=review_status,
        evidence_level=evidence_level,
        supporting_accounts_json=[
            {"account_user_id": "a", "shop_evidence_ids": ["artifact:1"], "note_evidence_ids": []},
            {"account_user_id": "b", "shop_evidence_ids": ["artifact:1"], "note_evidence_ids": []},
            {"account_user_id": "c", "shop_evidence_ids": ["artifact:1"], "note_evidence_ids": []},
        ],
        supporting_products_json=[
            {
                "account_user_id": "a",
                "evidence_id": "artifact:1",
                "product_id": "product-a",
                "title": "七宗罪人格测试",
                "source_url": "https://www.xiaohongshu.com/goods/product-a",
                "image_evidence_count": 4,
            }
        ],
        supporting_notes_json=[],
        reviewed_at=reviewed_at,
        rejection_reason="不跟进" if review_status == "rejected" else None,
        next_action="人工判断是否跟进",
        created_at=NOW,
    )


def test_demand_radar_projects_durable_business_state_without_inventing_project(tmp_path: Path) -> None:
    database = Database(tmp_path / "workbench.db", runtime_dir=tmp_path)
    with database.session() as session:
        pending_analysis = _analysis()
        pending = _opportunity(pending_analysis, title="人格测试方向", review_status="pending_review")
        approved_analysis = _analysis(product_id="product-b")
        approved = _opportunity(
            approved_analysis,
            title="资料模板方向",
            review_status="approved",
            evidence_level="warming_candidate",
        )
        session.add_all([pending_analysis, pending, approved_analysis, approved])
        session.flush()
        session.add(
            ProductRecord(
                opportunity_id=approved.id,
                name="历史资料产品",
                target_user="需要模板的用户",
                created_at=NOW,
            )
        )
        session.commit()

    result = BusinessWorkbenchService(database, today=lambda: date(2026, 8, 24)).demand_radar()

    assert result.summary.total_direction_count == 2
    assert result.summary.new_today_count == 2
    assert result.summary.pending_decision_count == 1
    assert result.summary.validated_count == 1
    assert result.summary.warming_count == 1
    assert result.summary.linked_product_count == 1

    pending = next(item for item in result.directions if item.title == "人格测试方向")
    assert pending.journey_stage == "demand_decision"
    assert pending.can_follow_up is True
    assert pending.representative_products[0].price == "¥19.9"
    assert pending.representative_products[0].sold == "1.2万+"
    assert pending.image_evidence_count == 4

    approved = next(item for item in result.directions if item.title == "资料模板方向")
    assert approved.journey_stage == "legacy_product_workspace"
    assert approved.linked_products[0].name == "历史资料产品"
    assert "Product Definition" not in approved.next_business_action or "核对" in approved.next_business_action


def test_approved_direction_without_product_moves_to_product_definition(tmp_path: Path) -> None:
    database = Database(tmp_path / "workbench.db", runtime_dir=tmp_path)
    with database.session() as session:
        analysis = _analysis()
        opportunity = _opportunity(analysis, title="人格测试方向", review_status="approved")
        session.add_all([analysis, opportunity])
        session.commit()

    result = BusinessWorkbenchService(database, today=lambda: date(2026, 8, 24)).demand_radar()
    assert result.directions[0].journey_stage == "product_definition"
    assert "Product Definition" in result.directions[0].next_business_action


@pytest.mark.anyio
async def test_business_demand_radar_api_is_safe_projection(tmp_path: Path) -> None:
    settings = Settings(runtime_dir=tmp_path / "runtime", database_path=tmp_path / "runtime" / "workbench.db")
    app = create_app(settings)
    assert app.state.database is not None
    with app.state.database.session() as session:
        analysis = _analysis()
        opportunity = _opportunity(analysis, title="人格测试方向", review_status="pending_review")
        session.add_all([analysis, opportunity])
        session.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/business/demand-radar")

    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["pending_decision_count"] == 1
    direction = body["directions"][0]
    assert direction["journey_stage"] == "demand_decision"
    rendered = str(body)
    assert "artifact_path" not in rendered
    assert "AgentRun" not in rendered
    assert "job_id" not in rendered
