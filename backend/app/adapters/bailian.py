"""Bounded OpenAI-compatible client for Alibaba Cloud Bailian."""

from __future__ import annotations

import json
import math
import time
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.app.adapters.contracts import ModelResult


DEFAULT_BAILIAN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_BAILIAN_TEXT_MODEL = "deepseek-v4-flash"


class StructuredModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system_prompt: str = Field(min_length=1)
    user_prompt: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1, max_length=100)
    evidence_ids: list[str] = Field(min_length=1)


class BailianError(RuntimeError):
    def __init__(self, message: str, *, attempts: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.attempts = attempts or []


class BailianNotConfigured(BailianError):
    pass


class BailianAuthenticationError(BailianError):
    pass


class BailianRetryExhausted(BailianError):
    pass


class BailianRequestFailed(BailianError):
    pass


class ModelOutputInvalid(BailianError):
    pass


class BailianModelAdapter:
    """Call one configured text model without persisting or exposing credentials."""

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str = DEFAULT_BAILIAN_BASE_URL,
        model: str = DEFAULT_BAILIAN_TEXT_MODEL,
        max_attempts: int = 3,
        timeout_seconds: float = 60.0,
        backoff_seconds: float = 0.25,
        transport: httpx.BaseTransport | None = None,
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

    def generate_structured(
        self, request: StructuredModelRequest, schema: type[BaseModel]
    ) -> ModelResult:
        if self._api_key is None:
            raise BailianNotConfigured("Bailian is not configured.")
        attempts: list[dict[str, Any]] = []
        started = time.monotonic()
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "response_format": {"type": "json_object"},
        }
        with httpx.Client(
            timeout=self.timeout_seconds,
            transport=self._transport,
            headers={"Authorization": f"Bearer {self._api_key}"},
        ) as client:
            for attempt_no in range(1, self.max_attempts + 1):
                try:
                    response = client.post(f"{self.base_url}/chat/completions", json=body)
                except httpx.TransportError as error:
                    category = "timeout" if isinstance(error, httpx.TimeoutException) else "network"
                    attempts.append({"attempt": attempt_no, "category": category})
                    if attempt_no == self.max_attempts:
                        raise BailianRetryExhausted(
                            "Bailian transport failed after bounded retries.", attempts=attempts
                        )
                    self._backoff(attempt_no)
                    continue

                if response.status_code in {401, 403}:
                    attempts.append({"attempt": attempt_no, "category": "authentication"})
                    raise BailianAuthenticationError(
                        "Bailian authentication failed.", attempts=attempts
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    attempts.append(
                        {"attempt": attempt_no, "category": f"http_{response.status_code}"}
                    )
                    if attempt_no == self.max_attempts:
                        raise BailianRetryExhausted(
                            "Bailian transient failure exhausted bounded retries.",
                            attempts=attempts,
                        )
                    self._backoff(attempt_no)
                    continue
                if response.status_code >= 400:
                    attempts.append(
                        {"attempt": attempt_no, "category": f"http_{response.status_code}"}
                    )
                    raise BailianRequestFailed(
                        f"Bailian request failed with HTTP {response.status_code}.",
                        attempts=attempts,
                    )
                attempts.append({"attempt": attempt_no, "category": "response_received"})
                return self._validated_result(
                    response, schema=schema, attempts=attempts, started=started
                )
        raise AssertionError("unreachable")

    def _validated_result(
        self,
        response: httpx.Response,
        *,
        schema: type[BaseModel],
        attempts: list[dict[str, Any]],
        started: float,
    ) -> ModelResult:
        try:
            envelope = response.json()
            content = envelope["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("content is not text")
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise TypeError("structured output is not an object")
            validated = schema.model_validate(parsed)
            usage = _validated_usage(envelope.get("usage", {}))
        except (ValueError, TypeError, KeyError, IndexError, ValidationError) as error:
            raise ModelOutputInvalid(
                "Bailian returned invalid structured output.", attempts=attempts
            ) from error
        duration_ms = max(0, round((time.monotonic() - started) * 1000))
        request_id = response.headers.get("x-request-id") or response.headers.get(
            "x-dashscope-request-id"
        )
        return ModelResult(
            model=self.model,
            output=validated.model_dump(mode="json"),
            raw_evidence={
                "provider": "alibaba_bailian",
                "provider_request_id": request_id,
                "attempts": attempts,
            },
            usage=usage,
            duration_ms=duration_ms,
        )

    def _backoff(self, attempt_no: int) -> None:
        if self.backoff_seconds:
            time.sleep(self.backoff_seconds * attempt_no)


def _validated_usage(value: Any) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("usage must be an object")
    result: dict[str, int] = {}
    for key, item in value.items():
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError("usage values must be finite non-negative integers")
        if not math.isfinite(float(item)) or item < 0 or int(item) != item:
            raise ValueError("usage values must be finite non-negative integers")
        result[str(key)] = int(item)
    return result
