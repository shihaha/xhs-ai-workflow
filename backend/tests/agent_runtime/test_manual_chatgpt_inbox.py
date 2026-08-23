from __future__ import annotations

import json
from pathlib import Path
from threading import Event

from backend.app.agent_runtime.job_binding import AgentJobCoordinator
from backend.app.agent_runtime.manual_chatgpt_handoff import (
    HandoffResultContract,
    ManualChatGPTHandoffRecord,
    ManualChatGPTHandoffService,
)
from backend.app.agent_runtime.manual_chatgpt_inbox import ManualChatGPTInboxWatcher
from backend.app.agent_runtime.types import RunBudget
from backend.app.db import Database
from backend.app.models.jobs import JobState
from backend.app.services.jobs import JobService


HANDOFF_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
HUMAN_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _waiting_handoff(tmp_path: Path):
    runtime = tmp_path / "runtime"
    database = Database(runtime / "workbench.sqlite3")
    jobs = JobService(database, runtime_dir=runtime)
    job = jobs.create(job_type="agent_orchestration", input_data={"case": "inbox"})
    bound = AgentJobCoordinator(database, run_id_factory=lambda: "run-inbox").claim_and_create_run(
        job.id,
        goal="wait for ChatGPT result",
        budget=RunBudget(max_wall_time_seconds=30),
    )
    service = ManualChatGPTHandoffService(
        database,
        runtime_dir=runtime,
        handoff_id_factory=lambda: HANDOFF_ID,
        human_action_id_factory=lambda: HUMAN_ID,
    )
    handoff = service.request_handoff(
        bound.run_id,
        task={"instruction": "high-quality analysis"},
        context_refs=["evidence:xhs:1"],
        stage_revision="analysis-r1",
        result_contract=HandoffResultContract(
            schema_version="analysis-v1",
            required_fields=("summary",),
            allow_extra_fields=False,
        ),
    )
    return runtime, database, jobs, job, service, handoff


def _write_valid_return(runtime: Path, handoff, *, result=None) -> Path:
    path = runtime / "external-results" / f"{handoff.handoff_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "handoff_id": handoff.handoff_id,
                "schema_version": handoff.schema_version,
                "input_hash": handoff.input_hash,
                "result": result or {"summary": "ChatGPT result"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_poll_waits_only_for_durable_pending_handoffs(tmp_path: Path) -> None:
    runtime, _database, _jobs, _job, service, handoff = _waiting_handoff(tmp_path)
    # An arbitrary inbox file is not a candidate because no durable handoff owns it.
    arbitrary = runtime / "external-results" / "cccccccc-cccc-4ccc-8ccc-cccccccccccc.json"
    arbitrary.parent.mkdir(parents=True, exist_ok=True)
    arbitrary.write_text("{}", encoding="utf-8")

    result = ManualChatGPTInboxWatcher(service).poll_once()

    assert result.checked == 1
    assert result.waiting == 1
    assert result.accepted == ()
    assert result.rejected == ()
    assert arbitrary.exists()
    assert handoff.handoff_id == HANDOFF_ID


def test_poll_automatically_accepts_valid_agentdock_return(tmp_path: Path) -> None:
    runtime, database, jobs, job, service, handoff = _waiting_handoff(tmp_path)
    _write_valid_return(runtime, handoff)

    result = ManualChatGPTInboxWatcher(service).poll_once()

    assert result.checked == 1
    assert result.waiting == 0
    assert len(result.accepted) == 1
    assert result.accepted[0].handoff_id == HANDOFF_ID
    assert result.rejected == ()
    assert jobs.get(job.id).state is JobState.needs_human
    with database.sessions() as session:
        record = session.get(ManualChatGPTHandoffRecord, HANDOFF_ID)
        assert record is not None and record.status == "accepted"


def test_malformed_partial_write_is_retried_after_agentdock_replaces_file(tmp_path: Path) -> None:
    runtime, database, _jobs, _job, service, handoff = _waiting_handoff(tmp_path)
    path = runtime / "external-results" / f"{HANDOFF_ID}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"handoff_id":', encoding="utf-8")
    watcher = ManualChatGPTInboxWatcher(service)

    first = watcher.poll_once()

    assert len(first.rejected) == 1
    assert "valid UTF-8 JSON" in first.rejected[0].detail
    with database.sessions() as session:
        record = session.get(ManualChatGPTHandoffRecord, HANDOFF_ID)
        assert record is not None and record.status == "pending"

    _write_valid_return(runtime, handoff)
    second = watcher.poll_once()

    assert len(second.accepted) == 1
    assert second.rejected == ()


def test_post_accept_callback_failure_does_not_rollback_durable_acceptance(tmp_path: Path) -> None:
    runtime, database, _jobs, _job, service, handoff = _waiting_handoff(tmp_path)
    _write_valid_return(runtime, handoff)

    def explode(_result) -> None:
        raise RuntimeError("observer failed")

    result = ManualChatGPTInboxWatcher(service, on_result=explode).poll_once()

    assert len(result.accepted) == 1
    assert len(result.rejected) == 1
    assert "post_accept_callback_error" in result.rejected[0].detail
    with database.sessions() as session:
        record = session.get(ManualChatGPTHandoffRecord, HANDOFF_ID)
        assert record is not None and record.status == "accepted"


def test_background_watcher_wake_accepts_return_without_manual_poll_call(tmp_path: Path) -> None:
    runtime, database, _jobs, _job, service, handoff = _waiting_handoff(tmp_path)
    observed = Event()
    watcher = ManualChatGPTInboxWatcher(
        service,
        poll_seconds=60,
        on_result=lambda _result: observed.set(),
    )
    watcher.start()
    try:
        _write_valid_return(runtime, handoff)
        watcher.wake()
        assert observed.wait(2), "background inbox watcher did not accept AgentDock return"
        assert watcher.last_loop_error is None
        assert watcher.last_poll is not None
        assert len(watcher.last_poll.accepted) == 1
        with database.sessions() as session:
            record = session.get(ManualChatGPTHandoffRecord, HANDOFF_ID)
            assert record is not None and record.status == "accepted"
    finally:
        assert watcher.close(timeout_seconds=2)
    assert watcher.is_running is False


def test_close_before_start_is_idempotently_safe(tmp_path: Path) -> None:
    _runtime, _database, _jobs, _job, service, _handoff = _waiting_handoff(tmp_path)
    watcher = ManualChatGPTInboxWatcher(service)

    assert watcher.close() is True
    assert watcher.close() is True
