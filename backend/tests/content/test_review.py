from pathlib import Path
import json

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.adapters.contracts import ModelAdapterError
from backend.app.features.content.schemas import ContentItemCreate, RegenerateCreate, ReviewCreate
from backend.app.features.content.service import (
    ContentModelFailure,
    ContentStateError,
    ContentValidationError,
)
from backend.tests.content.test_workflow import (
    CallbackModel,
    add_output_image,
    create_product,
    rebind_product_to_another_valid_opportunity,
    seeded_service,
)


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
    assert [review.outcome for review in approved.reviews] == ["succeeded", "succeeded", "succeeded"]
    assert all(review.error_category is None for review in approved.reviews)


def test_regenerate_trust_drift_preserves_failed_attempt_without_new_revision(tmp_path: Path) -> None:
    service, item, image = _draft(tmp_path)
    r1 = item.current_revision.id
    service.review(item.id, ReviewCreate(
        decision="reject", actor="operator", note="标题不够具体", expected_revision_id=r1,
        visual_checks=[{"material_id": image.id, "passed": False, "observation": "标题不具体"}],
    ))
    service.model_adapter = CallbackModel(
        item.current_revision.source_evidence_ids[0],
        lambda: rebind_product_to_another_valid_opportunity(
            service, item.product_id, item.opportunity_id,
        ),
    )

    with pytest.raises(ContentValidationError, match="product.*opportunity"):
        service.regenerate(item.id, RegenerateCreate(expected_revision_id=r1))

    failed = service.get_content_item(item.id)
    assert failed.status == "rejected"
    assert len(failed.revisions) == 1
    assert [review.decision for review in failed.reviews] == ["reject", "regenerate"]
    assert failed.reviews[1].outcome == "failed"
    assert failed.reviews[1].error_category == "trust_changed"
    assert failed.reviews[1].note == "Regeneration reserved from the rejected revision."


def test_regenerate_model_error_preserves_failed_attempt(tmp_path: Path) -> None:
    service, item, image = _draft(tmp_path)
    r1 = item.current_revision.id
    service.review(item.id, ReviewCreate(
        decision="reject", actor="operator", note="标题不够具体", expected_revision_id=r1,
        visual_checks=[{"material_id": image.id, "passed": False, "observation": "标题不具体"}],
    ))

    def model_error(request: object, schema: object):
        raise ModelAdapterError("provider secret must not persist", category="network")

    service.model_adapter.generate_structured = model_error  # type: ignore[attr-defined,method-assign]

    with pytest.raises(ContentModelFailure):
        service.regenerate(item.id, RegenerateCreate(expected_revision_id=r1))

    failed = service.get_content_item(item.id)
    assert failed.status == "rejected"
    assert len(failed.revisions) == 1
    assert failed.reviews[-1].decision == "regenerate"
    assert failed.reviews[-1].outcome == "failed"
    assert failed.reviews[-1].error_category == "model_failure"
    assert "secret" not in json.dumps(failed.model_dump(mode="json"))


def test_regenerate_success_transaction_failure_records_failed_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, item, image = _draft(tmp_path)
    r1 = item.current_revision.id
    service.review(item.id, ReviewCreate(
        decision="reject", actor="operator", note="标题不够具体", expected_revision_id=r1,
        visual_checks=[{"material_id": image.id, "passed": False, "observation": "标题不具体"}],
    ))
    real_commit = Session.commit
    commit_count = 0

    def fail_new_revision_commit(session: Session) -> None:
        nonlocal commit_count
        commit_count += 1
        if commit_count == 2:
            raise SQLAlchemyError("forced regeneration transaction failure")
        real_commit(session)

    monkeypatch.setattr(Session, "commit", fail_new_revision_commit)

    with pytest.raises(SQLAlchemyError, match="forced regeneration"):
        service.regenerate(item.id, RegenerateCreate(expected_revision_id=r1))

    result = service.get_content_item(item.id)
    assert result.status == "rejected"
    assert len(result.revisions) == 1
    assert result.reviews[-1].outcome == "failed"
    assert result.reviews[-1].error_category == "transaction_unknown"


def test_regenerate_reservation_commit_ack_landed_continues_exact_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, item, image = _draft(tmp_path)
    r1 = item.current_revision.id
    service.review(item.id, ReviewCreate(
        decision="reject", actor="operator", note="标题不够具体", expected_revision_id=r1,
        visual_checks=[{"material_id": image.id, "passed": False, "observation": "标题不具体"}],
    ))
    real_commit = Session.commit
    raised = False

    def commit_then_raise(session: Session) -> None:
        nonlocal raised
        if not raised:
            raised = True
            real_commit(session)
            raise SQLAlchemyError("ambiguous reservation acknowledgement")
        real_commit(session)

    monkeypatch.setattr(Session, "commit", commit_then_raise)

    regenerated = service.regenerate(
        item.id, RegenerateCreate(expected_revision_id=r1),
    )

    assert regenerated.status == "review"
    assert len(regenerated.revisions) == 2
    assert regenerated.reviews[-1].decision == "regenerate"
    assert regenerated.reviews[-1].outcome == "succeeded"


def test_regenerate_reservation_commit_not_landed_preserves_rejected_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, item, image = _draft(tmp_path)
    r1 = item.current_revision.id
    service.review(item.id, ReviewCreate(
        decision="reject", actor="operator", note="标题不够具体", expected_revision_id=r1,
        visual_checks=[{"material_id": image.id, "passed": False, "observation": "标题不具体"}],
    ))
    real_commit = Session.commit
    raised = False

    def reject_once(session: Session) -> None:
        nonlocal raised
        if not raised:
            raised = True
            raise SQLAlchemyError("reservation did not land")
        real_commit(session)

    monkeypatch.setattr(Session, "commit", reject_once)

    with pytest.raises(SQLAlchemyError, match="did not land"):
        service.regenerate(item.id, RegenerateCreate(expected_revision_id=r1))

    result = service.get_content_item(item.id)
    assert result.status == "rejected"
    assert len(result.revisions) == 1
    assert [review.decision for review in result.reviews] == ["reject"]


def test_regenerate_reservation_commit_unknown_restores_without_pending_deadlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, item, image = _draft(tmp_path)
    r1 = item.current_revision.id
    service.review(item.id, ReviewCreate(
        decision="reject", actor="operator", note="标题不够具体", expected_revision_id=r1,
        visual_checks=[{"material_id": image.id, "passed": False, "observation": "标题不具体"}],
    ))
    real_commit = Session.commit
    raised = False

    def commit_mutate_attempt_then_raise(session: Session) -> None:
        nonlocal raised
        if not raised:
            raised = True
            real_commit(session)
            with service.database.engine.begin() as connection:
                connection.exec_driver_sql(
                    "UPDATE content_reviews SET outcome='failed', "
                    "error_category='transaction_unknown' WHERE decision='regenerate'"
                )
            raise SQLAlchemyError("contradictory reservation acknowledgement")
        real_commit(session)

    monkeypatch.setattr(Session, "commit", commit_mutate_attempt_then_raise)

    with pytest.raises(ContentStateError, match="reservation_transaction_unknown"):
        service.regenerate(item.id, RegenerateCreate(expected_revision_id=r1))

    result = service.get_content_item(item.id)
    assert result.status == "rejected"
    assert len(result.revisions) == 1
    assert result.reviews[-1].outcome == "failed"
    assert result.reviews[-1].error_category == "transaction_unknown"


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
