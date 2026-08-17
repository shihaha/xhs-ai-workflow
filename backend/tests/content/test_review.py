from pathlib import Path
import json

import pytest

from backend.app.features.content.schemas import ContentItemCreate, RegenerateCreate, ReviewCreate
from backend.app.features.content.service import ContentStateError, ContentValidationError
from backend.tests.content.test_workflow import add_output_image, create_product, seeded_service


def _draft(tmp_path: Path):
    service, opportunity_id, evidence_id = seeded_service(tmp_path)
    product_id = create_product(service, opportunity_id)
    image = add_output_image(service, product_id, tmp_path)
    item = service.create_content_item(ContentItemCreate(
        product_id=product_id, opportunity_id=opportunity_id,
        template_key="list-v1", evidence_ids=[evidence_id],
        image_material_ids=[image.id], cover_material_id=image.id,
        research_facts=[{"fact": "真实事实", "evidence_ids": [evidence_id]}],
    ))
    return service, item, image


def test_reject_regenerate_approve_preserves_revision_and_review_history(tmp_path: Path) -> None:
    service, item, image = _draft(tmp_path)
    r1 = item.current_revision.id
    rejected = service.review(item.id, ReviewCreate(
        decision="reject", actor="operator", note="标题不够具体", expected_revision_id=r1,
        visual_checks=[{"material_id": image.id, "passed": False, "observation": "标题不具体"}],
    ))
    regenerated = service.regenerate(item.id, RegenerateCreate(expected_revision_id=r1))
    approved = service.review(item.id, ReviewCreate(
        decision="approve", actor="operator", note="事实和表达已核对",
        expected_revision_id=regenerated.current_revision.id,
        visual_checks=[{"material_id": image.id, "passed": True, "observation": "图文一致且清晰"}],
    ))

    assert rejected.status == "rejected"
    assert regenerated.status == "review"
    assert approved.status == "approved"
    assert [revision.number for revision in approved.revisions] == [1, 2]
    assert approved.revisions[0].body == item.revisions[0].body
    assert [review.decision for review in approved.reviews] == ["reject", "regenerate", "approve"]


def test_illegal_transitions_are_rejected(tmp_path: Path) -> None:
    service, item, image = _draft(tmp_path)
    service.review(item.id, ReviewCreate(
        decision="approve", actor="operator", note="ok", expected_revision_id=item.current_revision.id,
        visual_checks=[{"material_id": image.id, "passed": True, "observation": "清晰"}],
    ))
    with pytest.raises(ContentStateError):
        service.review(item.id, ReviewCreate(
            decision="reject", actor="operator", note="late", expected_revision_id=item.current_revision.id,
            visual_checks=[],
        ))
    with pytest.raises(ContentStateError):
        service.regenerate(item.id, RegenerateCreate(expected_revision_id=item.current_revision.id))


def test_invalid_model_output_does_not_create_successful_revision(tmp_path: Path) -> None:
    service, opportunity_id, evidence_id = seeded_service(tmp_path)
    product_id = create_product(service, opportunity_id)
    image = add_output_image(service, product_id, tmp_path)
    service.model_adapter.output = {}  # type: ignore[attr-defined]
    with pytest.raises(ContentValidationError):
        service.create_content_item(ContentItemCreate(
            product_id=product_id, opportunity_id=opportunity_id,
            template_key="list-v1", evidence_ids=[evidence_id],
            image_material_ids=[image.id], cover_material_id=image.id,
            research_facts=[{"fact": "真实事实", "evidence_ids": [evidence_id]}],
        ))
    assert service.list_content_items() == []


def test_success_metadata_is_allowlisted_and_does_not_persist_adapter_secrets(tmp_path: Path) -> None:
    service, opportunity_id, evidence_id = seeded_service(tmp_path)
    product_id = create_product(service, opportunity_id)
    image = add_output_image(service, product_id, tmp_path)
    secret = "Bearer top-secret"
    service.model_adapter.provider = secret  # type: ignore[attr-defined]
    service.model_adapter.model = secret  # type: ignore[attr-defined]
    original = service.model_adapter.generate_structured

    def generated(request: object, schema: object):
        result = original(request, schema)
        result.model = secret
        result.raw_evidence = {"token": secret, "attempts": [
            {"attempt": 1, "category": "response_received", "authorization": secret}
        ]}
        result.usage = {"total_tokens": 8, secret: 999}
        return result

    service.model_adapter.generate_structured = generated  # type: ignore[attr-defined,method-assign]
    item = service.create_content_item(ContentItemCreate(
        product_id=product_id, opportunity_id=opportunity_id,
        template_key="list-v1", evidence_ids=[evidence_id],
        image_material_ids=[image.id], cover_material_id=image.id,
        research_facts=[{"fact": "真实事实", "evidence_ids": [evidence_id]}],
    ))
    serialized = json.dumps(item.model_dump(mode="json"), ensure_ascii=False)
    assert secret not in serialized
    assert item.current_revision is not None
    assert item.current_revision.usage == {"total_tokens": 8}
    assert item.current_revision.attempts == [{"attempt": 1, "category": "response_received"}]
