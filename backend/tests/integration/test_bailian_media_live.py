"""Explicit opt-in Bailian media gate; default outcome is exactly not_run."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import select

from backend.app.adapters.bailian_media import (
    BailianImageGenerationAdapter,
    BailianVisionAdapter,
)
from backend.app.features.media.service import ContentMediaService
from backend.app.models.jobs import JobArtifactRecord
from backend.app.settings import Settings
from backend.tests.media.test_generation_service import _media_service


def _require_live_settings(tmp_path: Path) -> Settings:
    required = (
        "BAILIAN_API_KEY",
        "XHS_BAILIAN_IMAGE_MODEL",
        "XHS_BAILIAN_VISION_MODEL",
    )
    if os.environ.get("BAILIAN_MEDIA_LIVE_TEST") != "1":
        pytest.skip("not_run: BAILIAN_MEDIA_LIVE_TEST=1 was not supplied")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        pytest.skip("not_run: trusted Bailian key/model configuration is incomplete")
    return Settings(runtime_dir=tmp_path, database_path=tmp_path / "live.sqlite3")


def test_missing_live_authority_is_not_run_and_creates_no_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BAILIAN_MEDIA_LIVE_TEST", raising=False)
    monkeypatch.delenv("BAILIAN_API_KEY", raising=False)
    with pytest.raises(pytest.skip.Exception, match="not_run"):
        _require_live_settings(tmp_path)
    assert not (tmp_path / "live.sqlite3").exists()
    assert not (tmp_path / "content-generated").exists()


@pytest.mark.skipif(
    os.environ.get("BAILIAN_MEDIA_LIVE_TEST") != "1",
    reason="not_run: BAILIAN_MEDIA_LIVE_TEST=1 was not supplied",
)
def test_opt_in_live_generation_and_advisory_assessment(tmp_path: Path) -> None:
    settings = _require_live_settings(tmp_path)
    base, item, original = _media_service(tmp_path)
    image = BailianImageGenerationAdapter(
        api_key=settings.bailian_api_key,
        base_url=settings.bailian_image_base_url,
        model=settings.bailian_image_model,
        max_attempts=settings.bailian_image_max_attempts,
        timeout_seconds=settings.bailian_image_timeout_seconds,
        poll_deadline_seconds=settings.bailian_image_poll_deadline_seconds,
        poll_interval_seconds=settings.bailian_image_poll_interval_seconds,
        max_image_bytes=settings.bailian_image_max_bytes,
        max_image_pixels=settings.bailian_image_max_pixels,
    )
    vision = BailianVisionAdapter(
        api_key=settings.bailian_api_key,
        base_url=settings.bailian_vision_base_url,
        model=settings.bailian_vision_model,
        max_attempts=settings.bailian_vision_max_attempts,
        timeout_seconds=settings.bailian_vision_timeout_seconds,
    )
    service = ContentMediaService(
        base.database,
        content_service=base.content_service,
        image_adapter=image,
        vision_adapter=vision,
        runtime_dir=tmp_path,
    )

    generated = service.submit_generation(
        item.id,
        expected_revision_id=item.current_revision.id,
        image_plan_entry_id=original.id,
    )
    generated = service.run_generation(generated.id)
    material = service.get_output_material(generated.id)
    analyzed = service.submit_analysis(
        item.id,
        expected_revision_id=item.current_revision.id,
        material_ids=[material.id],
    )
    analyzed = service.run_analysis(analyzed.id)
    advice = service.get_analysis_assessment(analyzed.id)
    with base.database.session() as session:
        generation_facts = session.scalar(select(JobArtifactRecord).where(
            JobArtifactRecord.job_id == generated.job_id,
            JobArtifactRecord.kind == "generated_image_provenance",
        ))

    assert generated.status == analyzed.status == "succeeded"
    assert material.availability == "available"
    assert material.path.startswith(f"content-generated/{item.id}/{generated.id}/")
    assert generation_facts is not None
    assert generation_facts.metadata_json.get("provider_request_id")
    assert advice.provider_request_id
    assert advice.usage == analyzed.usage
    assert advice.assessment.summary
    assert base.content_service.get_content_item(item.id).status == "review"
