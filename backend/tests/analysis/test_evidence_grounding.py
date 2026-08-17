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


def _complete_artifact(database: Database, *, complete: bool = True) -> str:
    now = datetime.now(UTC).replace(tzinfo=None)
    result = {
        "status": "succeeded" if complete else "needs_human",
        "expected_count": 1,
        "discovered_count": 1,
        "succeeded_count": 1 if complete else 0,
        "complete": complete,
        "items": [{"id": "p1", "source_url": "https://www.xiaohongshu.com/goods/p1"}],
        "verification": {
            "expected_count": 1,
            "discovered_count": 1,
            "succeeded_count": 1 if complete else 0,
            "missing_count": 0 if complete else 1,
            "overflow_count": 0,
            "issues": [],
            "complete": complete,
        },
    }
    with database.session() as session:
        job = JobRecord(
            type="android_shop_collection",
            input_data={"account_user_id": "account-a"},
            state=JobState.succeeded if complete else JobState.needs_human,
            progress_current=1 if complete else 0,
            progress_total=1,
            created_at=now,
            updated_at=now,
        )
        session.add(job)
        session.flush()
        artifact = JobArtifactRecord(
            job_id=job.id,
            kind="shop_collection_result",
            path=f"evidence/shops/{job.id}/result.json",
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
    service = AnalysisService(database, StubModel(_output(evidence_id)))

    result = service.create(
        AnalysisCreate(
            analysis_type="account_opportunity",
            account_user_id="account-a",
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
    service = AnalysisService(database, model)

    result = service.create(
        AnalysisCreate(
            analysis_type="account_opportunity",
            account_user_id="account-a",
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
    service = AnalysisService(database, model)

    result = service.create(
        AnalysisCreate(
            analysis_type="account_opportunity",
            account_user_id="account-a",
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
    model = StubModel(_output(evidence_id))

    created = AnalysisService(database, model).create(
        AnalysisCreate(
            analysis_type="account_opportunity",
            account_user_id="account-a",
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
    service = AnalysisService(database, StubModel(output))

    result = service.create(
        AnalysisCreate(
            analysis_type="account_opportunity",
            account_user_id="account-a",
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

    assert response.status_code == 200
    assert response.json() == [
        {
            "evidence_id": evidence_id,
            "kind": "shop_collection_result",
            "account_user_id": "account-a",
            "eligible_for_opportunity": True,
        }
    ]
