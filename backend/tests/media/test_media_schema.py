from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError

from backend.app.db import Database
from backend.app.features.media.schemas import ContentMediaRunCreate, ContentMediaRunRead
from backend.app.features.media.store import (
    ContentMediaRunStore,
    MediaRunConflict,
)


def _seed_owner(database: Database) -> dict[str, str]:
    ids = {name: str(uuid4()) for name in ("job", "analysis", "opportunity", "product", "item", "revision")}
    now = datetime.now(timezone.utc)
    facts = [{"evidence_id": "rank-item:1", "kind": "rank_item"}]
    fingerprint_payload = {
        "account_scope": ["account-a"],
        "allowed_ids": ["rank-item:1"],
        "facts": facts,
        "trust": [{"evidence_id": "rank-item:1", "row": facts[0]}],
        "artifact_bindings": [],
    }
    trust_fingerprint = hashlib.sha256(json.dumps(
        fingerprint_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    snapshot = {
        "schema_version": 1,
        "trust_fingerprint": trust_fingerprint,
        **fingerprint_payload,
        "input_digest": "a" * 64,
    }
    with database.engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO analyses (id,analysis_type,account_user_id,account_user_ids_json,status,"
            "prompt_version,provider,model,input_digest,evidence_ids_json,evidence_snapshot_json,"
            "output_json,usage_json,duration_ms,attempts_json,error_category,error_detail,created_at) "
            "VALUES (:id,'account_report','account-a','[]','succeeded','v1','test','test',:digest,:evidence,:snapshot,:output,'{}',NULL,'[]',NULL,NULL,:now)"
        ), {
            "id": ids["analysis"], "digest": "a" * 64,
            "evidence": json.dumps(["rank-item:1"]),
            "snapshot": json.dumps(snapshot, ensure_ascii=False),
            "output": json.dumps({
                "claims": [{"claim": "fixture", "evidence_ids": ["rank-item:1"]}],
                "product_clusters": [], "opportunities": [],
            }, ensure_ascii=False),
            "now": now,
        })
        connection.execute(text(
            "INSERT INTO opportunities (id,analysis_id,title,status,summary,evidence_ids_json,next_action,created_at) "
            "VALUES (:id,:analysis,'fixture','观察中','fixture','[]','fixture',:now)"
        ), {"id": ids["opportunity"], "analysis": ids["analysis"], "now": now})
        connection.execute(text(
            "INSERT INTO content_products (id,opportunity_id,name,target_user,created_at) "
            "VALUES (:id,:opportunity,'fixture','fixture',:now)"
        ), {"id": ids["product"], "opportunity": ids["opportunity"], "now": now})
        connection.execute(text(
            "INSERT INTO content_items (id,product_id,opportunity_id,template_key,status,evidence_ids_json,"
            "material_ids_json,image_material_ids_json,cover_material_id,research_facts_json,current_revision_id,created_at,updated_at) "
            "VALUES (:id,:product,:opportunity,'fixture','draft','[]','[]','[]',:cover,'[]',NULL,:now,:now)"
        ), {"id": ids["item"], "product": ids["product"], "opportunity": ids["opportunity"], "cover": str(uuid4()), "now": now})
        connection.execute(text(
            "INSERT INTO content_revisions (id,content_item_id,number,title,body,claims_json,source_evidence_ids_json,"
            "image_plan_json,model_provider,model_name,prompt_version,usage_json,attempts_json,created_at) "
            "VALUES (:id,:item,1,'fixture','fixture','[]','[]','[]','test','test','v1','{}','[]',:now)"
        ), {"id": ids["revision"], "item": ids["item"], "now": now})
        connection.execute(text("UPDATE content_items SET current_revision_id=:revision WHERE id=:item"), ids)
        connection.execute(text(
            "INSERT INTO jobs (id,type,input_data,state,progress_current,progress_total,current_stage,error_category,"
            "retry_count,created_at,updated_at,started_at,completed_at,lease_expires_at) "
            "VALUES (:id,'content_image_generation','{}','queued',0,1,NULL,NULL,0,:now,:now,NULL,NULL,NULL)"
        ), {"id": ids["job"], "now": now})
    return ids


def _request(ids: dict[str, str], **changes: object) -> ContentMediaRunCreate:
    payload: dict[str, object] = {
        "id": str(uuid4()),
        "job_id": ids["job"],
        "owner_product_id": ids["product"],
        "content_item_id": ids["item"],
        "revision_id": ids["revision"],
        "plan_entry_id": "page-1",
        "capability": "generate",
        "provider": "alibaba_bailian",
        "model": "wan2.6-t2i",
        "prompt_version": "content-image-v1",
        "input_digest": "b" * 64,
        "allowed_evidence_ids": ["rank-item:1"],
        "allowed_material_ids": [],
    }
    payload.update(changes)
    return ContentMediaRunCreate.model_validate(payload)


def test_create_and_read_run_persists_only_bounded_trusted_facts(tmp_path: Path) -> None:
    database = Database(tmp_path / "media.sqlite3")
    ids = _seed_owner(database)
    run = ContentMediaRunStore(database).create(_request(ids))

    assert run.status == "queued"
    assert run.owner_product_id == ids["product"]
    assert run.allowed_evidence_ids == ["rank-item:1"]
    assert run.output_material_id is None
    assert run.analysis_artifact_id is None
    database.close()


def test_create_schema_rejects_secret_url_path_and_unknown_fields() -> None:
    base = {
        "id": str(uuid4()), "job_id": str(uuid4()), "owner_product_id": str(uuid4()),
        "content_item_id": str(uuid4()), "revision_id": str(uuid4()),
        "plan_entry_id": "page-1", "capability": "generate", "provider": "bailian",
        "model": "configured", "prompt_version": "v1", "input_digest": "c" * 64,
        "allowed_evidence_ids": [], "allowed_material_ids": [],
    }
    for forbidden in ("api_key", "endpoint", "url", "path"):
        with pytest.raises(ValidationError):
            ContentMediaRunCreate.model_validate({**base, forbidden: "secret-or-location"})


def test_read_schema_rejects_malformed_identity_digest_and_usage() -> None:
    valid = {
        "id": str(uuid4()), "job_id": str(uuid4()),
        "owner_product_id": str(uuid4()), "content_item_id": str(uuid4()),
        "revision_id": str(uuid4()), "plan_entry_id": "page-1",
        "capability": "generate", "status": "queued", "state_version": 0,
        "provider": "bailian", "model": "configured", "prompt_version": "v1",
        "input_digest": "d" * 64, "allowed_evidence_ids": [],
        "allowed_material_ids": [], "output_material_id": None,
        "analysis_artifact_id": None, "usage": {}, "duration_ms": None,
        "attempts": [], "error_category": None, "error_detail": None,
        "lease_token": None, "lease_expires_at": None,
        "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
        "completed_at": None,
    }
    for changes in (
        {"id": "not-a-uuid"}, {"input_digest": "short"},
        {"usage": {"tokens": -1}}, {"allowed_material_ids": ["x", "x"]},
    ):
        with pytest.raises(ValidationError):
            ContentMediaRunRead.model_validate({**valid, **changes})


def test_one_open_generation_per_revision_plan_entry(tmp_path: Path) -> None:
    database = Database(tmp_path / "unique.sqlite3")
    ids = _seed_owner(database)
    store = ContentMediaRunStore(database)
    store.create(_request(ids))

    with pytest.raises(MediaRunConflict):
        store.create(_request(ids, id=str(uuid4())))
    database.close()


def test_claim_and_terminal_transition_use_version_and_lease_cas(tmp_path: Path) -> None:
    database = Database(tmp_path / "cas.sqlite3")
    ids = _seed_owner(database)
    store = ContentMediaRunStore(database)
    queued = store.create(_request(ids))
    claimed = store.claim(
        queued.id,
        expected_version=queued.state_version,
        lease_token=str(uuid4()),
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
    )
    assert claimed.status == "running"
    assert claimed.state_version == 1

    with pytest.raises(MediaRunConflict):
        store.fail(
            claimed.id,
            expected_version=0,
            lease_token=claimed.lease_token,
            error_category="provider_failure",
            error_detail="safe",
        )
    failed = store.fail(
        claimed.id,
        expected_version=claimed.state_version,
        lease_token=claimed.lease_token,
        error_category="provider_failure",
        error_detail="safe",
    )
    assert failed.status == "failed"
    assert failed.completed_at is not None
    assert failed.lease_token is None
    database.close()


def test_expired_running_run_is_recoverable_not_successful(tmp_path: Path) -> None:
    database = Database(tmp_path / "recoverable.sqlite3")
    ids = _seed_owner(database)
    store = ContentMediaRunStore(database)
    queued = store.create(_request(ids))
    running = store.claim(
        queued.id,
        expected_version=0,
        lease_token=str(uuid4()),
        lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )

    recoverable = store.list_recoverable(now=datetime.now(timezone.utc))
    assert [row.id for row in recoverable] == [running.id]
    assert recoverable[0].status == "running"
    database.close()


def test_database_rejects_succeeded_generation_without_output(tmp_path: Path) -> None:
    database = Database(tmp_path / "constraints.sqlite3")
    ids = _seed_owner(database)
    request = _request(ids)
    with pytest.raises(IntegrityError):
        with database.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO content_media_runs (id,job_id,owner_product_id,content_item_id,revision_id,plan_entry_id,"
                "capability,status,state_version,provider,model,prompt_version,input_digest,allowed_evidence_ids_json,"
                "allowed_material_ids_json,output_material_id,analysis_artifact_id,usage_json,duration_ms,attempts_json,"
                "error_category,error_detail,lease_token,lease_expires_at,created_at,updated_at,completed_at) VALUES "
                "(:id,:job,:product,:item,:revision,'page-1','generate','succeeded',1,'bailian','model','v1',:digest,"
                "'[]','[]',NULL,NULL,'{}',1,'[]',NULL,NULL,NULL,NULL,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            ), {"id": request.id, "job": ids["job"], "product": ids["product"], "item": ids["item"], "revision": ids["revision"], "digest": "d" * 64})
    database.close()


def test_generation_success_persists_output_usage_attempts_and_job_state(tmp_path: Path) -> None:
    database = Database(tmp_path / "generation-success.sqlite3")
    ids = _seed_owner(database)
    store = ContentMediaRunStore(database)
    queued = store.create(_request(ids))
    running = store.claim(
        queued.id, expected_version=0, lease_token=str(uuid4()),
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
    )
    material_id = str(uuid4())
    with database.engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO content_product_materials (id,product_id,logical_name,logical_key,version,path,sha256,size_bytes,media_type,kind,created_at) "
            "VALUES (:id,:product,'generated.png','generated.png',1,'content-generated/file.png',:sha,68,'image/png','output_image',CURRENT_TIMESTAMP)"
        ), {"id": material_id, "product": ids["product"], "sha": "e" * 64})

    succeeded = store.succeed_generation(
        running.id, expected_version=running.state_version,
        lease_token=running.lease_token, output_material_id=material_id,
        usage={"images": 1}, duration_ms=25,
        attempts=[{"attempt": 1, "category": "response_received"}],
    )
    assert succeeded.status == "succeeded"
    assert succeeded.output_material_id == material_id
    assert succeeded.usage == {"images": 1}
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT state FROM jobs WHERE id=:id"), {"id": ids["job"]}).scalar_one() == "succeeded"
    database.close()


def test_analysis_success_requires_own_job_artifact(tmp_path: Path) -> None:
    database = Database(tmp_path / "analysis-success.sqlite3")
    ids = _seed_owner(database)
    with database.engine.begin() as connection:
        connection.execute(text("UPDATE jobs SET type='content_image_analysis' WHERE id=:id"), {"id": ids["job"]})
    store = ContentMediaRunStore(database)
    queued = store.create(_request(ids, capability="analyze", plan_entry_id=None))
    running = store.claim(
        queued.id, expected_version=0, lease_token=str(uuid4()),
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
    )
    with database.engine.begin() as connection:
        artifact_id = connection.execute(text(
            "INSERT INTO job_artifacts (job_id,kind,producer,path,metadata_json,created_at) "
            "VALUES (:job,'content_image_analysis','bailian_vision','evidence/media/assessment.json','{}',CURRENT_TIMESTAMP) RETURNING id"
        ), {"job": ids["job"]}).scalar_one()

    succeeded = store.succeed_analysis(
        running.id, expected_version=running.state_version,
        lease_token=running.lease_token, analysis_artifact_id=artifact_id,
        usage={"input_tokens": 20}, duration_ms=10,
        attempts=[{"attempt": 1, "category": "response_received"}],
    )
    assert succeeded.status == "succeeded"
    assert succeeded.analysis_artifact_id == artifact_id
    assert succeeded.output_material_id is None
    database.close()


def test_lost_commit_ack_returns_only_exact_proven_terminal_result(tmp_path: Path) -> None:
    database = Database(tmp_path / "ack.sqlite3")
    ids = _seed_owner(database)
    store = ContentMediaRunStore(database)
    queued = store.create(_request(ids))
    running = store.claim(
        queued.id, expected_version=0, lease_token=str(uuid4()),
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
    )

    fired = False
    def lose_ack(session: object) -> None:
        nonlocal fired
        if not fired:
            fired = True
            raise SQLAlchemyError("lost acknowledgement")
    event.listen(database.sessions.class_, "after_commit", lose_ack)
    try:
        result = store.fail(
            running.id, expected_version=running.state_version,
            lease_token=running.lease_token,
            error_category="provider_failure", error_detail="safe",
        )
    finally:
        event.remove(database.sessions.class_, "after_commit", lose_ack)
    assert result.status == "failed"
    assert result.error_category == "provider_failure"
    database.close()


def test_lost_create_commit_ack_returns_exact_persisted_run(tmp_path: Path) -> None:
    database = Database(tmp_path / "create-ack.sqlite3")
    ids = _seed_owner(database)
    store = ContentMediaRunStore(database)
    request = _request(ids)
    fired = False

    def lose_ack(session: object) -> None:
        nonlocal fired
        if not fired:
            fired = True
            raise SQLAlchemyError("lost acknowledgement")

    event.listen(database.sessions.class_, "after_commit", lose_ack)
    try:
        result = store.create(request)
    finally:
        event.remove(database.sessions.class_, "after_commit", lose_ack)
    assert result.id == request.id
    assert result.status == "queued"
    assert result.input_digest == request.input_digest
    database.close()


@pytest.mark.parametrize(
    ("method_name", "expected_status", "expects_error"),
    [("needs_human", "needs_human", True), ("cancel", "cancelled", False)],
)
def test_non_success_terminal_states_remain_truthful_and_listable(
    tmp_path: Path, method_name: str, expected_status: str, expects_error: bool,
) -> None:
    database = Database(tmp_path / f"{expected_status}.sqlite3")
    ids = _seed_owner(database)
    store = ContentMediaRunStore(database)
    queued = store.create(_request(ids))
    running = store.claim(
        queued.id, expected_version=0, lease_token=str(uuid4()),
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
    )
    method = getattr(store, method_name)
    kwargs: dict[str, object] = {
        "expected_version": running.state_version,
        "lease_token": running.lease_token,
    }
    if expects_error:
        kwargs.update({"error_category": "media_output_invalid", "error_detail": "safe validation failure"})
    terminal = method(running.id, **kwargs)

    assert terminal.status == expected_status
    assert terminal.output_material_id is None
    assert terminal.analysis_artifact_id is None
    assert [row.id for row in store.list_for_item(ids["item"])] == [terminal.id]
    database.close()


def test_failure_transitions_never_persist_caller_error_text_or_location(tmp_path: Path) -> None:
    hostile_category = "sk-live-secret-C:/runtime/https://evil.invalid"
    hostile_detail = "Bearer sk-live-secret at C:\\Users\\Admin\\key.txt https://evil.invalid"

    for method_name, expected_status, expected_detail in (
        ("fail", "failed", "Media run failed."),
        ("needs_human", "needs_human", "Media run requires operator review."),
    ):
        database = Database(tmp_path / f"sanitized-{method_name}.sqlite3")
        ids = _seed_owner(database)
        store = ContentMediaRunStore(database)
        queued = store.create(_request(ids))
        running = store.claim(
            queued.id, expected_version=0, lease_token=str(uuid4()),
            lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
        )

        terminal = getattr(store, method_name)(
            running.id, expected_version=running.state_version,
            lease_token=running.lease_token,
            error_category=hostile_category, error_detail=hostile_detail,
        )

        assert terminal.status == expected_status
        assert terminal.error_category == "internal_failure"
        assert terminal.error_detail == expected_detail
        with database.engine.connect() as connection:
            persisted = " ".join(connection.execute(text(
                "SELECT error_category,error_detail FROM content_media_runs WHERE id=:id"
            ), {"id": running.id}).one())
        assert "sk-live-secret" not in persisted
        assert "evil.invalid" not in persisted
        assert "Users" not in persisted
        database.close()
