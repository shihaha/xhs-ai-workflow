import json
from pathlib import Path

import httpx
import pytest

from backend.app.adapters.bailian import (
    BailianAuthenticationError,
    BailianModelAdapter,
    BailianNotConfigured,
    BailianRetryExhausted,
    StructuredModelRequest,
)
from backend.app.features.analysis.schemas import AnalysisOutput
from backend.app.main import create_app
from backend.app.settings import Settings


def _request() -> StructuredModelRequest:
    return StructuredModelRequest(
        system_prompt="json", user_prompt="facts", prompt_version="v1", evidence_ids=["x"]
    )


def test_missing_key_is_explicit_and_never_sent() -> None:
    adapter = BailianModelAdapter(api_key=None)
    with pytest.raises(BailianNotConfigured):
        adapter.generate_structured(_request(), AnalysisOutput)


def test_rate_limit_retries_bounded_then_succeeds() -> None:
    calls = 0
    payload = json.dumps(
        {
            "claims": [{"claim": "observed fact", "evidence_ids": ["x"]}],
            "product_clusters": [],
            "opportunities": [],
        }
    )

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(429, json={"error": {"message": "limited"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": payload}}]})

    adapter = BailianModelAdapter(
        api_key="secret", max_attempts=3, backoff_seconds=0,
        transport=httpx.MockTransport(handler),
    )
    adapter.generate_structured(_request(), AnalysisOutput)
    assert calls == 3


def test_timeout_exhaustion_is_bounded() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out", request=request)

    adapter = BailianModelAdapter(
        api_key="secret", max_attempts=2, backoff_seconds=0,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(BailianRetryExhausted) as caught:
        adapter.generate_structured(_request(), AnalysisOutput)
    assert calls == 2
    assert len(caught.value.attempts) == 2


def test_auth_failure_is_not_retried_and_secret_is_redacted() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": {"message": "bad secret"}})

    adapter = BailianModelAdapter(
        api_key="top-secret", max_attempts=3, backoff_seconds=0,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(BailianAuthenticationError) as caught:
        adapter.generate_structured(_request(), AnalysisOutput)
    assert calls == 1
    assert "top-secret" not in str(caught.value)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_unconfigured_api_returns_503_without_success_row(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    app = create_app(Settings(runtime_dir=runtime, database_path=runtime / "db.sqlite3"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/analyses",
            json={
                "analysis_type": "account_report",
                "account_user_id": "account-a",
                "evidence_ids": ["artifact:1"],
            },
        )
        listing = await client.get("/api/v1/analyses")
    assert response.status_code == 503
    assert listing.json() == []
    assert "key" not in response.text.lower() or "not configured" in response.text.lower()


@pytest.mark.skipif(not __import__("os").environ.get("BAILIAN_API_KEY"), reason="not_run: BAILIAN_API_KEY unavailable")
def test_live_bailian_contract_opt_in() -> None:
    adapter = BailianModelAdapter(api_key=__import__("os").environ["BAILIAN_API_KEY"])
    result = adapter.generate_structured(_request(), AnalysisOutput)
    assert result.model
