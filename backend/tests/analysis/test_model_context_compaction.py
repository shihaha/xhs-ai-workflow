from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from backend.app.adapters.contracts import ModelResult, StructuredModelRequest
from backend.app.db import Database
from backend.app.features.analysis.model_context import (
    COMPACT_CONTEXT_VERSION,
    COMPACT_PROMPT_VERSION_SUFFIX,
    CompactingModelAdapter,
    build_compact_user_prompt,
    build_model_evidence,
    prompt_size,
)
from backend.app.features.analysis.schemas import AnalysisCreate, AnalysisOutput
from backend.app.features.analysis.service import AnalysisService
from backend.app.features.radar.models import RankItemRecord, RankSnapshotRecord


class CapturingModel:
    provider = "stage3-stub"
    model = "stage3-deterministic"

    def __init__(self, output: dict[str, object]) -> None:
        self.output = output
        self.requests: list[StructuredModelRequest] = []

    @property
    def configured(self) -> bool:
        return True

    def generate_structured(self, request, _schema) -> ModelResult:
        self.requests.append(request)
        return ModelResult(
            model=self.model,
            output=self.output,
            raw_evidence={"attempts": []},
            usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            duration_ms=3,
        )


def _legacy_request(facts: list[dict[str, object]]) -> StructuredModelRequest:
    return StructuredModelRequest(
        system_prompt="Use persisted evidence only.",
        user_prompt=json.dumps(
            {
                "analysis_type": "account_opportunity",
                "account_user_id": None,
                "account_user_ids": ["account-a", "account-b"],
                "allowed_evidence": facts,
                "required_schema": AnalysisOutput.model_json_schema(),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        prompt_version="tutorial-demand-radar-specific-demand-v2",
        evidence_ids=[str(fact["evidence_id"]) for fact in facts],
    )


def _shop_fact(
    evidence_id: str,
    account_user_id: str,
    *,
    raw_blob_chars: int = 0,
) -> dict[str, object]:
    items = []
    for index in range(3):
        items.append(
            {
                "id": f"{account_user_id}-product-{index}",
                "kind": "shop_product",
                "source_url": f"https://www.xiaohongshu.com/goods/{account_user_id}-{index}",
                "raw_evidence": {
                    "ui_hierarchy": "x" * raw_blob_chars,
                    "screenshot_manifest": [f"frame-{n}.png" for n in range(20)],
                    "internal_debug": {"bounds": [1, 2, 3, 4]},
                },
                "data": {
                    "title": f"七宗罪人格测试 {index}",
                    "price": "9.9",
                    "category": "数字内容",
                    "description": "人格测试与结果解读",
                },
            }
        )
    return {
        "evidence_id": evidence_id,
        "kind": "shop_collection_result",
        "job_id": f"job-{account_user_id}",
        "job_state": "succeeded",
        "account_user_id": account_user_id,
        "job_input": {
            "account_user_id": account_user_id,
            "device_id": "physical-device-audit-only",
            "verification_dir": "evidence/shops/huge/audit-only",
            "selector_profile_version": "xhs-shop-v1",
        },
        "trusted_shop_result": {
            "job_id": f"job-{account_user_id}",
            "status": "succeeded",
            "detail": None,
            "selector_profile_version": "xhs-shop-v1",
            "expected_count": 3,
            "discovered_count": 3,
            "collected_count": 3,
            "raw_observation_count": 3,
            "duplicate_observation_count": 0,
            "succeeded_count": 3,
            "missing_count": 0,
            "missing_items": [],
            "collection_missing_count": 0,
            "collection_missing_items": [],
            "rejected_count": 0,
            "rejected_items": [],
            "overflow_count": 0,
            "evidence_artifacts": [
                f"evidence/shops/{account_user_id}/frame-{n}.png" for n in range(30)
            ],
            "items": items,
            "verification": {
                "expected_count": 3,
                "discovered_count": 3,
                "succeeded_count": 3,
                "missing_count": 0,
                "missing_items": [],
                "overflow_count": 0,
                "issues": [],
                "complete": True,
            },
            "complete": True,
            "collection_mode": "evidence_sample",
            "product_sample_limit": 3,
            "available_count_observed": 3,
            "test_override": False,
            "test_override_reason": None,
            "sample_complete": True,
            "shop_complete": False,
            "natural_end_reached": True,
            "scope_classification": "in_scope",
            "scope_reason": "digital delivery verified",
            "sample_manifest_path": f"evidence/shops/{account_user_id}/manifest.json",
            "sample_manifest_sha256": "a" * 64,
            "sample_collection_path": f"evidence/shops/{account_user_id}/collection.json",
            "sample_collection_sha256": "b" * 64,
        },
    }


def _note_fact(evidence_id: str, account_user_id: str, index: int) -> dict[str, object]:
    return {
        "evidence_id": evidence_id,
        "kind": "account_note",
        "account_user_id": account_user_id,
        "facts": {
            "profile": {
                "user_id": account_user_id,
                "source_url": f"https://www.xiaohongshu.com/user/profile/{account_user_id}",
                "nickname": f"账号-{account_user_id}",
                "bio": "心理测试与人格内容",
                "public_stats": {"fans": 1000},
            },
            "note": {
                "note_id": f"note-{account_user_id}-{index}",
                "source_url": f"https://www.xiaohongshu.com/explore/{account_user_id}-{index}",
                "title": f"七宗罪测试 {index}",
                "summary": "用户讨论人格分类、测试结果与分享场景。",
                "published_at": "2026-08-18",
                "public_interactions": {"likes": 100 + index, "comments": 10 + index},
            },
        },
    }


def test_compact_projection_preserves_identity_owner_and_normalized_product_data() -> None:
    facts = [
        _shop_fact("artifact:1", "account-a", raw_blob_chars=50_000),
        _note_fact("account-note:2", "account-a", 0),
        {
            "evidence_id": "rank-item:3",
            "kind": "rank_item",
            "account_user_id": "account-a",
            "user_id": "account-a",
            "source_url": "https://www.xiaohongshu.com/explore/rank-3",
            "facts": {"title": "测试榜单", "gmv_range": "1万-5万"},
            "raw_evidence": {"large": "z" * 50_000},
        },
    ]
    original = deepcopy(facts)

    compact = build_model_evidence(facts)

    assert facts == original
    assert [row["evidence_id"] for row in compact] == [
        "artifact:1",
        "account-note:2",
        "rank-item:3",
    ]
    assert [row["account_user_id"] for row in compact] == ["account-a"] * 3
    shop = compact[0]["trusted_shop_result"]
    assert isinstance(shop, dict)
    assert shop["items"][0]["data"]["title"] == "七宗罪人格测试 0"
    assert shop["items"][0]["data"]["price"] == "9.9"
    assert "raw_evidence" not in shop["items"][0]
    assert "evidence_artifacts" not in shop
    assert "sample_manifest_sha256" not in shop
    assert "raw_evidence" not in compact[2]
    assert compact[1]["facts"] == facts[1]["facts"]


def test_compact_user_prompt_removes_only_proven_duplicate_schema() -> None:
    facts = [_shop_fact("artifact:1", "account-a")]
    legacy = _legacy_request(facts)

    compact = build_compact_user_prompt(legacy, AnalysisOutput)
    payload = json.loads(compact.user_prompt)

    assert payload["context_version"] == COMPACT_CONTEXT_VERSION
    assert "required_schema" not in payload
    assert payload["allowed_evidence"][0]["evidence_id"] == "artifact:1"
    assert compact.evidence_ids == legacy.evidence_ids
    assert compact.system_prompt == legacy.system_prompt
    assert compact.prompt_version == legacy.prompt_version + COMPACT_PROMPT_VERSION_SUFFIX


def test_compaction_rejects_schema_mismatch_instead_of_dropping_it() -> None:
    legacy = _legacy_request([_shop_fact("artifact:1", "account-a")])
    payload = json.loads(legacy.user_prompt)
    payload["required_schema"] = {"type": "object", "properties": {}}
    bad = legacy.model_copy(update={"user_prompt": json.dumps(payload)})

    try:
        build_compact_user_prompt(bad, AnalysisOutput)
    except ValueError as error:
        assert "schema" in str(error)
    else:
        raise AssertionError("schema mismatch must fail closed")


def test_high_bloat_fixture_reduces_serialized_model_context_by_at_least_80_percent() -> None:
    facts: list[dict[str, object]] = [
        _shop_fact("artifact:1", "account-a", raw_blob_chars=120_000),
        _shop_fact("artifact:2", "account-b", raw_blob_chars=120_000),
    ]
    next_id = 3
    for account in ("account-a", "account-b"):
        for index in range(10):
            facts.append(_note_fact(f"account-note:{next_id}", account, index))
            next_id += 1
    legacy = _legacy_request(facts)

    compact = build_compact_user_prompt(legacy, AnalysisOutput)
    legacy_size = prompt_size(legacy.user_prompt)
    compact_size = prompt_size(compact.user_prompt)

    assert compact_size["characters"] <= legacy_size["characters"] * 0.20
    assert compact_size["utf8_bytes"] <= legacy_size["utf8_bytes"] * 0.20
    # Size is a structural proxy only. Provider token savings are measured by a
    # separately authorized live call, never inferred from this assertion.


def _database(root: Path) -> Database:
    root.mkdir(parents=True, exist_ok=True)
    return Database(root / "workbench.sqlite3")


def _seed_rank_item(database: Database) -> str:
    with database.session() as session:
        snapshot = RankSnapshotRecord(
            source_date="2026-08-18",
            collected_at="2026-08-18T10:00:00",
            board="阅读榜",
            dimension="优秀账号",
            source_url="https://example.test/qianfan",
            raw_evidence={"fixture": "stage3-context"},
            submitted_count=1,
        )
        session.add(snapshot)
        session.flush()
        item = RankItemRecord(
            snapshot_id=snapshot.id,
            stable_key="account-a:stage3",
            rank_no=1,
            title="七宗罪测试",
            author_name="受控账号",
            publish_date="2026-08-18",
            read_range="10万以上",
            click_rate_range="10%-20%",
            pay_rate_range="70%-90%",
            gmv_range="1万-5万",
            note_id="stage3-note",
            user_id="account-a",
            source_url="https://www.xiaohongshu.com/explore/stage3-note",
            raw_evidence={"large_debug_blob": "r" * 80_000},
        )
        session.add(item)
        session.commit()
        return f"rank-item:{item.id}"


def _output(evidence_id: str) -> dict[str, object]:
    return {
        "claims": [{"claim": "存在人格测试相关内容", "evidence_ids": [evidence_id]}],
        "product_clusters": [
            {
                "name": "七宗罪测试",
                "summary": "受控 Stage 3 A/B 输出",
                "evidence_ids": [evidence_id],
            }
        ],
        "account_demand_profiles": [],
        "cross_account_conclusion": None,
        "opportunities": [],
    }


def _business_signature(result) -> dict[str, object]:
    return {
        "analysis_type": result.analysis_type,
        "account_user_id": result.account_user_id,
        "account_user_ids": result.account_user_ids,
        "status": result.status,
        "provider": result.provider,
        "model": result.model,
        "input_digest": result.input_digest,
        "evidence_ids": result.evidence_ids,
        "output": result.output.model_dump(mode="json") if result.output else None,
        "usage": result.usage,
        "duration_ms": result.duration_ms,
        "error_category": result.error_category,
    }


def test_analysis_service_business_result_is_identical_with_compacting_adapter(tmp_path: Path) -> None:
    baseline_db = _database(tmp_path / "baseline")
    baseline_evidence = _seed_rank_item(baseline_db)
    baseline_model = CapturingModel(_output(baseline_evidence))
    payload = AnalysisCreate(
        analysis_type="account_report",
        account_user_id="account-a",
        evidence_ids=[baseline_evidence],
    )
    baseline = AnalysisService(
        baseline_db,
        baseline_model,
        runtime_dir=baseline_db.database_path.parent,
    ).create(payload)

    compact_db = _database(tmp_path / "compact")
    compact_evidence = _seed_rank_item(compact_db)
    assert compact_evidence == baseline_evidence
    compact_downstream = CapturingModel(_output(compact_evidence))
    compact = AnalysisService(
        compact_db,
        CompactingModelAdapter(compact_downstream),
        runtime_dir=compact_db.database_path.parent,
    ).create(payload)

    assert _business_signature(compact) == _business_signature(baseline)
    assert len(baseline_model.requests) == 1
    assert len(compact_downstream.requests) == 1
    baseline_prompt = baseline_model.requests[0].user_prompt
    compact_prompt = compact_downstream.requests[0].user_prompt
    assert "large_debug_blob" in baseline_prompt
    assert "large_debug_blob" not in compact_prompt
    assert "required_schema" in json.loads(baseline_prompt)
    assert "required_schema" not in json.loads(compact_prompt)
    assert prompt_size(compact_prompt)["characters"] < prompt_size(baseline_prompt)["characters"]
