from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.app.adapters.contracts import ModelResult
from backend.app.db import Database
from backend.app.features.analysis.models import AnalysisRecord, OpportunityRecord
from backend.app.features.content.schemas import (
    ContentItemCreate,
    MaterialCreate,
    ProductCreate,
)
from backend.app.features.content.service import ContentService, ContentValidationError
from backend.app.features.radar.models import RankItemRecord, RankSnapshotRecord


class FakeModel:
    configured = True
    provider = "controlled_fake"
    model = "fake-content-v1"

    def __init__(self, evidence_id: str) -> None:
        self.evidence_id = evidence_id
        self.output: dict[str, object] | None = None

    def generate_structured(self, request: object, schema: object) -> ModelResult:
        output = self.output if self.output is not None else {
                "title": "一份有来源的清单",
                "body": "这是基于已保存证据生成的正文。",
                "claims": [
                    {"claim": "存在这个需求", "evidence_ids": [self.evidence_id]}
                ],
                "source_evidence_ids": [self.evidence_id],
            }
        return ModelResult(
            model=self.model,
            output=output,
            raw_evidence={"attempts": [{"attempt": 1, "category": "response_received"}]},
            usage={"total_tokens": 8},
            duration_ms=1,
        )


def seed_database(database: Database) -> tuple[str, str]:
    now = datetime.now(UTC).replace(tzinfo=None)
    with database.session() as session:
        snapshot = RankSnapshotRecord(
            source_date="2026-08-17", collected_at="2026-08-17T10:00:00",
            board="成交榜", dimension="优秀账号",
            source_url="https://example.com/rank", raw_evidence={"source": "fixture"},
            submitted_count=1,
        )
        item = RankItemRecord(
            stable_key="note:content-1", rank_no=1, user_id="account-a",
            source_url="https://example.com/note/1", raw_evidence={"source": "fixture"},
        )
        snapshot.items.append(item)
        session.add(snapshot)
        session.flush()
        evidence_id = f"rank-item:{item.id}"
        analysis = AnalysisRecord(
            analysis_type="account_opportunity", account_user_id=None,
            account_user_ids_json=["account-a"], status="succeeded",
            prompt_version="analysis-v1", provider="controlled_fake", model="fake",
            input_digest="a" * 64, evidence_ids_json=[evidence_id],
            output_json={"claims": [], "product_clusters": [], "opportunities": []},
            usage_json={}, attempts_json=[], created_at=now,
        )
        opportunity = OpportunityRecord(
            analysis=analysis, title="资料产品机会", status="升温",
            summary="真实证据支持", evidence_ids_json=[evidence_id],
            next_action="制作一篇待审核内容", created_at=now,
        )
        session.add(analysis)
        session.commit()
        opportunity_id = opportunity.id
    return opportunity_id, evidence_id


def seeded_service(tmp_path: Path) -> tuple[ContentService, str, str]:
    database = Database(tmp_path / "db.sqlite3")
    opportunity_id, evidence_id = seed_database(database)
    return ContentService(database, FakeModel(evidence_id), runtime_dir=tmp_path), opportunity_id, evidence_id


def create_product(service: ContentService, opportunity_id: str) -> str:
    return service.create_product(
        ProductCreate(name="测试产品", target_user="需要清单的人", opportunity_id=opportunity_id)
    ).id


def test_material_versions_are_hashed_by_system_and_old_versions_remain(tmp_path: Path) -> None:
    service, opportunity_id, _ = seeded_service(tmp_path)
    product_id = create_product(service, opportunity_id)
    material = tmp_path / "materials" / "facts.md"
    material.parent.mkdir()
    material.write_text("第一版事实", encoding="utf-8")

    first = service.add_material(product_id, MaterialCreate(
        logical_name="facts.md", path="materials/facts.md", media_type="text/markdown"
    ))
    material.write_text("第二版事实", encoding="utf-8")
    second = service.add_material(product_id, MaterialCreate(
        logical_name="facts.md", path="materials/facts.md", media_type="text/markdown"
    ))

    assert (first.version, second.version) == (1, 2)
    assert first.sha256 != second.sha256
    assert (tmp_path / first.path).read_text(encoding="utf-8") == "第一版事实"
    assert len(service.get_product(product_id).materials) == 2


@pytest.mark.parametrize("path", ["../outside.md", "materials/missing.md"])
def test_material_path_must_be_existing_contained_regular_file(tmp_path: Path, path: str) -> None:
    service, opportunity_id, _ = seeded_service(tmp_path)
    product_id = create_product(service, opportunity_id)
    (tmp_path.parent / "outside.md").write_text("outside", encoding="utf-8")
    with pytest.raises(ContentValidationError):
        service.add_material(product_id, MaterialCreate(
            logical_name="facts.md", path=path, media_type="text/markdown"
        ))


def test_material_symlink_is_rejected(tmp_path: Path) -> None:
    service, opportunity_id, _ = seeded_service(tmp_path)
    product_id = create_product(service, opportunity_id)
    outside = tmp_path.parent / "real-material.md"
    outside.write_text("outside", encoding="utf-8")
    link = tmp_path / "linked.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ContentValidationError):
        service.add_material(product_id, MaterialCreate(
            logical_name="facts.md", path="linked.md", media_type="text/markdown"
        ))


def test_client_cannot_supply_material_hash_or_version() -> None:
    with pytest.raises(ValidationError):
        MaterialCreate.model_validate({
            "logical_name": "facts.md", "path": "materials/facts.md",
            "media_type": "text/markdown", "sha256": "0" * 64, "version": 99,
        })


def test_source_linked_research_and_model_revision_are_persisted(tmp_path: Path) -> None:
    service, opportunity_id, evidence_id = seeded_service(tmp_path)
    product_id = create_product(service, opportunity_id)
    material = tmp_path / "materials" / "facts.md"
    material.parent.mkdir()
    material.write_text("事实资料", encoding="utf-8")
    material_id = service.add_material(product_id, MaterialCreate(
        logical_name="facts.md", path="materials/facts.md", media_type="text/markdown"
    )).id

    item = service.create_content_item(ContentItemCreate(
        product_id=product_id, opportunity_id=opportunity_id,
        template_key="list-v1", evidence_ids=[evidence_id], material_ids=[material_id],
        research_facts=[{"fact": "用户需要清单", "evidence_ids": [evidence_id]}],
    ))

    assert item.status == "review"
    assert item.current_revision is not None
    assert item.current_revision.number == 1
    assert item.current_revision.claims[0].evidence_ids == [evidence_id]
    assert len(service.get_content_item(item.id).revisions) == 1


def test_unknown_or_cross_opportunity_citation_rejects_whole_draft(tmp_path: Path) -> None:
    service, opportunity_id, _ = seeded_service(tmp_path)
    product_id = create_product(service, opportunity_id)
    with pytest.raises(ContentValidationError):
        service.create_content_item(ContentItemCreate(
            product_id=product_id, opportunity_id=opportunity_id,
            template_key="list-v1", evidence_ids=["rank-item:999999"],
            research_facts=[{"fact": "伪造事实", "evidence_ids": ["rank-item:999999"]}],
        ))
    assert service.list_content_items() == []
