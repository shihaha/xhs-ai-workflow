from __future__ import annotations

import io
import json
from collections.abc import Callable

import httpx
import pytest
from PIL import Image

from backend.app.adapters.bailian_media import (
    BailianAuthenticationError,
    BailianImageGenerationAdapter,
    BailianMediaOutputInvalid,
    BailianMediaRetryExhausted,
    BailianVisionAdapter,
)
from backend.app.adapters.contracts import (
    ImageGenerationRequest,
    VisionImage,
    VisionRequest,
    VisualAssessment,
)


def _png_bytes(size: tuple[int, int] = (2, 3)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (17, 34, 51)).save(buffer, format="PNG")
    return buffer.getvalue()


def _vision_request() -> VisionRequest:
    return VisionRequest(
        prompt="Inspect only the supplied managed image.",
        prompt_version="vision-v1",
        material_ids=["material-1"],
        images=[
            VisionImage(
                material_id="material-1", mime_type="image/png", data=_png_bytes()
            )
        ],
    )


def _assessment_json() -> str:
    return json.dumps(
        {
            "summary": "clear cover",
            "plan_match": True,
            "text_readability": "readable",
            "defects": [],
            "safety_issues": [],
            "suggestions": ["human review remains required"],
        }
    )


def test_vision_uses_only_configured_model_and_returns_advisory_assessment() -> None:
    """The adapter must ignore no caller model because the contract exposes none."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={"x-request-id": "vision-request-1"},
            json={
                "choices": [{"message": {"content": _assessment_json()}}],
                "usage": {"total_tokens": 12},
            },
        )

    adapter = BailianVisionAdapter(
        api_key="top-secret",
        base_url="https://trusted.example/compatible-mode/v1",
        model="qwen-vl-max",
        transport=httpx.MockTransport(handler),
    )

    result = adapter.analyze_images(_vision_request(), VisualAssessment)

    body = json.loads(seen[0].content)
    assert seen[0].url == "https://trusted.example/compatible-mode/v1/chat/completions"
    assert body["model"] == "qwen-vl-max"
    assert body["messages"][0]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert result.output.plan_match is True
    assert result.raw_evidence["provider_request_id"] == "vision-request-1"
    assert "top-secret" not in json.dumps(result.model_dump(mode="json"))


@pytest.mark.parametrize("status", [401, 403])
def test_media_authentication_failure_is_sanitized(status: int) -> None:
    """Neither the credential nor provider body may escape an auth failure."""
    secret = "credential-sentinel"
    transport = httpx.MockTransport(
        lambda _: httpx.Response(status, text=f"provider leaked {secret}")
    )
    adapter = BailianVisionAdapter(api_key=secret, transport=transport)

    with pytest.raises(BailianAuthenticationError) as caught:
        adapter.analyze_images(_vision_request(), VisualAssessment)

    serialized = str(caught.value) + json.dumps(caught.value.attempts)
    assert secret not in serialized
    assert "provider leaked" not in serialized
    assert caught.value.attempts == [{"attempt": 1, "category": "authentication"}]


@pytest.mark.parametrize("status", [429, 500])
def test_transient_http_failures_stop_at_configured_attempt_bound(status: int) -> None:
    """A persistent transient response must not produce an unbounded call loop."""
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"message": "do not persist me"})

    adapter = BailianVisionAdapter(
        api_key="secret",
        max_attempts=2,
        backoff_seconds=0,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(BailianMediaRetryExhausted) as caught:
        adapter.analyze_images(_vision_request(), VisualAssessment)

    assert calls == 2
    assert caught.value.attempts == [
        {"attempt": 1, "category": f"http_{status}"},
        {"attempt": 2, "category": f"http_{status}"},
    ]


def test_transport_timeout_is_bounded_and_truthful() -> None:
    """Timeout retries must terminate and record only their safe category."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("secret provider detail", request=request)

    adapter = BailianVisionAdapter(
        api_key="secret",
        max_attempts=2,
        backoff_seconds=0,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(BailianMediaRetryExhausted) as caught:
        adapter.analyze_images(_vision_request(), VisualAssessment)

    assert calls == 2
    assert caught.value.attempts[-1]["category"] == "timeout"
    assert "secret provider detail" not in str(caught.value)


def test_vision_rejects_invalid_provider_schema() -> None:
    """A model-provided approve flag or missing fields cannot become visual advice."""
    response = httpx.Response(
        200,
        json={"choices": [{"message": {"content": '{"approved": true}'}}]},
    )
    adapter = BailianVisionAdapter(
        api_key="secret", transport=httpx.MockTransport(lambda _: response)
    )

    with pytest.raises(BailianMediaOutputInvalid):
        adapter.analyze_images(_vision_request(), VisualAssessment)


def _image_transport(image_response: Callable[[], httpx.Response]) -> httpx.MockTransport:
    poll_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "request_id": "submit-1",
                    "output": {"task_id": "task-1", "task_status": "PENDING"},
                },
            )
        if str(request.url).endswith("/tasks/task-1"):
            poll_count += 1
            if poll_count == 1:
                return httpx.Response(
                    200, json={"output": {"task_id": "task-1", "task_status": "RUNNING"}}
                )
            return httpx.Response(
                200,
                headers={"x-request-id": "final-request-1"},
                json={
                    "request_id": "final-request-1",
                    "output": {
                        "task_id": "task-1",
                        "task_status": "SUCCEEDED",
                        "results": [{"url": "https://result.example/generated.png"}],
                    },
                    "usage": {"image_count": 1},
                },
            )
        if str(request.url) == "https://result.example/generated.png":
            return image_response()
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    return httpx.MockTransport(handler)


def test_image_generation_polls_bounded_task_and_validates_real_png() -> None:
    """A successful task is not successful output until its downloaded bytes decode."""
    png = _png_bytes()
    submitted_bodies: list[dict[str, object]] = []

    base_transport = _image_transport(
        lambda: httpx.Response(200, headers={"content-type": "image/png"}, content=png)
    )

    def record_submission(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            submitted_bodies.append(json.loads(request.content))
        return base_transport.handle_request(request)

    adapter = BailianImageGenerationAdapter(
        api_key="secret",
        base_url="https://trusted.example/api/v1",
        model="wan2.6-t2i",
        poll_interval_seconds=0,
        transport=httpx.MockTransport(record_submission),
    )

    images = adapter.generate_images(
        ImageGenerationRequest(prompt="draw a book", prompt_version="image-v1")
    )

    assert len(images) == 1
    assert images[0].data == png
    assert (images[0].mime_type, images[0].width, images[0].height) == (
        "image/png",
        2,
        3,
    )
    assert images[0].provider_request_id == "final-request-1"
    assert images[0].usage == {"image_count": 1}
    assert submitted_bodies == [
        {
            "model": "wan2.6-t2i",
            "input": {"prompt": "draw a book"},
            "parameters": {"size": "1280*1280", "n": 1},
        }
    ]


@pytest.mark.parametrize(
    ("content_type", "content"),
    [
        ("text/html", b"<html>not an image</html>"),
        ("application/json", b'{"error":"not an image"}'),
        ("image/png", b"\x89PNG\r\n\x1a\nnot-a-real-png"),
    ],
)
def test_image_generation_rejects_html_json_and_fake_png(
    content_type: str, content: bytes
) -> None:
    """Provider success with non-image bytes must not become generated output."""
    adapter = BailianImageGenerationAdapter(
        api_key="secret",
        poll_interval_seconds=0,
        transport=_image_transport(
            lambda: httpx.Response(200, headers={"content-type": content_type}, content=content)
        ),
    )

    with pytest.raises(BailianMediaOutputInvalid):
        adapter.generate_images(
            ImageGenerationRequest(prompt="draw a book", prompt_version="image-v1")
        )


def test_image_generation_rejects_byte_and_pixel_limits() -> None:
    """Oversized downloads and decoded pixel counts must remain bounded."""
    png = _png_bytes((4, 4))
    byte_adapter = BailianImageGenerationAdapter(
        api_key="secret",
        poll_interval_seconds=0,
        max_image_bytes=len(png) - 1,
        transport=_image_transport(
            lambda: httpx.Response(200, headers={"content-type": "image/png"}, content=png)
        ),
    )
    pixel_adapter = BailianImageGenerationAdapter(
        api_key="secret",
        poll_interval_seconds=0,
        max_image_pixels=15,
        transport=_image_transport(
            lambda: httpx.Response(200, headers={"content-type": "image/png"}, content=png)
        ),
    )

    for adapter in (byte_adapter, pixel_adapter):
        with pytest.raises(BailianMediaOutputInvalid):
            adapter.generate_images(
                ImageGenerationRequest(prompt="draw a book", prompt_version="image-v1")
            )


def test_generated_image_download_retries_transient_response_without_credential() -> None:
    """A transient result host failure must retry without forwarding the Bailian key."""
    png = _png_bytes()
    download_calls = 0

    def image_response() -> httpx.Response:
        nonlocal download_calls
        download_calls += 1
        if download_calls == 1:
            return httpx.Response(429, text="retry")
        return httpx.Response(200, headers={"content-type": "image/png"}, content=png)

    base_transport = _image_transport(image_response)

    def reject_download_credential(request: httpx.Request) -> httpx.Response:
        if request.url.host == "result.example":
            assert "authorization" not in request.headers
        return base_transport.handle_request(request)

    adapter = BailianImageGenerationAdapter(
        api_key="credential-sentinel",
        max_attempts=2,
        poll_interval_seconds=0,
        backoff_seconds=0,
        transport=httpx.MockTransport(reject_download_credential),
    )

    images = adapter.generate_images(
        ImageGenerationRequest(prompt="draw a book", prompt_version="image-v1")
    )

    assert download_calls == 2
    assert images[0].data == png
