from __future__ import annotations

import io

import pytest
from PIL import Image
from pydantic import ValidationError

from backend.app.adapters.contracts import (
    GeneratedImage,
    ImageGenerationRequest,
    VisionImage,
    VisionRequest,
    VisionResult,
    VisualAssessment,
)


def _png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2, 3), (17, 34, 51)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.mark.parametrize(
    "payload",
    [
        {"prompt": "draw a book", "prompt_version": "media-v1", "model": "arbitrary"},
        {"prompt": "draw a book", "prompt_version": "media-v1", "endpoint": "https://evil.test"},
        {"prompt": "draw a book", "prompt_version": "media-v1", "path": "C:/out.png"},
    ],
)
def test_image_request_cannot_override_trusted_provider_configuration(payload: dict[str, str]) -> None:
    """Adding provider-owned fields to a request must remain a validation error."""
    with pytest.raises(ValidationError):
        ImageGenerationRequest.model_validate(payload)


def test_vision_request_binds_each_managed_material_to_one_image() -> None:
    """A mismatched ID/image list could analyze bytes under the wrong material identity."""
    with pytest.raises(ValidationError, match="material"):
        VisionRequest(
            prompt="inspect",
            prompt_version="vision-v1",
            material_ids=["material-1", "material-2"],
            images=[VisionImage(material_id="material-1", mime_type="image/png", data=_png_bytes())],
        )


def test_visual_assessment_and_result_reject_unknown_or_unbounded_facts() -> None:
    """Provider extras and enormous usage counters must not enter persisted facts."""
    assessment = {
        "summary": "clear cover",
        "plan_match": True,
        "text_readability": "readable",
        "defects": [],
        "safety_issues": [],
        "suggestions": [],
    }
    with pytest.raises(ValidationError):
        VisualAssessment.model_validate({**assessment, "approved": True})
    with pytest.raises(ValidationError):
        VisionResult.model_validate(
            {
                "model": "qwen-vl-max",
                "output": assessment,
                "raw_evidence": {"provider": "alibaba_bailian"},
                "usage": {"tokens": 1_000_000_001},
            }
        )


def test_generated_image_requires_metadata_that_matches_real_bytes() -> None:
    """A result cannot claim PNG dimensions or digest without the corresponding bytes."""
    png = _png_bytes()
    with pytest.raises(ValidationError):
        GeneratedImage(
            data=png,
            mime_type="image/png",
            width=99,
            height=3,
            sha256="0" * 64,
            provider_request_id="request-1",
        )
