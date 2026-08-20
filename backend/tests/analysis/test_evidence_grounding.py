import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.adapters.contracts import ModelResult
from backend.app.db import Database
from backend.app.features.analysis.models import AnalysisRecord, OpportunityRecord
from backend.app.features.analysis.schemas import AnalysisCreate
from backend.app.features.analysis.service import AnalysisService
from backend.app.models.jobs import JobArtifactRecord, JobRecord, JobState
from backend.app.main import create_app
from backend.app.settings import Settings


class StubModel:
    provider = "stub_provider"
    model = "deepseek-test"

    def __init__(self, output: dict[str, object]) -> None:
        self.output = output
        self.calls = 0

    @property
    def configured(self) -> bool:
        return True

    def generate_structured(self, request: object, schema: object) -> ModelResult:
        self.calls += 1
        return ModelResult(
            model=self.model,
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
            producer="android_shop_worker_v1",
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
        "opportunities": [],
    }


def test_success_persists_only_fully_grounded_account_report(tmp_path: Path) -> None:
    database = Database(tmp_path / "db.sqlite3")
    evidence_id = _complete_artifact(database)
    service = AnalysisService(
        database, StubModel(_output(evidence_id)), runtime_dir=tmp_path
    )

    result = service.create(
        AnalysisCreate(
            analysis_type="account_report",
            account_user_id="account-a",
            evidence_ids=[evidence_id],
        )
    )

    assert result.status == "succeeded"
    assert result.output is not None
    assert service.list_opportunities() == []
    database.close()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE analyses SET evidence_snapshot_json='{}' WHERE id=:analysis_id",
        "UPDATE analyses SET input_digest='ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff' WHERE id=:analysis_id",
        "DELETE FROM analyses WHERE id=:analysis_id",
    ],
    ids=["update-snapshot", "update-binding", "delete"],
)
def test_success_binds_a_physically_immutable_evidence_snapshot(
    tmp_path: Path,
    statement: str,
) -> None:
    database = Database(tmp_path / "db.sqlite3")
    evidence_id = _complete_artifact(database)
    service = AnalysisService(
        database,
        StubModel(_output(evidence_id)),
        runtime_dir=tmp_path,
    )

    created = service.create(
        AnalysisCreate(
            analysis_type="account_report",
            account_user_id="account-a",
            evidence_ids=[evidence_id],
        )
    )
    with database.session() as session:
        row = session.get(AnalysisRecord, created.id)
        assert row is not None
        snapshot = row.evidence_snapshot_json
        assert snapshot is not None
        binding = snapshot["artifact_bindings"][0]
        target = tmp_path / binding["relative_path"]
        assert snapshot["schema_version"] == 1
        assert snapshot["allowed_ids"] == [evidence_id]
        assert snapshot["account_scope"] == ["account-a"]
        assert binding["evidence_id"] == evidence_id
        assert binding["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    with pytest.raises(IntegrityError):
        with database.engine.begin() as connection:
            connection.execute(text(statement), {"analysis_id": created.id})
    assert service.get(created.id).status == "succeeded"
    database.close()


def test_status_downgrade_preserves_sealed_snapshot_across_restart(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "db.sqlite3"
    database = Database(database_path)
    evidence_id = _complete_artifact(database)
    service = AnalysisService(
        database,
        StubModel(_output(evidence_id)),
        runtime_dir=tmp_path,
    )
    created = service.create(
        AnalysisCreate(
            analysis_type="account_report",
            account_user_id="account-a",
            evidence_ids=[evidence_id],
        )
    )
    with database.session() as session:
        row = session.get(AnalysisRecord, created.id)
        assert row is not None
        sealed_snapshot = deepcopy(row.evidence_snapshot_json)
        sealed_output = deepcopy(row.output_json)
        row.status = "needs_human"
        session.commit()
    downgraded = service.get(created.id)
    assert downgraded.status == "needs_human"
    assert downgraded.output is None
    assert service.list_opportunities() == []
    database.close()

    reopened = Database(database_path)
    with reopened.session() as session:
        row = session.get(AnalysisRecord, created.id)
        assert row is not None
        assert row.status == "needs_human"
        assert row.evidence_snapshot_json == sealed_snapshot
        assert row.output_json == sealed_output
        assert row.opportunities == []
    reopened.close()


@pytest.mark.parametrize("new_status", ["succeeded", "failed", "invented"])
def test_sealed_non_success_status_is_terminal(
    tmp_path: Path,
    new_status: str,
) -> None:
    database = Database(tmp_path / "db.sqlite3")
    evidence_id = _complete_artifact(database)
    service = AnalysisService(
        database,
        StubModel(_output(evidence_id)),
        runtime_dir=tmp_path,
    )
    created = service.create(
        AnalysisCreate(
            analysis_type="account_report",
            account_user_id="account-a",
            evidence_ids=[evidence_id],
        )
    )
    with database.engine.begin() as connection:
        connection.execute(
            text("UPDATE analyses SET status='needs_human' WHERE id=:analysis_id"),
            {"analysis_id": created.id},
        )
    with pytest.raises(IntegrityError):
        with database.engine.begin() as connection:
            connection.execute(
                text("UPDATE analyses SET status=:status WHERE id=:analysis_id"),
                {"analysis_id": created.id, "status": new_status},
            )
    assert service.get(created.id).status == "needs_human"
    database.close()


def test_opportunity_cannot_be_added_to_non_success_analysis(tmp_path: Path) -> None:
    database = Database(tmp_path / "db.sqlite3")
    evidence_id = _complete_artifact(database)
    service = AnalysisService(
        database,
        StubModel(_output(evidence_id)),
        runtime_dir=tmp_path,
    )
    created = service.create(
        AnalysisCreate(
            analysis_type="account_report",
            account_user_id="account-a",
            evidence_ids=[evidence_id],
        )
    )
    with database.engine.begin() as connection:
        connection.execute(
            text("UPDATE analyses SET status='failed' WHERE id=:analysis_id"),
            {"analysis_id": created.id},
        )
    with pytest.raises(IntegrityError):
        with database.session() as session:
            session.add(OpportunityRecord(
                analysis_id=created.id,
                title="must stay historical",
                status="观察中",
                summary="not active",
                evidence_ids_json=[evidence_id],
                next_action="none",
                created_at=datetime.now(UTC).replace(tzinfo=None),
            ))
            session.commit()
    database.close()


def _recompute_snapshot_fingerprint(snapshot: dict[str, object]) -> None:
    payload = {
        "account_scope": snapshot["account_scope"],
        "allowed_ids": snapshot["allowed_ids"],
        "facts": snapshot["facts"],
        "trust": snapshot["trust"],
        "artifact_bindings": snapshot["artifact_bindings"],
    }
    snapshot["trust_fingerprint"] = hashlib.sha256(json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _insert_success_clone(
    database: Database,
    source_id: str,
    snapshot: dict[str, object],
) -> None:
    with database.session() as session:
        source = session.get(AnalysisRecord, source_id)
        assert source is not None
        values = {
            "id": str(uuid4()),
            "analysis_type": source.analysis_type,
            "account_user_id": source.account_user_id,
            "account_user_ids_json": json.dumps(source.account_user_ids_json),
            "status": "succeeded",
            "prompt_version": source.prompt_version,
            "provider": source.provider,
            "model": source.model,
            "input_digest": source.input_digest,
            "evidence_ids_json": json.dumps(source.evidence_ids_json),
            "evidence_snapshot_json": json.dumps(snapshot, ensure_ascii=False),
            "output_json": json.dumps(source.output_json, ensure_ascii=False),
            "usage_json": json.dumps(source.usage_json),
            "duration_ms": source.duration_ms,
            "attempts_json": json.dumps(source.attempts_json),
            "error_category": source.error_category,
            "error_detail": source.error_detail,
            "created_at": source.created_at,
        }
    with database.engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO analyses ("
            "id, analysis_type, account_user_id, account_user_ids_json, status, "
            "prompt_version, provider, model, input_digest, evidence_ids_json, "
            "evidence_snapshot_json, output_json, usage_json, duration_ms, "
            "attempts_json, error_category, error_detail, created_at"
            ") VALUES ("
            ":id, :analysis_type, :account_user_id, :account_user_ids_json, :status, "
            ":prompt_version, :provider, :model, :input_digest, :evidence_ids_json, "
            ":evidence_snapshot_json, :output_json, :usage_json, :duration_ms, "
            ":attempts_json, :error_category, :error_detail, :created_at)"
        ), values)


def _mutate_snapshot(snapshot: dict[str, object], mutation: str) -> None:
    if mutation == "fingerprint":
        snapshot["trust_fingerprint"] = "0" * 64
        return
    if mutation == "fact_alignment":
        snapshot["facts"][0]["evidence_id"] = "artifact:999"  # type: ignore[index]
    elif mutation == "trust_alignment":
        snapshot["trust"][0]["evidence_id"] = "artifact:999"  # type: ignore[index]
    elif mutation == "missing_binding":
        snapshot["artifact_bindings"] = []
    elif mutation == "bool_binding":
        snapshot["artifact_bindings"][0]["artifact_id"] = True  # type: ignore[index]
    elif mutation == "float_binding":
        snapshot["artifact_bindings"][0]["size_bytes"] = 1.0  # type: ignore[index]
    elif mutation == "overflow_binding":
        snapshot["artifact_bindings"][0]["artifact_id"] = 9_223_372_036_854_775_808  # type: ignore[index]
    elif mutation == "parent_ids":
        snapshot["allowed_ids"] = ["artifact:999"]
    else:
        raise AssertionError(mutation)
    _recompute_snapshot_fingerprint(snapshot)


@pytest.mark.parametrize(
    "mutation",
    [
        "fingerprint",
        "fact_alignment",
        "trust_alignment",
        "missing_binding",
        "bool_binding",
        "float_binding",
        "overflow_binding",
        "parent_ids",
    ],
)
def test_runtime_success_insert_uses_complete_snapshot_validator(
    tmp_path: Path,
    mutation: str,
) -> None:
    database = Database(tmp_path / "db.sqlite3")
    evidence_id = _complete_artifact(database)
    service = AnalysisService(
        database,
        StubModel(_output(evidence_id)),
        runtime_dir=tmp_path,
    )
    created = service.create(
        AnalysisCreate(
            analysis_type="account_report",
            account_user_id="account-a",
            evidence_ids=[evidence_id],
        )
    )
    with database.session() as session:
        source = session.get(AnalysisRecord, created.id)
        assert source is not None and source.evidence_snapshot_json is not None
        snapshot = deepcopy(source.evidence_snapshot_json)
    _mutate_snapshot(snapshot, mutation)

    with pytest.raises(IntegrityError):
        _insert_success_clone(database, created.id, snapshot)
    database.close()


def test_runtime_success_update_uses_complete_snapshot_validator(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "db.sqlite3")
    evidence_id = _complete_artifact(database)
    service = AnalysisService(
        database,
        StubModel(_output(evidence_id)),
        runtime_dir=tmp_path,
    )
    created = service.create(
        AnalysisCreate(
            analysis_type="account_report",
            account_user_id="account-a",
            evidence_ids=[evidence_id],
        )
    )
    trigger_names = (
        "ck_analysis_evidence_snapshot_immutable_update",
        "ck_analysis_evidence_snapshot_success_update",
    )
    with database.engine.begin() as connection:
        trigger_sql = {
            name: connection.scalar(text(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=:name"
            ), {"name": name})
            for name in trigger_names
        }
        assert all(isinstance(sql, str) for sql in trigger_sql.values())
        snapshot_json = connection.scalar(text(
            "SELECT evidence_snapshot_json FROM analyses WHERE id=:analysis_id"
        ), {"analysis_id": created.id})
        snapshot = json.loads(snapshot_json)
        snapshot["trust_fingerprint"] = "0" * 64
        for name in trigger_names:
            connection.execute(text(f"DROP TRIGGER {name}"))
        connection.execute(text(
            "UPDATE analyses SET evidence_snapshot_json=:snapshot WHERE id=:analysis_id"
        ), {"snapshot": json.dumps(snapshot), "analysis_id": created.id})
        for sql in trigger_sql.values():
            connection.execute(text(sql))

    with pytest.raises(IntegrityError):
        with database.engine.begin() as connection:
            connection.execute(text(
                "UPDATE analyses SET status='succeeded' WHERE id=:analysis_id"
            ), {"analysis_id": created.id})
    database.close()


def test_success_commit_ack_lost_returns_exact_persisted_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "db.sqlite3")
    evidence_id = _complete_artifact(database)
    model = StubModel(_output(evidence_id))
    service = AnalysisService(database, model, runtime_dir=tmp_path)
    real_commit = Session.commit
    raised = False

    def commit_then_raise(session: Session) -> None:
        nonlocal raised
        real_commit(session)
        if not raised:
            raised = True
            raise SQLAlchemyError("secret acknowledgement lost")

    monkeypatch.setattr(Session, "commit", commit_then_raise)
    created = service.create(
        AnalysisCreate(
            analysis_type="account_report",
            account_user_id="account-a",
            evidence_ids=[evidence_id],
        )
    )

    assert created.status == "succeeded"
    assert model.calls == 1
    assert len(service.list()) == 1
    assert service.list_opportunities() == []
    database.close()


def test_success_commit_not_landed_is_explicitly_rolled_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "db.sqlite3")
    evidence_id = _complete_artifact(database)
    model = StubModel(_output(evidence_id))
    service = AnalysisService(database, model, runtime_dir=tmp_path)
    monkeypatch.setattr(
        Session,
        "commit",
        lambda _session: (_ for _ in ()).throw(
            SQLAlchemyError("secret commit rejected")
        ),
    )

    with pytest.raises(Exception) as caught:
        service.create(
            AnalysisCreate(
                analysis_type="account_report",
                account_user_id="account-a",
                evidence_ids=[evidence_id],
            )
        )
    assert str(caught.value) == "analysis_success_commit_rolled_back"
    assert model.calls == 1
    assert service.list() == []
    database.close()


def test_success_commit_partial_or_mismatched_graph_is_transaction_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "db.sqlite3")
    evidence_id = _complete_artifact(database)
    model = StubModel(_output(evidence_id))
    service = AnalysisService(database, model, runtime_dir=tmp_path)
    real_commit = Session.commit
    raised = False

    def commit_mutate_then_raise(session: Session) -> None:
        nonlocal raised
        real_commit(session)
        if not raised:
            raised = True
            with database.engine.begin() as connection:
                connection.execute(text(
                        "INSERT INTO opportunities "
                        "(id,analysis_id,title,status,summary,evidence_ids_json,"
                        "review_status,evidence_level,supporting_accounts_json,"
                        "supporting_products_json,supporting_notes_json,reviewed_at,"
                        "rejection_reason,next_action,created_at) "
                        "SELECT 'unexpected-opportunity',id,'mismatched','升温','bad',"
                        "evidence_ids_json,'pending_review','warming_candidate',"
                        "'[{\"account_user_id\":\"account-a\"},{\"account_user_id\":\"account-b\"}]',"
                        "'[]','[]',NULL,NULL,'none',created_at FROM analyses LIMIT 1"
                ))
            raise SQLAlchemyError("secret ambiguous acknowledgement")

    monkeypatch.setattr(Session, "commit", commit_mutate_then_raise)
    with pytest.raises(Exception) as caught:
        service.create(
            AnalysisCreate(
                analysis_type="account_report",
                account_user_id="account-a",
                evidence_ids=[evidence_id],
            )
        )
    assert str(caught.value) == "analysis_success_transaction_unknown"
    assert model.calls == 1
    database.close()


def test_success_commit_unreadable_fresh_proof_is_transaction_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "db.sqlite3")
    evidence_id = _complete_artifact(database)
    model = StubModel(_output(evidence_id))
    service = AnalysisService(database, model, runtime_dir=tmp_path)
    real_session = database.session

    def reject_commit_and_proof(_session: Session) -> None:
        monkeypatch.setattr(
            database,
            "session",
            lambda: (_ for _ in ()).throw(SQLAlchemyError("secret proof unreadable")),
        )
        raise SQLAlchemyError("secret acknowledgement unreadable")

    monkeypatch.setattr(Session, "commit", reject_commit_and_proof)
    with pytest.raises(Exception) as caught:
        service.create(
            AnalysisCreate(
                analysis_type="account_report",
                account_user_id="account-a",
                evidence_ids=[evidence_id],
            )
        )
    assert str(caught.value) == "analysis_success_transaction_unknown"
    assert model.calls == 1
    monkeypatch.setattr(database, "session", real_session)
    database.close()


def test_success_commit_proof_retries_one_transient_fresh_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "db.sqlite3")
    evidence_id = _complete_artifact(database)
    model = StubModel(_output(evidence_id))
    service = AnalysisService(database, model, runtime_dir=tmp_path)
    real_commit = Session.commit
    real_session = database.session
    proof_calls = 0

    def flaky_fresh_session():
        nonlocal proof_calls
        proof_calls += 1
        if proof_calls == 1:
            raise SQLAlchemyError("transient fresh read")
        return real_session()

    def commit_then_raise(session: Session) -> None:
        real_commit(session)
        monkeypatch.setattr(database, "session", flaky_fresh_session)
        raise SQLAlchemyError("secret acknowledgement lost")

    monkeypatch.setattr(Session, "commit", commit_then_raise)
    created = service.create(
        AnalysisCreate(
            analysis_type="account_report",
            account_user_id="account-a",
            evidence_ids=[evidence_id],
        )
    )
    assert created.status == "succeeded"
    assert proof_calls == 2
    assert model.calls == 1
    monkeypatch.setattr(database, "session", real_session)
    database.close()


def test_shop_artifact_swap_after_final_resolve_cannot_commit_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "db.sqlite3")
    evidence_id = _complete_artifact(database)
    with database.session() as session:
        artifact = session.get(
            JobArtifactRecord,
            int(evidence_id.removeprefix("artifact:")),
        )
        assert artifact is not None
        target = tmp_path / artifact.path
    replacement = target.with_name("replacement-after-final-resolve.json")
    replacement.write_bytes(b"tampered-after-final-resolve")
    service = AnalysisService(
        database,
        StubModel(_output(evidence_id)),
        runtime_dir=tmp_path,
    )
    real_resolve = service._resolve_evidence_in_session
    resolve_count = 0

    def swap_after_final_resolve(session: object, payload: AnalysisCreate):
        nonlocal resolve_count
        resolved = real_resolve(session, payload)
        resolve_count += 1
        if resolve_count == 2:
            replacement.replace(target)
        return resolved

    monkeypatch.setattr(
        service,
        "_resolve_evidence_in_session",
        swap_after_final_resolve,
    )

    created = service.create(
        AnalysisCreate(
            analysis_type="account_report",
            account_user_id="account-a",
            evidence_ids=[evidence_id],
        )
    )

    assert resolve_count == 2
    assert target.read_bytes() == b"tampered-after-final-resolve"
    assert created.status == "needs_human"
    assert created.error_category == "evidence_changed_after_model"
    assert created.output is None
    assert service.list_opportunities() == []
    database.close()


@pytest.mark.parametrize("bad_id", ["artifact:99999", "artifact:2"])
def test_unknown_or_foreign_citation_rejects_whole_output(tmp_path: Path, bad_id: str) -> None:
    database = Database(tmp_path / "db.sqlite3")
    evidence_id = _complete_artifact(database)
    model = StubModel(_output(bad_id))
    service = AnalysisService(database, model, runtime_dir=tmp_path)

    result = service.create(
        AnalysisCreate(
            analysis_type="account_report",
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
    service = AnalysisService(database, model, runtime_dir=tmp_path)

    result = service.create(
        AnalysisCreate(
            analysis_type="account_report",
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
    absolute = tmp_path / "evidence" / "shops"
    result_file = next(absolute.glob("*/result.json"))
    result_file.write_text(
        __import__("json").dumps(result, ensure_ascii=False), encoding="utf-8"
    )
    model = StubModel(_output(evidence_id))

    created = AnalysisService(database, model, runtime_dir=tmp_path).create(
        AnalysisCreate(
            analysis_type="account_report",
            account_user_id="account-a",
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
            analysis_type="account_report",
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
    service = AnalysisService(database, StubModel(output), runtime_dir=tmp_path)

    result = service.create(
        AnalysisCreate(
            analysis_type="account_report",
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
        model = StubModel(_output(evidence_id))
        app.state.bailian_adapter = model
        app.state.analysis_service.model_adapter = model
        substituted = await client.post(
            "/api/v1/analyses",
                json={
                    "analysis_type": "account_report",
                    "account_user_id": "account-a",
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
                analysis_type="account_report",
                account_user_id="account-a",
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
    assert created.status_code == 422


def _replace_result(
    database: Database,
    runtime_dir: Path,
    evidence_id: str,
    result: dict[str, object],
    *,
    expected_count: int | None = None,
) -> None:
    with database.session() as session:
        artifact = session.get(JobArtifactRecord, int(evidence_id.split(":")[1]))
        assert artifact is not None
        if expected_count is not None:
            job_input = dict(artifact.job.input_data)
            job_input["expected_count"] = expected_count
            artifact.job.input_data = job_input
            artifact.job.progress_current = expected_count
            artifact.job.progress_total = expected_count
        artifact.metadata_json = {"result": result}
        path = artifact.path
        session.commit()
    target = runtime_dir / path
    target.write_text(__import__("json").dumps(result, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize("tamper", ["duplicate_item_id", "raw_conservation", "negative_counter"])
def test_historical_shop_result_must_satisfy_full_collection_invariants(
    tmp_path: Path, tamper: str
) -> None:
    database = Database(tmp_path / "db.sqlite3")
    evidence_id = _complete_artifact(database)
    with database.session() as session:
        artifact = session.get(JobArtifactRecord, int(evidence_id.split(":")[1]))
        assert artifact is not None
        result = deepcopy(artifact.metadata_json["result"])
    expected = 1
    if tamper == "duplicate_item_id":
        expected = 2
        second = deepcopy(result["items"][0])
        second["source_url"] = "https://www.xiaohongshu.com/goods/p2"
        result["items"].append(second)
        for container in (result, result["verification"]):
            container["expected_count"] = 2
            container["discovered_count"] = 2
            container["succeeded_count"] = 2
        result["collected_count"] = 2
        result["raw_observation_count"] = 2
    elif tamper == "raw_conservation":
        result["raw_observation_count"] = 99
    else:
        result["duplicate_observation_count"] = -1
    _replace_result(database, tmp_path, evidence_id, result, expected_count=expected)
    model = StubModel(_output(evidence_id, status="已验证"))

    created = AnalysisService(database, model, runtime_dir=tmp_path).create(
        AnalysisCreate(
            analysis_type="account_report",
            account_user_id="account-a",
            evidence_ids=[evidence_id],
        )
    )

    assert created.status == "needs_human"
    assert model.calls == 0
    database.close()


@pytest.mark.parametrize("file_kind", ["invalid_utf8", "oversize"])
def test_untrusted_result_file_is_bounded_and_never_crashes_discovery_or_create(
    tmp_path: Path, file_kind: str
) -> None:
    database = Database(tmp_path / "db.sqlite3")
    evidence_id = _complete_artifact(database)
    with database.session() as session:
        artifact = session.get(JobArtifactRecord, int(evidence_id.split(":")[1]))
        assert artifact is not None
        target = tmp_path / artifact.path
        result = deepcopy(artifact.metadata_json["result"])
        if file_kind == "oversize":
            result["padding"] = "x" * (5 * 1024 * 1024)
            artifact.metadata_json = {"result": result}
        session.commit()
    if file_kind == "invalid_utf8":
        target.write_bytes(b"\xff\xfe\x80")
    else:
        target.write_text(__import__("json").dumps(result), encoding="utf-8")
    model = StubModel(_output(evidence_id))
    service = AnalysisService(database, model, runtime_dir=tmp_path)

    discovered = service.list_evidence(account_user_id="account-a")
    created = service.create(
        AnalysisCreate(
            analysis_type="account_report",
            account_user_id="account-a",
            evidence_ids=[evidence_id],
        )
    )

    assert discovered[0].eligible_for_opportunity is False
    assert created.status == "needs_human"
    assert model.calls == 0
    database.close()


def test_external_artifact_provenance_is_never_trusted(tmp_path: Path) -> None:
    database = Database(tmp_path / "db.sqlite3")
    evidence_id = _complete_artifact(database)
    with database.session() as session:
        artifact = session.get(JobArtifactRecord, int(evidence_id.split(":")[1]))
        assert artifact is not None
        artifact.producer = "external"
        session.commit()
    model = StubModel(_output(evidence_id, status="已验证"))

    created = AnalysisService(database, model, runtime_dir=tmp_path).create(
        AnalysisCreate(
            analysis_type="account_report",
            account_user_id="account-a",
            evidence_ids=[evidence_id],
        )
    )

    assert created.status == "needs_human"
    assert model.calls == 0
    database.close()


def test_duplicate_rejected_references_break_collection_conservation(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "db.sqlite3")
    evidence_id = _complete_artifact(database)
    with database.session() as session:
        artifact = session.get(JobArtifactRecord, int(evidence_id.split(":")[1]))
        assert artifact is not None
        result = deepcopy(artifact.metadata_json["result"])
    duplicate = {
        "reference": "same-card",
        "reason": "duplicate_source_url",
        "raw_evidence": {"source_url": "https://www.xiaohongshu.com/goods/p1"},
    }
    result["rejected_items"] = [duplicate, deepcopy(duplicate)]
    result["rejected_count"] = 2
    result["duplicate_observation_count"] = 2
    result["raw_observation_count"] = 3
    _replace_result(database, tmp_path, evidence_id, result)
    model = StubModel(_output(evidence_id))

    created = AnalysisService(database, model, runtime_dir=tmp_path).create(
        AnalysisCreate(
            analysis_type="account_report",
            account_user_id="account-a",
            evidence_ids=[evidence_id],
        )
    )

    assert created.status == "needs_human"
    assert model.calls == 0
    database.close()


def test_open_handle_identity_rejects_atomic_path_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "db.sqlite3")
    evidence_id = _complete_artifact(database)
    with database.session() as session:
        artifact = session.get(JobArtifactRecord, int(evidence_id.split(":")[1]))
        assert artifact is not None
        target = tmp_path / artifact.path
        replacement_result = deepcopy(artifact.metadata_json["result"])
        replacement_result["detail"] = "replacement"
        artifact.metadata_json = {"result": replacement_result}
        session.commit()
    replacement = target.with_name("replacement.json")
    replacement.write_text(
        __import__("json").dumps(replacement_result, ensure_ascii=False), encoding="utf-8"
    )
    original_open = Path.open
    swapped = False

    def swapping_open(path: Path, *args: object, **kwargs: object):
        nonlocal swapped
        if path == target and not swapped:
            swapped = True
            replacement.replace(target)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", swapping_open)
    service = AnalysisService(database, StubModel(_output(evidence_id)), runtime_dir=tmp_path)

    discovered = service.list_evidence(account_user_id="account-a")

    assert swapped is True
    assert discovered[0].eligible_for_opportunity is False
    database.close()


@pytest.mark.anyio
async def test_public_jobs_api_rejects_reserved_worker_types_and_artifact_kind(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "reserved-api"
    app = create_app(Settings(runtime_dir=runtime, database_path=runtime / "db.sqlite3"))
    evidence_file = runtime / "external.json"
    evidence_file.write_text("{}", encoding="utf-8")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        reserved_job = await client.post(
            "/api/v1/jobs",
            json={"type": "android_shop_collection", "input": {}},
        )
        generic_job = await client.post(
            "/api/v1/jobs", json={"type": "manual_note", "input": {}}
        )
        reserved_artifact = await client.post(
            f"/api/v1/jobs/{generic_job.json()['id']}/artifacts",
            json={
                "kind": "shop_collection_result",
                "path": "external.json",
                "metadata": {},
            },
        )

    assert reserved_job.status_code == 422
    assert generic_job.status_code == 201
    assert reserved_artifact.status_code == 422
