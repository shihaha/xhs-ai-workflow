from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.adapters.contracts import GeneratedImage, ImageGenerationRequest
from backend.app.features.content.models import ArtifactCleanupRecord, ProductMaterialRecord
from backend.app.models.jobs import JobArtifactRecord
from backend.app.features.content.schemas import ContentItemCreate, ResearchFact
from backend.app.features.media.service import ContentMediaService
from backend.tests.content.test_workflow import (
    add_output_image,
    create_product,
    seeded_service,
)


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class FakeImageAdapter:
    configured = True
    provider = "controlled_image"
    model = "controlled-image-v1"

    def generate_images(self, request: ImageGenerationRequest) -> list[GeneratedImage]:
        return [GeneratedImage(
            data=PNG_1X1,
            mime_type="image/png",
            width=1,
            height=1,
            sha256=hashlib.sha256(PNG_1X1).hexdigest(),
            provider_request_id="generated-request-1",
            usage={"images": 1},
            duration_ms=7,
            raw_evidence={
                "provider": self.provider,
                "provider_request_id": "generated-request-1",
                "attempts": [{"attempt": 1, "category": "response_received"}],
                "prompt_version": request.prompt_version,
            },
        )]


class UnusedVisionAdapter:
    configured = True
    provider = "controlled_vision"
    model = "controlled-vision-v1"

    def analyze_images(self, request, schema):  # pragma: no cover - wrong path
        raise AssertionError("vision adapter must not be called by generation")


def _media_service(tmp_path: Path) -> tuple[ContentMediaService, object, object]:
    content, opportunity_id, evidence_id = seeded_service(tmp_path)
    product_id = create_product(content, opportunity_id)
    original = add_output_image(content, product_id, tmp_path)
    item = content.create_content_item(ContentItemCreate(
        product_id=product_id,
        opportunity_id=opportunity_id,
        template_key="list-v1",
        evidence_ids=[evidence_id],
        material_ids=[],
        image_material_ids=[original.id],
        cover_material_id=original.id,
        research_facts=[ResearchFact(fact="真实需求", evidence_ids=[evidence_id])],
    ))
    service = ContentMediaService(
        content.database,
        content_service=content,
        image_adapter=FakeImageAdapter(),
        vision_adapter=UnusedVisionAdapter(),
        runtime_dir=tmp_path,
    )
    return service, item, original


def test_generated_png_becomes_server_managed_output_with_run_provenance(
    tmp_path: Path,
) -> None:
    """Dropping the run/material/cleanup transaction must fail this test."""

    service, item, original = _media_service(tmp_path)
    run = service.submit_generation(
        item.id,
        expected_revision_id=item.current_revision.id,
        image_plan_entry_id=original.id,
    )

    completed = service.run_generation(run.id)
    material = service.get_output_material(completed.id)

    assert completed.status == "succeeded"
    assert completed.output_material_id == material.id
    assert material.kind == "output_image"
    assert material.media_type == "image/png"
    assert material.sha256 == hashlib.sha256(PNG_1X1).hexdigest()
    assert material.size_bytes == len(PNG_1X1)
    assert material.path == (
        f"content-generated/{item.id}/{run.id}/{material.id}.png"
    )
    assert (tmp_path / material.path).read_bytes() == PNG_1X1

    with service.database.session() as session:
        cleanup = session.query(ArtifactCleanupRecord).filter_by(
            owner_type="material", owner_id=material.id
        ).one()
        assert cleanup.state == "cancelled"
        assert cleanup.relative_path == material.path
        provenance = session.query(JobArtifactRecord).filter_by(
            job_id=completed.job_id, kind="generated_image_provenance"
        ).one()
        assert provenance.metadata_json["width"] == 1
        assert provenance.metadata_json["height"] == 1
        assert provenance.metadata_json["run_id"] == run.id
        assert provenance.metadata_json["material_id"] == material.id


def test_generation_rejects_stale_revision_before_calling_provider(tmp_path: Path) -> None:
    """Removing current-revision binding must fail this test."""

    service, item, original = _media_service(tmp_path)
    from backend.app.features.media.service import MediaStateError

    with pytest.raises(MediaStateError, match="revision"):
        service.submit_generation(
            item.id,
            expected_revision_id="00000000-0000-4000-8000-000000000000",
            image_plan_entry_id=original.id,
        )


def test_generation_commit_ack_loss_returns_only_exact_persisted_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing exact post-commit proof must fail this test."""

    service, item, original = _media_service(tmp_path)
    run = service.submit_generation(
        item.id,
        expected_revision_id=item.current_revision.id,
        image_plan_entry_id=original.id,
    )
    real_commit = Session.commit
    fired = False

    def commit_then_raise(session: Session) -> None:
        nonlocal fired
        if not fired and any(
            isinstance(value, ProductMaterialRecord)
            and value.path.startswith("content-generated/")
            for value in session.identity_map.values()
        ):
            fired = True
            real_commit(session)
            raise SQLAlchemyError("lost acknowledgement")
        real_commit(session)

    monkeypatch.setattr(Session, "commit", commit_then_raise)
    completed = service.run_generation(run.id)

    assert fired is True
    assert completed.status == "succeeded"
    assert service.get_output_material(run.id).availability == "available"


def test_generation_database_failure_retains_file_for_cleanup_not_direct_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adding request-path unlink or losing the cleanup fact must fail this test."""

    service, item, original = _media_service(tmp_path)
    run = service.submit_generation(
        item.id,
        expected_revision_id=item.current_revision.id,
        image_plan_entry_id=original.id,
    )

    def fail_finalization(*args, **kwargs) -> None:
        raise SQLAlchemyError("forced finalization failure")

    monkeypatch.setattr(service, "_finish_run_in_session", fail_finalization)
    with pytest.raises(Exception, match="transaction outcome"):
        service.run_generation(run.id)

    with service.database.session() as session:
        cleanup = session.query(ArtifactCleanupRecord).filter_by(
            reason="generated_image_transaction_unknown", state="pending"
        ).one()
        assert (tmp_path / cleanup.relative_path).read_bytes() == PNG_1X1
        assert session.query(ProductMaterialRecord).filter_by(
            id=cleanup.owner_id
        ).one_or_none() is None


def test_generation_write_failure_persists_failed_run_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leaving a write failure stuck in running must fail this test."""

    import backend.app.features.media.service as media_module

    service, item, original = _media_service(tmp_path)
    run = service.submit_generation(
        item.id,
        expected_revision_id=item.current_revision.id,
        image_plan_entry_id=original.id,
    )
    monkeypatch.setattr(
        media_module,
        "write_contained_atomic",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("write failed")),
    )

    with pytest.raises(OSError, match="write failed"):
        service.run_generation(run.id)

    assert service.get_run(run.id).status == "failed"


def test_generated_material_read_rejects_provenance_tamper(tmp_path: Path) -> None:
    """Removing generated provenance sealing must fail this test."""

    service, item, original = _media_service(tmp_path)
    run = service.submit_generation(
        item.id,
        expected_revision_id=item.current_revision.id,
        image_plan_entry_id=original.id,
    )
    completed = service.run_generation(run.id)
    with service.database.session() as session:
        artifact = session.query(JobArtifactRecord).filter_by(
            job_id=completed.job_id, kind="generated_image_provenance"
        ).one()
        metadata = dict(artifact.metadata_json)
        metadata["width"] = 999
        artifact.metadata_json = metadata
        session.commit()

    from backend.app.features.media.service import MediaValidationError
    with pytest.raises(MediaValidationError, match="provenance"):
        service.get_output_material(run.id)
