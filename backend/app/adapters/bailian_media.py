"""Bounded Bailian visual-understanding and asynchronous image clients."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ValidationError

from backend.app.adapters.contracts import (
    GeneratedImage,
    ImageGenerationRequest,
    ModelAdapterError,
    VisionRequest,
    VisionResult,
    VisualAssessment,
)
from backend.app.adapters.bailian import _validated_usage


DEFAULT_BAILIAN_VISION_BASE_URL = (
    "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
DEFAULT_BAILIAN_IMAGE_BASE_URL = "https://dashscope.aliyuncs.com/api/v1"
DEFAULT_BAILIAN_VISION_MODEL = "qwen-vl-max"
DEFAULT_BAILIAN_IMAGE_MODEL = "wan2.6-t2i"
_MAX_PROVIDER_JSON_BYTES = 1024 * 1024
_TERMINAL_FAILURE_STATUSES = {"FAILED", "UNKNOWN", "CANCELED", "CANCELLED"}
_PENDING_STATUSES = {
    "PENDING",
    "RUNNING",
    "PRE-PROCESSING",
    "POST-PROCESSING",
    "SUSPENDED",
}
_MIME_BY_FORMAT = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


class BailianMediaError(ModelAdapterError):
    category = "media_request_failed"

    def __init__(self, message: str, *, attempts: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message, category=self.category, attempts=attempts)


class BailianMediaNotConfigured(BailianMediaError):
    category = "media_unconfigured"


class BailianAuthenticationError(BailianMediaError):
    category = "media_authentication_failed"


class BailianMediaRetryExhausted(BailianMediaError):
    category = "media_retry_exhausted"


class BailianMediaRequestFailed(BailianMediaError):
    category = "media_request_failed"


class BailianMediaOutputInvalid(BailianMediaError):
    category = "media_output_invalid"


class _BailianMediaClient:
    provider = "alibaba_bailian"

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str,
        model: str,
        max_attempts: int,
        timeout_seconds: float,
        backoff_seconds: float,
        transport: httpx.BaseTransport | None,
    ) -> None:
        self._api_key = api_key.strip() if api_key and api_key.strip() else None
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_attempts = max(1, max_attempts)
        self.timeout_seconds = timeout_seconds
        self.backoff_seconds = max(0, backoff_seconds)
        self._transport = transport

    @property
    def configured(self) -> bool:
        return self._api_key is not None

    def _authorized_client(self) -> httpx.Client:
        if self._api_key is None:
            raise BailianMediaNotConfigured("Bailian media capability is not configured.")
        return httpx.Client(
            timeout=self.timeout_seconds,
            transport=self._transport,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )

    def _request(
        self,
        client: httpx.Client,
        method: str,
        url: str,
        *,
        attempts: list[dict[str, Any]],
        json_body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        for attempt_no in range(1, self.max_attempts + 1):
            try:
                response = client.request(
                    method, url, json=json_body, headers=extra_headers
                )
            except httpx.TransportError as error:
                category = "timeout" if isinstance(error, httpx.TimeoutException) else "network"
                attempts.append({"attempt": attempt_no, "category": category})
                if attempt_no == self.max_attempts:
                    raise BailianMediaRetryExhausted(
                        "Bailian media transport failed after bounded retries.",
                        attempts=attempts,
                    ) from None
                self._backoff(attempt_no)
                continue
            if response.status_code in {401, 403}:
                attempts.append({"attempt": attempt_no, "category": "authentication"})
                raise BailianAuthenticationError(
                    "Bailian media authentication failed.", attempts=attempts
                )
            if response.status_code in {408, 429} or response.status_code >= 500:
                attempts.append(
                    {"attempt": attempt_no, "category": f"http_{response.status_code}"}
                )
                if attempt_no == self.max_attempts:
                    raise BailianMediaRetryExhausted(
                        "Bailian media transient failure exhausted bounded retries.",
                        attempts=attempts,
                    )
                self._backoff(attempt_no)
                continue
            if response.status_code >= 400:
                attempts.append(
                    {"attempt": attempt_no, "category": f"http_{response.status_code}"}
                )
                raise BailianMediaRequestFailed(
                    f"Bailian media request failed with HTTP {response.status_code}.",
                    attempts=attempts,
                )
            attempts.append({"attempt": attempt_no, "category": "response_received"})
            return response
        raise AssertionError("unreachable")

    def _backoff(self, attempt_no: int) -> None:
        if self.backoff_seconds:
            time.sleep(self.backoff_seconds * attempt_no)


class BailianVisionAdapter(_BailianMediaClient):
    """Analyze supplied image bytes with one configured Bailian vision model."""

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str = DEFAULT_BAILIAN_VISION_BASE_URL,
        model: str = DEFAULT_BAILIAN_VISION_MODEL,
        max_attempts: int = 3,
        timeout_seconds: float = 60.0,
        backoff_seconds: float = 0.25,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            model=model,
            max_attempts=max_attempts,
            timeout_seconds=timeout_seconds,
            backoff_seconds=backoff_seconds,
            transport=transport,
        )

    def analyze_images(
        self, request: VisionRequest, schema: type[BaseModel]
    ) -> VisionResult:
        attempts: list[dict[str, Any]] = []
        started = time.monotonic()
        content: list[dict[str, Any]] = [{"type": "text", "text": request.prompt}]
        content.extend(
            {
                "type": "image_url",
                "image_url": {
                    "url": (
                        f"data:{image.mime_type};base64,"
                        + base64.b64encode(image.data).decode("ascii")
                    )
                },
            }
            for image in request.images
        )
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "response_format": {"type": "json_object"},
        }
        with self._authorized_client() as client:
            response = self._request(
                client,
                "POST",
                f"{self.base_url}/chat/completions",
                attempts=attempts,
                json_body=body,
            )
        try:
            envelope = _response_json(response)
            raw_content = envelope["choices"][0]["message"]["content"]
            if not isinstance(raw_content, str):
                raise TypeError("content is not text")
            parsed = json.loads(raw_content)
            validated = schema.model_validate(parsed)
            assessment = VisualAssessment.model_validate(
                validated.model_dump(mode="json")
            )
            usage = _validated_usage(envelope.get("usage", {}))
        except (ValueError, TypeError, KeyError, IndexError, ValidationError) as error:
            raise BailianMediaOutputInvalid(
                "Bailian returned invalid visual output.", attempts=attempts
            ) from error
        return VisionResult(
            model=self.model,
            output=assessment,
            raw_evidence={
                "provider": self.provider,
                "provider_request_id": _request_id(response, envelope),
                "attempts": attempts,
                "prompt_version": request.prompt_version,
                "material_ids": request.material_ids,
            },
            usage=usage,
            duration_ms=max(0, round((time.monotonic() - started) * 1000)),
        )


class BailianImageGenerationAdapter(_BailianMediaClient):
    """Create one image through Bailian's bounded asynchronous task API."""

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str = DEFAULT_BAILIAN_IMAGE_BASE_URL,
        model: str = DEFAULT_BAILIAN_IMAGE_MODEL,
        max_attempts: int = 3,
        timeout_seconds: float = 60.0,
        backoff_seconds: float = 0.25,
        poll_deadline_seconds: float = 300.0,
        poll_interval_seconds: float = 3.0,
        max_poll_attempts: int = 120,
        max_image_bytes: int = 20 * 1024 * 1024,
        max_image_pixels: int = 16_777_216,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            model=model,
            max_attempts=max_attempts,
            timeout_seconds=timeout_seconds,
            backoff_seconds=backoff_seconds,
            transport=transport,
        )
        self.poll_deadline_seconds = max(0.001, poll_deadline_seconds)
        self.poll_interval_seconds = max(0, poll_interval_seconds)
        self.max_poll_attempts = max(1, max_poll_attempts)
        self.max_image_bytes = max(1, max_image_bytes)
        self.max_image_pixels = max(1, max_image_pixels)

    def generate_images(self, request: ImageGenerationRequest) -> list[GeneratedImage]:
        attempts: list[dict[str, Any]] = []
        started = time.monotonic()
        body = {
            "model": self.model,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"text": request.prompt}],
                    }
                ]
            },
            "parameters": {"size": "1280*1280", "n": 1},
        }
        with self._authorized_client() as client:
            submitted = self._request(
                client,
                "POST",
                f"{self.base_url}/services/aigc/image-generation/generation",
                attempts=attempts,
                json_body=body,
                extra_headers={"X-DashScope-Async": "enable"},
            )
            try:
                submit_envelope = _response_json(submitted)
                task_id = submit_envelope["output"]["task_id"]
                status = submit_envelope["output"]["task_status"]
                if not isinstance(task_id, str) or not task_id or not isinstance(status, str):
                    raise TypeError("invalid task identity")
            except (ValueError, TypeError, KeyError) as error:
                raise BailianMediaOutputInvalid(
                    "Bailian returned an invalid image task.", attempts=attempts
                ) from error
            deadline = time.monotonic() + self.poll_deadline_seconds
            final_response = submitted
            final_envelope = submit_envelope
            poll_count = 0
            while status != "SUCCEEDED":
                if status in _TERMINAL_FAILURE_STATUSES:
                    raise BailianMediaRequestFailed(
                        "Bailian image task ended without a generated image.",
                        attempts=attempts,
                    )
                if status not in _PENDING_STATUSES:
                    raise BailianMediaOutputInvalid(
                        "Bailian returned an unknown image task status.", attempts=attempts
                    )
                if time.monotonic() >= deadline or poll_count >= self.max_poll_attempts:
                    raise BailianMediaRetryExhausted(
                        "Bailian image task exceeded its bounded polling deadline.",
                        attempts=attempts,
                    )
                if self.poll_interval_seconds:
                    time.sleep(
                        min(self.poll_interval_seconds, max(0, deadline - time.monotonic()))
                    )
                poll_count += 1
                final_response = self._request(
                    client,
                    "GET",
                    f"{self.base_url}/tasks/{task_id}",
                    attempts=attempts,
                )
                try:
                    final_envelope = _response_json(final_response)
                    output = final_envelope["output"]
                    if output.get("task_id", task_id) != task_id:
                        raise ValueError("task identity changed")
                    status = output["task_status"]
                    if not isinstance(status, str):
                        raise TypeError("invalid task status")
                except (ValueError, TypeError, KeyError) as error:
                    raise BailianMediaOutputInvalid(
                        "Bailian returned an invalid image task result.", attempts=attempts
                    ) from error

        try:
            output = final_envelope["output"]
            choices = output["choices"]
            if not isinstance(choices, list) or not choices:
                raise ValueError("expected one generated result")
            image_urls: list[str] = []
            for choice in choices:
                content = choice["message"]["content"]
                if not isinstance(content, list):
                    raise TypeError("generated content must be a list")
                image_urls.extend(
                    item["image"]
                    for item in content
                    if isinstance(item, dict) and "image" in item
                )
            if len(image_urls) != 1:
                raise ValueError("expected one generated image")
            result_url = image_urls[0]
            if not isinstance(result_url, str) or not _safe_https_url(result_url):
                raise ValueError("invalid generated result URL")
            usage = _validated_usage(final_envelope.get("usage", {}))
        except (ValueError, TypeError, KeyError, IndexError) as error:
            raise BailianMediaOutputInvalid(
                "Bailian returned invalid generated image metadata.", attempts=attempts
            ) from error

        image_bytes, mime_type, width, height = self._download_and_validate(result_url)
        request_id = _request_id(final_response, final_envelope)
        duration_ms = max(0, round((time.monotonic() - started) * 1000))
        return [
            GeneratedImage(
                data=image_bytes,
                mime_type=mime_type,
                width=width,
                height=height,
                sha256=hashlib.sha256(image_bytes).hexdigest(),
                provider_request_id=request_id,
                usage=usage,
                duration_ms=duration_ms,
                raw_evidence={
                    "provider": self.provider,
                    "provider_request_id": request_id,
                    "task_id": task_id,
                    "attempts": attempts,
                    "prompt_version": request.prompt_version,
                },
            )
        ]

    def _download_and_validate(self, url: str) -> tuple[bytes, str, int, int]:
        # Deliberately use a credential-free client for provider-owned result URLs.
        download_attempts: list[dict[str, Any]] = []
        chunks: list[bytes] = []
        declared_mime = ""
        with httpx.Client(timeout=self.timeout_seconds, transport=self._transport) as client:
            for attempt_no in range(1, self.max_attempts + 1):
                try:
                    with client.stream("GET", url) as response:
                        if response.status_code in {408, 429} or response.status_code >= 500:
                            download_attempts.append(
                                {
                                    "attempt": attempt_no,
                                    "category": f"http_{response.status_code}",
                                }
                            )
                            if attempt_no == self.max_attempts:
                                raise BailianMediaRetryExhausted(
                                    "Generated image download exhausted bounded retries.",
                                    attempts=download_attempts,
                                )
                            self._backoff(attempt_no)
                            continue
                        if response.status_code >= 400:
                            download_attempts.append(
                                {
                                    "attempt": attempt_no,
                                    "category": f"http_{response.status_code}",
                                }
                            )
                            raise BailianMediaRequestFailed(
                                f"Generated image download failed with HTTP {response.status_code}.",
                                attempts=download_attempts,
                            )
                        declared_mime = response.headers.get("content-type", "").split(
                            ";", 1
                        )[0].lower()
                        if declared_mime not in _MIME_BY_FORMAT.values():
                            raise BailianMediaOutputInvalid(
                                "Generated output did not declare a supported image MIME type."
                            )
                        content_length = response.headers.get("content-length")
                        if (
                            content_length is not None
                            and int(content_length) > self.max_image_bytes
                        ):
                            raise BailianMediaOutputInvalid(
                                "Generated image exceeded the byte limit."
                            )
                        chunks = []
                        total = 0
                        for chunk in response.iter_bytes():
                            total += len(chunk)
                            if total > self.max_image_bytes:
                                raise BailianMediaOutputInvalid(
                                    "Generated image exceeded the byte limit."
                                )
                            chunks.append(chunk)
                    break
                except httpx.TransportError as error:
                    category = (
                        "timeout" if isinstance(error, httpx.TimeoutException) else "network"
                    )
                    download_attempts.append(
                        {"attempt": attempt_no, "category": category}
                    )
                    if attempt_no == self.max_attempts:
                        raise BailianMediaRetryExhausted(
                            "Generated image download exhausted bounded retries.",
                            attempts=download_attempts,
                        ) from None
                    self._backoff(attempt_no)
                except BailianMediaError:
                    raise
                except ValueError:
                    raise BailianMediaRequestFailed(
                        "Generated image download failed.", attempts=download_attempts
                    ) from None
        data = b"".join(chunks)
        try:
            with Image.open(io.BytesIO(data)) as image:
                actual_mime = _MIME_BY_FORMAT.get(image.format or "")
                width, height = image.size
                if actual_mime != declared_mime:
                    raise ValueError("declared MIME does not match bytes")
                if width <= 0 or height <= 0 or width * height > self.max_image_pixels:
                    raise ValueError("pixel limit exceeded")
                image.load()
        except (OSError, ValueError, UnidentifiedImageError) as error:
            raise BailianMediaOutputInvalid(
                "Generated output was not a valid bounded image."
            ) from error
        return data, declared_mime, width, height


def _response_json(response: httpx.Response) -> dict[str, Any]:
    content = response.content
    if len(content) > _MAX_PROVIDER_JSON_BYTES:
        raise ValueError("provider response exceeded the JSON byte limit")
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("provider response must be an object")
    return parsed


def _request_id(response: httpx.Response, envelope: dict[str, Any]) -> str | None:
    value = (
        response.headers.get("x-request-id")
        or response.headers.get("x-dashscope-request-id")
        or envelope.get("request_id")
    )
    return value if isinstance(value, str) and 0 < len(value) <= 500 else None


def _safe_https_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except (ValueError, UnicodeError):
        return False
    return parsed.scheme == "https" and bool(parsed.hostname) and parsed.username is None
