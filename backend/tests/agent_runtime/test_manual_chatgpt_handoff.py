from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from backend.app.agent_runtime.job_binding import (
    AgentJobBindingRecord,
    AgentJobCoordinator,
)
from backend.app.agent_runtime.manual_chatgpt_handoff import (
    HandoffResultContract,
    MANUAL_CHATGPT_REQUIRED,
    MANUAL_CHATGPT_RESULT_READY,
    ManualChatGPTHandoffError,
    ManualChatGPTHandoffRecord,
    ManualChatGPTHandoffService,
)
from backend.app.agent_runtime.persistence import (
    AgentRunRecord,
    AgentRunStore,
    HumanActionRecord,
)
from backend.app.agent_runtime.types import AgentRunState, RunBudget
from backend.app.db import Database
from backend.app.models.jobs import JobState
from backend.app.services.jobs import JobService


HANDOFF_ID = "11111111-1111-4111-8111-111111111111"
HUMAN_ID = "22222222-2222-4222-8222-222222222222"


def _setup(tmp_path: Path):
    runtime = tmp_path / "runtime"
    database = Database(runtime / "workbench.sqlite3")
    jobs = JobService(database, runtime_dir=runtime)
    job = jobs.create(job_type="agent_orchestration", input_data={"case": "handoff"})
    bound = AgentJobCoordinator(database, run_id_factory=lambda: "run-1").claim_and_create_run(
        job.id,
        goal="analyze quality-sensitive evidence",
        budget=RunBudget(max_wall_time_seconds=30),
    )
    service = ManualChatGPTHandoffService(
        database,
        runtime_dir=runtime,
        handoff_id_factory=lambda: HANDOFF_ID,
        human_action_id_factory=lambda: HUMAN_ID,
    )
    contract = HandoffResultContract(
        schema_version="xhs-analysis-v1",
        required_fields=("summary", "recommendations"),
        allow_extra_fields=False,
    )
    handoff = service.request_handoff(
        bound.run_id,
        task={"instruction": "analyze these grounded notes", "count": 20},
        context_refs=["evidence:xhs:note:1", "evidence:xhs:note:2"],
        stage_revision="content-analysis-r3",
        result_contract=contract,
    )
    return runtime, database, jobs, job, bound, service, contract, handoff


def _write_return(
    runtime: Path,
    handoff,
    *,
    handoff_id: str | None = None,
    schema_version: str | None = None,
    input_hash: str | None = None,
    result: dict | None = None,
    extra_envelope: dict | None = None,
) -> Path:
    path = runtime / "external-results" / f"{handoff.handoff_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        "handoff_id": handoff_id or handoff.handoff_id,
        "schema_version": schema_version or handoff.schema_version,
        "input_hash": input_hash or handoff.input_hash,
        "result": result
        if result is not None
        else {"summary": "grounded pattern", "recommendations": ["test first"]},
    }
    if extra_envelope:
        envelope.update(extra_envelope)
    path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    return path


def test_request_handoff_atomically_moves_job_and_run_to_manual_wait_and_writes_package(
    tmp_path: Path,
) -> None:
    runtime, database, jobs, job, bound, _service, contract, handoff = _setup(tmp_path)

    waiting_job = jobs.get(job.id)
    assert waiting_job.state is JobState.needs_human
    assert waiting_job.lease_expires_at is None
    assert waiting_job.current_stage == MANUAL_CHATGPT_REQUIRED
    assert waiting_job.error_category == MANUAL_CHATGPT_REQUIRED

    run = AgentRunStore(database).get_run(bound.run_id)
    assert run.state == AgentRunState.needs_human.value
    assert run.error_category == MANUAL_CHATGPT_REQUIRED

    human = AgentRunStore(database).pending_human_action(bound.run_id)
    assert human is not None
    assert human.id == HUMAN_ID
    assert human.tool_name == "manual_chatgpt"
    assert human.request_json["handoff_id"] == HANDOFF_ID

    package_path = runtime / handoff.package_path
    assert package_path.exists()
    package = json.loads(package_path.read_text(encoding="utf-8"))
    assert package["handoff_id"] == HANDOFF_ID
    assert package["input_hash"] == handoff.input_hash
    assert package["result_contract"] == contract.model_dump(mode="json")
    assert package["agentdock_return"]["relative_path"] == (
        f"external-results/{HANDOFF_ID}.json"
    )


def test_materialize_package_recovers_after_disk_file_is_deleted(tmp_path: Path) -> None:
    runtime, _database, _jobs, _job, _bound, service, _contract, handoff = _setup(tmp_path)
    package_path = runtime / handoff.package_path
    package_path.unlink()

    regenerated = service.materialize_package(handoff.handoff_id)

    assert regenerated == package_path
    assert json.loads(regenerated.read_text(encoding="utf-8"))["input_hash"] == handoff.input_hash


def test_valid_agentdock_return_is_persisted_before_job_continuation(tmp_path: Path) -> None:
    runtime, database, jobs, job, bound, service, _contract, handoff = _setup(tmp_path)
    _write_return(runtime, handoff)

    accepted = service.accept_return_file(handoff.handoff_id)

    assert accepted.handoff_id == HANDOFF_ID
    assert accepted.job_id == job.id
    assert accepted.source_run_id == bound.run_id
    assert accepted.evidence_ref == f"external:chatgpt:{HANDOFF_ID}"
    assert accepted.result["summary"] == "grounded pattern"

    # Slice 1 is intentionally fail-closed: accepted external evidence is
    # durable before a later continuation slice may re-claim this Job.
    waiting_job = jobs.get(job.id)
    assert waiting_job.state is JobState.needs_human
    assert waiting_job.lease_expires_at is None
    assert waiting_job.current_stage == MANUAL_CHATGPT_RESULT_READY
    assert waiting_job.error_category == MANUAL_CHATGPT_RESULT_READY

    run = AgentRunStore(database).get_run(bound.run_id)
    assert run.state == AgentRunState.needs_human.value
    assert run.error_category == MANUAL_CHATGPT_RESULT_READY
    steps = AgentRunStore(database).list_steps(bound.run_id)
    wait_step = next(step for step in steps if step.kind == "external_handoff")
    result_step = next(step for step in steps if step.kind == "external_result")
    assert wait_step.status == "resolved"
    assert result_step.status == "succeeded"
    assert result_step.output_json == accepted.result
    assert result_step.evidence_refs_json == [accepted.evidence_ref]

    with database.sessions() as session:
        record = session.get(ManualChatGPTHandoffRecord, HANDOFF_ID)
        human = session.get(HumanActionRecord, HUMAN_ID)
        assert record is not None and record.status == "accepted"
        assert record.result_json == accepted.result
        assert record.result_path == f"external-results/{HANDOFF_ID}.json"
        assert human is not None and human.status == "completed"
        assert human.resolution_json["evidence_ref"] == accepted.evidence_ref


def test_duplicate_agentdock_return_replay_is_rejected_without_second_result_step(
    tmp_path: Path,
) -> None:
    runtime, database, _jobs, _job, bound, service, _contract, handoff = _setup(tmp_path)
    _write_return(runtime, handoff)
    service.accept_return_file(HANDOFF_ID)

    with pytest.raises(ManualChatGPTHandoffError, match="already accepted|replay"):
        service.accept_return_file(HANDOFF_ID)

    result_steps = [
        step
        for step in AgentRunStore(database).list_steps(bound.run_id)
        if step.kind == "external_result"
    ]
    assert len(result_steps) == 1


@pytest.mark.parametrize(
    ("override", "error"),
    [
        ({"input_hash": "sha256:" + "0" * 64}, "input_hash"),
        ({"schema_version": "old-schema"}, "schema_version"),
        ({"handoff_id": "33333333-3333-4333-8333-333333333333"}, "handoff_id"),
    ],
)
def test_stale_or_wrong_identity_envelope_is_rejected_and_handoff_stays_pending(
    tmp_path: Path, override: dict, error: str
) -> None:
    runtime, database, jobs, job, bound, service, _contract, handoff = _setup(tmp_path)
    _write_return(runtime, handoff, **override)

    with pytest.raises(ManualChatGPTHandoffError, match=error):
        service.accept_return_file(HANDOFF_ID)

    assert jobs.get(job.id).state is JobState.needs_human
    assert AgentRunStore(database).get_run(bound.run_id).error_category == MANUAL_CHATGPT_REQUIRED
    with database.sessions() as session:
        record = session.get(ManualChatGPTHandoffRecord, HANDOFF_ID)
        human = session.get(HumanActionRecord, HUMAN_ID)
        assert record is not None and record.status == "pending"
        assert human is not None and human.status == "pending"


def test_result_contract_missing_or_extra_fields_is_rejected(tmp_path: Path) -> None:
    runtime, database, _jobs, _job, bound, service, _contract, handoff = _setup(tmp_path)
    _write_return(runtime, handoff, result={"summary": "only one field"})

    with pytest.raises(ManualChatGPTHandoffError, match="missing required fields"):
        service.accept_return_file(HANDOFF_ID)

    _write_return(
        runtime,
        handoff,
        result={
            "summary": "ok",
            "recommendations": [],
            "unexpected": "must fail when extras are forbidden",
        },
    )
    with pytest.raises(ManualChatGPTHandoffError, match="unexpected fields"):
        service.accept_return_file(HANDOFF_ID)

    assert not any(
        step.kind == "external_result"
        for step in AgentRunStore(database).list_steps(bound.run_id)
    )


def test_unexpected_envelope_field_is_rejected(tmp_path: Path) -> None:
    runtime, _database, _jobs, _job, _bound, service, _contract, handoff = _setup(tmp_path)
    _write_return(runtime, handoff, extra_envelope={"job_id": "attacker-selected-job"})

    with pytest.raises(ManualChatGPTHandoffError, match="unexpected or missing"):
        service.accept_return_file(HANDOFF_ID)


def test_cancelled_job_wins_over_late_agentdock_return(tmp_path: Path) -> None:
    runtime, database, jobs, job, bound, service, _contract, handoff = _setup(tmp_path)
    jobs.transition(job.id, JobState.cancelled)
    _write_return(runtime, handoff)

    with pytest.raises(ManualChatGPTHandoffError, match="authoritative lease-free manual wait"):
        service.accept_return_file(HANDOFF_ID)

    assert jobs.get(job.id).state is JobState.cancelled
    assert not any(
        step.kind == "external_result"
        for step in AgentRunStore(database).list_steps(bound.run_id)
    )


def test_newer_binding_wins_over_late_agentdock_return(tmp_path: Path) -> None:
    runtime, database, jobs, job, bound, service, _contract, handoff = _setup(tmp_path)
    newer_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=1)
    with database.sessions.begin() as session:
        session.add(
            AgentRunRecord(
                id="run-new",
                goal="newer authority",
                state=AgentRunState.running.value,
                budget_json=RunBudget(max_wall_time_seconds=30).model_dump(mode="json"),
                step_count=0,
                model_calls=0,
                input_tokens=0,
                output_tokens=0,
                created_at=newer_at,
                updated_at=newer_at,
            )
        )
        session.flush()
        session.add(
            AgentJobBindingRecord(
                run_id="run-new",
                job_id=job.id,
                created_at=newer_at,
            )
        )
    _write_return(runtime, handoff)

    with pytest.raises(ManualChatGPTHandoffError, match="authority changed"):
        service.accept_return_file(HANDOFF_ID)

    assert jobs.get(job.id).state is JobState.needs_human
    assert not any(
        step.kind == "external_result"
        for step in AgentRunStore(database).list_steps(bound.run_id)
    )


def test_oversized_return_is_rejected_before_json_parse(tmp_path: Path) -> None:
    runtime, database, _jobs, _job, bound, _service, _contract, handoff = _setup(tmp_path)
    strict_service = ManualChatGPTHandoffService(
        database,
        runtime_dir=runtime,
        max_result_bytes=32,
    )
    path = runtime / "external-results" / f"{handoff.handoff_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{" + "x" * 100 + "}", encoding="utf-8")

    with pytest.raises(ManualChatGPTHandoffError, match="size limit"):
        strict_service.accept_return_file(HANDOFF_ID)

    assert not any(
        step.kind == "external_result"
        for step in AgentRunStore(database).list_steps(bound.run_id)
    )
