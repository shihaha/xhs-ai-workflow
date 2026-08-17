import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from backend.app.adapters.bailian import (
    BailianAuthenticationError,
    BailianModelAdapter,
    BailianNotConfigured,
    BailianRetryExhausted,
)
from backend.app.adapters.contracts import StructuredModelRequest
from backend.app.adapters.contracts import ModelAdapterError, ModelResult
from backend.app.db import Database
from backend.app.features.analysis.schemas import AnalysisCreate
from backend.app.features.analysis.service import AnalysisService
from backend.app.features.radar.models import RankItemRecord, RankSnapshotRecord
from backend.app.features.analysis.schemas import AnalysisOutput
from backend.app.main import create_app
from backend.app.settings import Settings


def _request() -> StructuredModelRequest:
    return StructuredModelRequest(
        system_prompt="json",
        user_prompt="facts",
        prompt_version="v1",
        evidence_ids=["artifact:1"],
    )


def test_missing_key_is_explicit_and_never_sent() -> None:
    adapter = BailianModelAdapter(api_key=None)
    with pytest.raises(BailianNotConfigured):
        adapter.generate_structured(_request(), AnalysisOutput)


def test_rate_limit_retries_bounded_then_succeeds() -> None:
    calls = 0
    payload = json.dumps(
        {
            "claims": [{"claim": "observed fact", "evidence_ids": ["artifact:1"]}],
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


def test_http_408_is_retried_as_transient() -> None:
    calls = 0
    payload = json.dumps(
        {
            "claims": [{"claim": "fact", "evidence_ids": ["artifact:1"]}],
            "product_clusters": [],
            "opportunities": [],
        }
    )

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(408, json={"error": {"message": "timeout"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": payload}}]})

    adapter = BailianModelAdapter(
        api_key="secret", max_attempts=2, backoff_seconds=0,
        transport=httpx.MockTransport(handler),
    )
    adapter.generate_structured(_request(), AnalysisOutput)
    assert calls == 2


def test_absurd_usage_integer_is_a_structured_failure_not_overflow() -> None:
    payload = json.dumps(
        {
            "claims": [{"claim": "fact", "evidence_ids": ["artifact:1"]}],
            "product_clusters": [],
            "opportunities": [],
        }
    )
    adapter = BailianModelAdapter(
        api_key="secret",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": payload}}],
                    "usage": {"total_tokens": 10**1000},
                },
            )
        ),
    )
    from backend.app.adapters.bailian import ModelOutputInvalid

    with pytest.raises(ModelOutputInvalid):
        adapter.generate_structured(_request(), AnalysisOutput)


def _rank_evidence(database: Database) -> str:
    with database.session() as session:
        snapshot = RankSnapshotRecord(
            source_date="2026-08-17",
            collected_at="2026-08-17T12:00:00",
            board="成交榜",
            dimension="优秀账号",
            source_url="https://example.com/rank",
            raw_evidence={"source": "test"},
            submitted_count=1,
        )
        item = RankItemRecord(
            stable_key="note:n1",
            rank_no=1,
            user_id="account-a",
            source_url="https://example.com/n1",
            raw_evidence={"source": "test"},
        )
        snapshot.items.append(item)
        session.add(snapshot)
        session.commit()
        return f"rank-item:{item.id}"


class _FailingProvider:
    configured = True
    provider = "replacement_provider"
    model = "replacement-model"

    def __init__(self, error: Exception) -> None:
        self.error = error

    def generate_structured(self, request: object, schema: object) -> ModelResult:
        raise self.error


class _SuccessfulProvider:
    configured = True
    provider = "replacement_provider"
    model = "replacement-model"

    def __init__(self, raw_evidence: object) -> None:
        self.raw_evidence = raw_evidence

    def generate_structured(
        self, request: StructuredModelRequest, schema: object
    ) -> ModelResult:
        return ModelResult.model_construct(
            model=self.model,
            output={
                "claims": [
                    {
                        "claim": "persisted fact",
                        "evidence_ids": list(request.evidence_ids),
                    }
                ],
                "product_clusters": [],
                "opportunities": [],
            },
            raw_evidence=self.raw_evidence,
            usage={},
            duration_ms=1,
        )


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (TimeoutError("secret timeout detail"), "model_timeout"),
        (
            ModelAdapterError(
                "safe transport failure",
                category="model_transport_failed",
                attempts=[{"attempt": 1, "category": "network"}],
            ),
            "model_transport_failed",
        ),
    ],
)
def test_provider_neutral_failures_persist_safe_facts(
    tmp_path: Path, error: Exception, category: str
) -> None:
    database = Database(tmp_path / "db.sqlite3")
    evidence_id = _rank_evidence(database)
    adapter = _FailingProvider(error)
    service = AnalysisService(database, adapter, runtime_dir=tmp_path)

    result = service.create(
        AnalysisCreate(
            analysis_type="account_report",
            account_user_id="account-a",
            evidence_ids=[evidence_id],
        )
    )

    assert result.status == "failed"
    assert result.provider == "replacement_provider"
    assert result.model == "replacement-model"
    assert result.error_category == category
    assert "secret" not in (result.error_detail or "")
    database.close()


@pytest.mark.anyio
async def test_protocol_adapter_reporting_unconfigured_returns_503(tmp_path: Path) -> None:
    class Unconfigured:
        configured = False
        provider = "replacement_provider"
        model = "replacement-model"

    runtime = tmp_path / "runtime-unconfigured"
    app = create_app(
        Settings(runtime_dir=runtime, database_path=runtime / "db.sqlite3", bailian_api_key="x")
    )
    adapter = Unconfigured()
    app.state.bailian_adapter = adapter
    app.state.analysis_service.model_adapter = adapter
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
    assert response.status_code == 503


def test_adapter_programming_value_error_is_not_misclassified_as_grounding(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "programming.sqlite3")
    evidence_id = _rank_evidence(database)
    service = AnalysisService(
        database,
        _FailingProvider(ValueError("adapter implementation bug")),
        runtime_dir=tmp_path,
    )

    with pytest.raises(ValueError, match="implementation bug"):
        service.create(
            AnalysisCreate(
                analysis_type="account_report",
                account_user_id="account-a",
                evidence_ids=[evidence_id],
            )
        )
    assert service.list() == []
    database.close()


def test_shared_error_metadata_is_projected_to_safe_bounded_fields(tmp_path: Path) -> None:
    database = Database(tmp_path / "safe-error.sqlite3")
    evidence_id = _rank_evidence(database)
    secret = "Bearer top-secret-token"
    error = ModelAdapterError(
        secret,
        category=secret,
        attempts=[
            {
                "attempt": 1,
                "category": "network",
                "authorization": secret,
                "headers": {"Authorization": secret},
                "body": secret,
                "token": secret,
            },
            {"attempt": -1, "category": secret, "body": secret},
        ] * 50,
    )
    service = AnalysisService(
        database, _FailingProvider(error), runtime_dir=tmp_path
    )

    created = service.create(
        AnalysisCreate(
            analysis_type="account_report",
            account_user_id="account-a",
            evidence_ids=[evidence_id],
        )
    )
    serialized = json.dumps(created.model_dump(mode="json"), ensure_ascii=False)

    assert created.error_category == "model_request_failed"
    assert created.attempts == [{"attempt": 1, "category": "network"}] * 20
    assert secret not in serialized
    database.close()


@pytest.mark.anyio
async def test_shared_error_secret_never_reaches_api_or_database_response(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "safe-api"
    app = create_app(
        Settings(
            runtime_dir=runtime,
            database_path=runtime / "db.sqlite3",
            bailian_api_key="configured",
        )
    )
    evidence_id = _rank_evidence(app.state.database)
    secret = "Bearer api-secret-token"
    adapter = _FailingProvider(
        ModelAdapterError(
            secret,
            category=secret,
            attempts=[
                {"attempt": 1, "category": "network", "authorization": secret}
            ],
        )
    )
    app.state.bailian_adapter = adapter
    app.state.analysis_service.model_adapter = adapter
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/analyses",
            json={
                "analysis_type": "account_report",
                "account_user_id": "account-a",
                "evidence_ids": [evidence_id],
            },
        )
        listing = await client.get("/api/v1/analyses")

    assert response.status_code == 201
    assert secret not in response.text
    assert secret not in listing.text


@pytest.mark.anyio
async def test_success_attempt_metadata_is_projected_to_safe_bounded_fields(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "safe-success"
    app = create_app(
        Settings(
            runtime_dir=runtime,
            database_path=runtime / "db.sqlite3",
            bailian_api_key="configured",
        )
    )
    evidence_id = _rank_evidence(app.state.database)
    secret = "Bearer success-secret-token"
    safe_attempt = {
        "attempt": 1,
        "category": "network",
        "authorization": secret,
        "headers": {"Authorization": secret},
        "body": {"token": secret},
    }
    adapter = _SuccessfulProvider(
        {
            "attempts": [safe_attempt] * 25
            + [
                {"attempt": -1, "category": "network", "token": secret},
                {"attempt": 2, "category": secret, "token": secret},
            ],
            "headers": {"Authorization": secret},
        }
    )
    app.state.bailian_adapter = adapter
    app.state.analysis_service.model_adapter = adapter

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/analyses",
            json={
                "analysis_type": "account_report",
                "account_user_id": "account-a",
                "evidence_ids": [evidence_id],
            },
        )
        listing = await client.get("/api/v1/analyses")
        fetched = await client.get(f"/api/v1/analyses/{response.json()['id']}")

    expected_attempts = [{"attempt": 1, "category": "network"}] * 20
    assert response.status_code == 201
    assert response.json()["attempts"] == expected_attempts
    assert listing.json()[0]["attempts"] == expected_attempts
    assert fetched.json()["attempts"] == expected_attempts
    with app.state.database.session() as session:
        persisted_attempts = session.scalar(
            text("SELECT attempts_json FROM analyses WHERE id=:analysis_id"),
            {"analysis_id": response.json()["id"]},
        )
    combined = response.text + listing.text + fetched.text + str(persisted_attempts)
    assert secret not in combined


@pytest.mark.anyio
async def test_success_with_non_mapping_raw_evidence_is_safe_and_persists_no_attempts(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "non-mapping-success"
    app = create_app(
        Settings(
            runtime_dir=runtime,
            database_path=runtime / "db.sqlite3",
            bailian_api_key="configured",
        )
    )
    evidence_id = _rank_evidence(app.state.database)
    secret = "Bearer malformed-success-secret"
    adapter = _SuccessfulProvider(secret)
    app.state.bailian_adapter = adapter
    app.state.analysis_service.model_adapter = adapter

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/analyses",
            json={
                "analysis_type": "account_report",
                "account_user_id": "account-a",
                "evidence_ids": [evidence_id],
            },
        )
        listing = await client.get("/api/v1/analyses")

    assert response.status_code == 201
    assert response.json()["attempts"] == []
    assert listing.json()[0]["attempts"] == []
    with app.state.database.session() as session:
        persisted_attempts = session.scalar(text("SELECT attempts_json FROM analyses"))
    assert secret not in response.text + listing.text + str(persisted_attempts)


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


@pytest.mark.anyio
async def test_oversized_evidence_id_is_422_before_sqlite(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    app = create_app(
        Settings(
            runtime_dir=runtime,
            database_path=runtime / "db.sqlite3",
            bailian_api_key="configured",
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/analyses",
            json={
                "analysis_type": "account_report",
                "account_user_id": "account-a",
                "evidence_ids": ["artifact:9223372036854775808"],
            },
        )
    assert response.status_code == 422


@pytest.mark.skipif(not __import__("os").environ.get("BAILIAN_API_KEY"), reason="not_run: BAILIAN_API_KEY unavailable")
def test_live_bailian_contract_opt_in() -> None:
    adapter = BailianModelAdapter(api_key=__import__("os").environ["BAILIAN_API_KEY"])
    result = adapter.generate_structured(_request(), AnalysisOutput)
    assert result.model
