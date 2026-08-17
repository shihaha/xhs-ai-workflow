from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from backend.app.adapters.contracts import ModelResult
from backend.app.db import Database
from backend.app.features.analysis.models import AnalysisRecord, OpportunityRecord
from backend.app.features.analysis.schemas import AnalysisCreate
from backend.app.features.analysis.service import AnalysisService
from backend.app.models.jobs import JobArtifactRecord, JobRecord, JobState
from backend.app.main import create_app
from backend.app.settings import Settings


class StubModel:
    def __init__(self, output: dict[str, object]) -> None:
        self.output = output
        self.calls = 0

    @property
    def configured(self) -> bool:
        return True

    def generate_structured(self, request: object, schema: object) -> ModelResult:
        self.calls += 1
        return ModelResult(
            model="deepseek-test",
            output=self.output,
            raw_evidence={"provider_request_id": "req-1"},
            usage={"total_tokens": 10},
            duration_ms=2,
        )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _shop_result(job_id: str, *, complete: bool = True) -> dict[str, object]:
    source_url = "https://www.xiaohongshu.com/goods/p1"
    return {
        "job_id": job_id,
        "status": "succeeded" if complete else "needs_human",
        "detail": None if complete else "product_evidence_verification_failed",
        "selector_profile_version": "xhs-shop-v1",
        "expected_count": 1,
        "discovered_count": 1,
        "collected_count": 1,
        "raw_observation_count": 1,
        "duplicate_observation_count": 0,
        "succeeded_count": 1 if complete else 0,
        "missing_count": 0 if complete else 1,
        "missing_items": [] if complete else [
            {
                "reference": source_url,
                "reason": "image_manifest_incomplete",
                "raw_evidence": {"product_dir": "01_product"},
            }
        ],
        "collection_missing_count": 0,
        "collection_missing_items": [],
        "rejected_count": 0,
        "rejected_items": [],
        "overflow_count": 0,
        "evidence_artifacts": [],
        "items": [
            {
                "id": "p1",
                "kind": "shop_product",
                "source_url": source_url,
                "raw_evidence": {"detail": "persisted"},
                "data": {},
            }
        ],
        "verification": {
            "expected_count": 1,
            "discovered_count": 1,
            "succeeded_count": 1 if complete else 0,
            "missing_count": 0 if complete else 1,
            "missing_items": [] if complete else [
                {
                    "reference": source_url,
                    "reason": "image_manifest_incomplete",
                    "product_dir": "01_product",
                }
            ],
            "overflow_count": 0,
            "issues": [],
            "complete": complete,
        },
        "complete": complete,
    }


def _complete_artifact(
    database: Database,
    *,
    complete: bool = True,
    account_user_id: str | None = "account-a",
    write_file: bool = True,
) -> str:
    now = datetime.now(UTC).replace(tzinfo=None)
    with database.session() as session:
        job = JobRecord(
            type="android_shop_collection",
            input_data={"account_user_id": account_user_id, "expected_count": 1},
            state=JobState.succeeded if complete else JobState.needs_human,
            progress_current=1 if complete else 0,
            progress_total=1,
            current_stage="shop_complete" if complete else "shop_needs_human",
            created_at=now,
            updated_at=now,
        )
        session.add(job)
        session.flush()
        result = _shop_result(job.id, complete=complete)
        relative_path = f"evidence/shops/{job.id}/result.json"
        if write_file:
            absolute_path = database.database_path.parent / relative_path
            absolute_path.parent.mkdir(parents=True, exist_ok=True)
            absolute_path.write_text(
                __import__("json").dumps(result, ensure_ascii=False), encoding="utf-8"
            )
        artifact = JobArtifactRecord(
            job_id=job.id,
            kind="shop_collection_result",
            path=relative_path,
            metadata_json={"result": result},
            created_at=now,
        )
        session.add(artifact)
        session.commit()
        return f"artifact:{artifact.id}"


def _output(evidence_id: str, *, status: str = "升温") -> dict[str, object]:
    return {
        "claims": [{"claim": "出现可复用方向", "evidence_ids": [evidence_id]}],
        "product_clusters": [
            {"name": "资料产品", "summary": "同类交付", "evidence_ids": [evidence_id]}
        ],
        "opportunities": [
            {
                "title": "资料方向",
                "status": status,
                "summary": "证据支持继续观察",
                "evidence_ids": [evidence_id],
                "next_action": "人工检查商品图后决定是否测试",
            }
        ],
    }


def test_success_persists_only_fully_grounded_output(tmp_path: Path) -> None:
    database = Database(tmp_path / "db.sqlite3")
    evidence_id = _complete_artifact(database)
    service = AnalysisService(
        database, StubModel(_output(evidence_id)), runtime_dir=tmp_path
    )

    result = service.create(
        AnalysisCreate(
            analysis_type="account_opportunity",
            account_user_ids=["account-a"],
            evidence_ids=[evidence_id],
        )
    )

    assert result.status == "succeeded"
    assert result.output is not None
    opportunities = service.list_opportunities()
    assert len(opportunities) == 1
    assert opportunities[0].evidence_ids == [evidence_id]
    database.close()


@pytest.mark.parametrize("bad_id", ["artifact:99999", "artifact:2"])
def test_unknown_or_foreign_citation_rejects_whole_output(tmp_path: Path, bad_id: str) -> None:
    database = Database(tmp_path / "db.sqlite3")
    evidence_id = _complete_artifact(database)
    model = StubModel(_output(bad_id))
    service = AnalysisService(database, model, runtime_dir=tmp_path)

    result = service.create(
        AnalysisCreate(
            analysis_type="account_opportunity",
            account_user_ids=["account-a"],
            evidence_ids=[evidence_id],
        )
    )

    assert result.status == "failed"
    assert result.output is None
    assert result.error_category == "evidence_grounding_failed"
    with database.session() as session:
        assert session.scalars(select(OpportunityRecord)).all() == []
    database.close()


def test_incomplete_deep_verification_never_calls_model_or_persists_opportunity(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "db.sqlite3")
    evidence_id = _complete_artifact(database, complete=False)
    model = StubModel(_output(evidence_id, status="已验证"))
    service = AnalysisService(database, model, runtime_dir=tmp_path)

    result = service.create(
        AnalysisCreate(
            analysis_type="account_opportunity",
            account_user_ids=["account-a"],
            evidence_ids=[evidence_id],
        )
    )

    assert result.status == "needs_human"
    assert result.output is None
    assert result.error_category == "deep_verification_incomplete"
    assert model.calls == 0
    assert service.list_opportunities() == []
    database.close()


def test_zero_product_completion_is_not_opportunity_evidence(tmp_path: Path) -> None:
    database = Database(tmp_path / "db.sqlite3")
    evidence_id = _complete_artifact(database)
    with database.session() as session:
        artifact = session.get(JobArtifactRecord, int(evidence_id.split(":")[1]))
        assert artifact is not None
        result = deepcopy(artifact.metadata_json["result"])
        for container in (result, result["verification"]):
            container["expected_count"] = 0
            container["discovered_count"] = 0
            container["succeeded_count"] = 0
        result["items"] = []
        artifact.metadata_json = {"result": result}
        session.commit()
    absolute = tmp_path / "evidence" / "shops"
    result_file = next(absolute.glob("*/result.json"))
    result_file.write_text(
        __import__("json").dumps(result, ensure_ascii=False), encoding="utf-8"
    )
    model = StubModel(_output(evidence_id))

    created = AnalysisService(database, model, runtime_dir=tmp_path).create(
        AnalysisCreate(
            analysis_type="account_opportunity",
            account_user_ids=["account-a"],
            evidence_ids=[evidence_id],
        )
    )

    assert created.status == "needs_human"
    assert model.calls == 0
    database.close()


def test_contradictory_complete_file_cannot_hide_collection_missing_items(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "db.sqlite3")
    evidence_id = _complete_artifact(database)
    with database.session() as session:
        artifact = session.get(JobArtifactRecord, int(evidence_id.split(":")[1]))
        assert artifact is not None
        result = deepcopy(artifact.metadata_json["result"])
        result["collection_missing_count"] = 1
        result["collection_missing_items"] = [
            {
                "reference": "missing-product",
                "reason": "not_collected",
                "raw_evidence": {"source": "test"},
            }
        ]
        artifact.metadata_json = {"result": result}
        session.commit()
    result_file = next((tmp_path / "evidence" / "shops").glob("*/result.json"))
    result_file.write_text(
        __import__("json").dumps(result, ensure_ascii=False), encoding="utf-8"
    )
    model = StubModel(_output(evidence_id, status="已验证"))

    created = AnalysisService(database, model, runtime_dir=tmp_path).create(
        AnalysisCreate(
            analysis_type="account_opportunity",
            account_user_ids=["account-a"],
            evidence_ids=[evidence_id],
        )
    )

    assert created.status == "needs_human"
    assert model.calls == 0
    database.close()


def test_citationless_claim_rejects_entire_result(tmp_path: Path) -> None:
    database = Database(tmp_path / "db.sqlite3")
    evidence_id = _complete_artifact(database)
    output = _output(evidence_id)
    output["claims"] = [{"claim": "无来源结论", "evidence_ids": []}]
    service = AnalysisService(database, StubModel(output), runtime_dir=tmp_path)

    result = service.create(
        AnalysisCreate(
            analysis_type="account_opportunity",
            account_user_ids=["account-a"],
            evidence_ids=[evidence_id],
        )
    )

    assert result.status == "failed"
    assert result.output is None
    with database.session() as session:
        assert len(session.scalars(select(AnalysisRecord)).all()) == 1
        assert session.scalars(select(OpportunityRecord)).all() == []
    database.close()


@pytest.mark.anyio
async def test_evidence_endpoint_exposes_ids_required_to_create_analysis(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    app = create_app(Settings(runtime_dir=runtime, database_path=runtime / "db.sqlite3"))
    evidence_id = _complete_artifact(app.state.database)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/v1/analysis-evidence", params={"account_user_id": "account-a"}
        )
        model = StubModel(_output(evidence_id))
        app.state.bailian_adapter = model
        app.state.analysis_service.model_adapter = model
        substituted = await client.post(
            "/api/v1/analyses",
            json={
                "analysis_type": "account_opportunity",
                "account_user_ids": ["account-a"],
                "evidence_ids": [evidence_id],
            },
        )

    assert response.status_code == 200
    assert response.json() == [
        {
            "evidence_id": evidence_id,
            "kind": "shop_collection_result",
            "account_user_id": "account-a",
            "eligible_for_opportunity": True,
        }
    ]
    assert substituted.status_code == 201
    assert substituted.json()["status"] == "succeeded"


def test_analysis_scope_is_explicit_and_typed() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AnalysisCreate(
            analysis_type="account_report",
            evidence_ids=["artifact:1"],
        )
    with pytest.raises(ValidationError):
        AnalysisCreate(
            analysis_type="account_report",
            account_user_id="account-a",
            account_user_ids=["account-b"],
            evidence_ids=["artifact:1"],
        )

    cross_account = AnalysisCreate(
        analysis_type="account_opportunity",
        account_user_ids=["account-a", "account-b"],
        evidence_ids=["artifact:1", "artifact:2"],
    )
    assert cross_account.account_user_ids == ["account-a", "account-b"]


@pytest.mark.parametrize(
    "evidence_id",
    [
        "artifact:0",
        "artifact:01",
        "artifact:+1",
        "artifact:１",
        "artifact:9223372036854775808",
        "rank-item:0001",
    ],
)
def test_input_evidence_ids_require_canonical_sqlite_identity(evidence_id: str) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AnalysisCreate(
            analysis_type="account_report",
            account_user_id="account-a",
            evidence_ids=[evidence_id],
        )


def test_every_evidence_must_have_nonempty_matching_account_scope(tmp_path: Path) -> None:
    database = Database(tmp_path / "db.sqlite3")
    unowned = _complete_artifact(database, account_user_id=None)
    service = AnalysisService(database, StubModel(_output(unowned)), runtime_dir=tmp_path)

    with pytest.raises(ValueError, match="account"):
        service.create(
            AnalysisCreate(
                analysis_type="account_opportunity",
                account_user_ids=["account-a"],
                evidence_ids=[unowned],
            )
        )
    database.close()


@pytest.mark.anyio
async def test_public_jobs_api_forgery_is_never_opportunity_eligible(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    app = create_app(
        Settings(
            runtime_dir=runtime,
            database_path=runtime / "db.sqlite3",
            bailian_api_key="configured-for-stub",
        )
    )
    forged_path = runtime / "unrelated.json"
    forged_path.write_text("{}", encoding="utf-8")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/api/v1/jobs",
            json={
                "type": "android_shop_collection",
                "input": {"account_user_id": "account-a", "expected_count": 1},
                "progress_total": 1,
            },
        )
        job_id = created.json()["id"]
        assert (await client.post(f"/api/v1/jobs/{job_id}/claim")).status_code == 200
        assert (
            await client.post(
                f"/api/v1/jobs/{job_id}/transition",
                json={"state": "succeeded", "progress_current": 1, "progress_total": 1},
            )
        ).status_code == 200
        result = _shop_result(job_id)
        attached = await client.post(
            f"/api/v1/jobs/{job_id}/artifacts",
            json={
                "kind": "shop_collection_result",
                "path": "unrelated.json",
                "metadata": {"result": result},
            },
        )
        assert attached.status_code == 201
        evidence = await client.get(
            "/api/v1/analysis-evidence", params={"account_user_id": "account-a"}
        )
        forged_id = evidence.json()[0]["evidence_id"]
        assert evidence.json()[0]["eligible_for_opportunity"] is False

        model = StubModel(_output(forged_id, status="已验证"))
        app.state.bailian_adapter = model
        app.state.analysis_service.model_adapter = model
        analysis = await client.post(
            "/api/v1/analyses",
            json={
                "analysis_type": "account_opportunity",
                "account_user_ids": ["account-a"],
                "evidence_ids": [forged_id],
            },
        )

    assert analysis.status_code == 201
    assert analysis.json()["status"] == "needs_human"
    assert model.calls == 0
