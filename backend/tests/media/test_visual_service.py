from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.adapters.contracts import VisionRequest, VisionResult, VisualAssessment
from backend.app.features.media.service import ContentMediaService, MediaValidationError
from backend.app.models.jobs import JobArtifactRecord
from backend.tests.content.test_workflow import (
    add_output_image,
    create_product,
    rebind_product_to_another_valid_opportunity,
)
from backend.tests.media.test_generation_service import (
    FakeImageAdapter,
    _media_service,
)


class FakeVisionAdapter:
    configured = True
    provider = "controlled_vision"
    model = "controlled-vision-v1"

    def analyze_images(self, request: VisionRequest, schema) -> VisionResult:
        output = schema.model_validate({
            "summary": "画面与计划一致",
            "plan_match": True,
            "text_readability": "清晰",
            "defects": [],
            "safety_issues": [],
            "suggestions": ["仍需人工确认"],
        })
        return VisionResult(
            model=self.model,
            output=output,
            raw_evidence={
                "provider": self.provider,
                "provider_request_id": "vision-request-1",
                "attempts": [{"attempt": 1, "category": "response_received"}],
                "prompt_version": request.prompt_version,
                "material_ids": request.material_ids,
            },
            usage={"total_tokens": 9},
            duration_ms=5,
        )


def _visual_service(tmp_path: Path):
    base, item, original = _media_service(tmp_path)
    service = ContentMediaService(
        base.database,
        content_service=base.content_service,
        image_adapter=FakeImageAdapter(),
        vision_adapter=FakeVisionAdapter(),
        runtime_dir=tmp_path,
    )
    return service, item, original


def test_visual_assessment_is_durable_advice_and_never_approves_content(
    tmp_path: Path,
) -> None:
    """Any automatic content approval or unbound assessment must fail this test."""

    service, item, original = _visual_service(tmp_path)
    before = service.content_service.get_content_item(item.id).status
    run = service.submit_analysis(
        item.id,
        expected_revision_id=item.current_revision.id,
        material_ids=[original.id],
    )

    completed = service.run_analysis(run.id)
    assessment = service.get_analysis_assessment(completed.id)

    assert completed.status == "succeeded"
    assert completed.analysis_artifact_id is not None
    assert assessment.assessment.plan_match is True
    assert assessment.material_ids == [original.id]
    assert assessment.model == "controlled-vision-v1"
    assert assessment.run_id == run.id
    assert service.content_service.get_content_item(item.id).status == before == "review"


def test_visual_read_fails_closed_after_managed_material_drift(tmp_path: Path) -> None:
    """Skipping fresh file/hash verification when reading advice must fail this test."""

    service, item, original = _visual_service(tmp_path)
    run = service.submit_analysis(
        item.id,
        expected_revision_id=item.current_revision.id,
        material_ids=[original.id],
    )
    completed = service.run_analysis(run.id)
    material_path = tmp_path / service.content_service.get_product(item.product_id).materials[0].path
    material_path.write_bytes(b"tampered")

    with pytest.raises(MediaValidationError, match="trust|changed"):
        service.get_analysis_assessment(completed.id)


def test_visual_read_fails_closed_after_assessment_fact_tamper(tmp_path: Path) -> None:
    """Removing the durable assessment digest check must fail this test."""

    service, item, original = _visual_service(tmp_path)
    run = service.submit_analysis(
        item.id,
        expected_revision_id=item.current_revision.id,
        material_ids=[original.id],
    )
    completed = service.run_analysis(run.id)
    with service.database.session() as session:
        artifact = session.get(JobArtifactRecord, completed.analysis_artifact_id)
        metadata = dict(artifact.metadata_json)
        metadata["assessment"] = {
            **metadata["assessment"],
            "plan_match": False,
            "summary": "tampered",
        }
        artifact.metadata_json = metadata
        session.commit()

    with pytest.raises(MediaValidationError, match="assessment"):
        service.get_analysis_assessment(completed.id)


def test_visual_submission_rejects_cross_product_material(tmp_path: Path) -> None:
    """Removing the product/content binding must fail this test."""

    service, item, _ = _visual_service(tmp_path)
    foreign_product_id = create_product(service.content_service, item.opportunity_id)
    foreign = add_output_image(
        service.content_service, foreign_product_id, tmp_path, "foreign.png"
    )

    with pytest.raises(MediaValidationError, match="bound"):
        service.submit_analysis(
            item.id,
            expected_revision_id=item.current_revision.id,
            material_ids=[foreign.id],
        )


def test_visual_commit_ack_loss_returns_exact_persisted_assessment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Treating a committed assessment as failure after lost ACK must fail this test."""

    service, item, original = _visual_service(tmp_path)
    run = service.submit_analysis(
        item.id,
        expected_revision_id=item.current_revision.id,
        material_ids=[original.id],
    )
    real_commit = Session.commit
    fired = False

    def commit_then_raise(session: Session) -> None:
        nonlocal fired
        if not fired and any(
            isinstance(value, JobArtifactRecord)
            and value.kind == "visual_assessment"
            for value in session.identity_map.values()
        ):
            fired = True
            real_commit(session)
            raise SQLAlchemyError("lost acknowledgement")
        real_commit(session)

    monkeypatch.setattr(Session, "commit", commit_then_raise)
    completed = service.run_analysis(run.id)

    assert fired is True
    assert completed.status == "succeeded"
    assert service.get_analysis_assessment(run.id).assessment.plan_match is True


def test_visual_read_rejects_content_trust_drift(tmp_path: Path) -> None:
    """Reading old advice after content/product trust drift must fail this test."""

    service, item, original = _visual_service(tmp_path)
    run = service.submit_analysis(
        item.id,
        expected_revision_id=item.current_revision.id,
        material_ids=[original.id],
    )
    service.run_analysis(run.id)
    rebind_product_to_another_valid_opportunity(
        service.content_service, item.product_id, item.opportunity_id
    )

    with pytest.raises(MediaValidationError, match="trust"):
        service.get_analysis_assessment(run.id)
