from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from backend.app.agent_runtime import RunBudget
from backend.app.agent_runtime.job_binding import AgentJobCoordinator
from backend.app.agent_runtime.manual_chatgpt_handoff import (
    HandoffResultContract,
    ManualChatGPTHandoffService,
)
from backend.app.main import create_app
from backend.app.settings import Settings


HANDOFF_ID = "33333333-3333-4333-8333-333333333333"
HUMAN_ID = "44444444-4444-4444-8444-444444444444"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _settings(tmp_path: Path) -> Settings:
    runtime = tmp_path / "runtime"
    return Settings(runtime_dir=runtime, database_path=runtime / "workbench.sqlite3")


def _seed_handoff(app, tmp_path: Path):
    database = app.state.database
    jobs = app.state.job_service
    assert database is not None
    assert jobs is not None

    job = jobs.create(
        job_type="agent_orchestration",
        input_data={"private_job_input": "must-not-leak"},
        current_stage="agent_start",
    )
    bound = AgentJobCoordinator(
        database, run_id_factory=lambda: "run-chatgpt-workbench"
    ).claim_and_create_run(
        job.id,
        goal="quality-sensitive analysis",
        budget=RunBudget(max_wall_time_seconds=30),
    )
    service = ManualChatGPTHandoffService(
        database,
        runtime_dir=tmp_path / "runtime",
        handoff_id_factory=lambda: HANDOFF_ID,
        human_action_id_factory=lambda: HUMAN_ID,
    )
    contract = HandoffResultContract(
        schema_version="chatgpt-workbench-v1",
        required_fields=("summary", "recommendations"),
        allow_extra_fields=False,
    )
    handoff = service.request_handoff(
        bound.run_id,
        task={
            "instruction": "PRIVATE-TASK-CONTENT-MUST-NOT-LEAK",
            "account": "PRIVATE-ACCOUNT-MUST-NOT-LEAK",
        },
        context_refs=[
            "PRIVATE-CONTEXT-REF-ONE",
            "PRIVATE-CONTEXT-REF-TWO",
        ],
        stage_revision="quality-r7",
        result_contract=contract,
    )
    return job, bound, service, handoff


def _write_return(tmp_path: Path, handoff) -> None:
    path = tmp_path / "runtime" / "external-results" / f"{handoff.handoff_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "handoff_id": handoff.handoff_id,
                "schema_version": handoff.schema_version,
                "input_hash": handoff.input_hash,
                "result": {
                    "summary": "PRIVATE-CHATGPT-RESULT-MUST-NOT-LEAK",
                    "recommendations": ["PRIVATE-RECOMMENDATION-MUST-NOT-LEAK"],
                },
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.anyio
async def test_pending_chatgpt_handoff_projection_is_durable_authoritative_and_redacted(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    job, bound, _service, handoff = _seed_handoff(app, tmp_path)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        listed = await client.get("/api/v1/agent-runtime/chatgpt-handoffs")
        pending = await client.get(
            "/api/v1/agent-runtime/chatgpt-handoffs", params={"status": "pending"}
        )
        detail = await client.get(
            f"/api/v1/agent-runtime/chatgpt-handoffs/{handoff.handoff_id}"
        )

    assert listed.status_code == 200
    assert pending.status_code == 200
    assert detail.status_code == 200
    assert len(listed.json()) == 1
    assert pending.json() == listed.json()

    row = detail.json()
    assert row["handoff_id"] == HANDOFF_ID
    assert row["job_id"] == job.id
    assert row["source_run_id"] == bound.run_id
    assert row["human_action_id"] == HUMAN_ID
    assert row["status"] == "pending"
    assert row["human_action_status"] == "pending"
    assert row["job_state"] == "needs_human"
    assert row["current_stage"] == "manual_chatgpt_required"
    assert row["stage_revision"] == "quality-r7"
    assert row["schema_version"] == "chatgpt-workbench-v1"
    assert row["input_hash"].startswith("sha256:")
    assert row["context_ref_count"] == 2
    assert row["has_result"] is False
    assert row["is_current_binding"] is True
    assert row["authority_ambiguous"] is False
    assert row["needs_chatgpt"] is True
    assert row["result_ready"] is False

    combined = listed.text + detail.text
    for secret in (
        "must-not-leak",
        "PRIVATE-TASK-CONTENT-MUST-NOT-LEAK",
        "PRIVATE-ACCOUNT-MUST-NOT-LEAK",
        "PRIVATE-CONTEXT-REF-ONE",
        "PRIVATE-CONTEXT-REF-TWO",
        "task_json",
        "context_refs",
        "package_path",
        "result_path",
        "result_json",
        "chatgpt-handoffs/outbox",
        "external-results",
    ):
        assert secret not in combined

    restarted = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=restarted), base_url="http://testserver"
    ) as client:
        after_restart = await client.get(
            f"/api/v1/agent-runtime/chatgpt-handoffs/{HANDOFF_ID}"
        )

    assert after_restart.status_code == 200
    assert after_restart.json()["needs_chatgpt"] is True
    assert after_restart.json()["input_hash"] == row["input_hash"]


@pytest.mark.anyio
async def test_accepted_chatgpt_result_projects_ready_without_exposing_result(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path))
    job, bound, service, handoff = _seed_handoff(app, tmp_path)
    _write_return(tmp_path, handoff)
    accepted = service.accept_return_file(handoff.handoff_id)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        accepted_rows = await client.get(
            "/api/v1/agent-runtime/chatgpt-handoffs", params={"status": "accepted"}
        )
        pending_rows = await client.get(
            "/api/v1/agent-runtime/chatgpt-handoffs", params={"status": "pending"}
        )
        completed_actions = await client.get(
            "/api/v1/agent-runtime/human-actions", params={"status": "completed"}
        )
        invalid_handoff_status = await client.get(
            "/api/v1/agent-runtime/chatgpt-handoffs", params={"status": "bogus"}
        )
        missing = await client.get(
            "/api/v1/agent-runtime/chatgpt-handoffs/55555555-5555-4555-8555-555555555555"
        )

    assert accepted_rows.status_code == 200
    assert pending_rows.status_code == 200
    assert completed_actions.status_code == 200
    assert invalid_handoff_status.status_code == 422
    assert missing.status_code == 404
    assert pending_rows.json() == []

    row = accepted_rows.json()[0]
    assert row["handoff_id"] == HANDOFF_ID
    assert row["job_id"] == job.id
    assert row["source_run_id"] == bound.run_id
    assert row["status"] == "accepted"
    assert row["human_action_status"] == "completed"
    assert row["current_stage"] == "manual_chatgpt_result_ready"
    assert row["has_result"] is True
    assert row["needs_chatgpt"] is False
    assert row["result_ready"] is True
    assert row["accepted_at"] is not None
    assert accepted.evidence_ref == f"external:chatgpt:{HANDOFF_ID}"

    assert len(completed_actions.json()) == 1
    assert completed_actions.json()[0]["id"] == HUMAN_ID
    assert completed_actions.json()[0]["job_id"] == job.id

    combined = accepted_rows.text + completed_actions.text
    assert "PRIVATE-CHATGPT-RESULT-MUST-NOT-LEAK" not in combined
    assert "PRIVATE-RECOMMENDATION-MUST-NOT-LEAK" not in combined
    assert "result_json" not in combined
    assert "result_path" not in combined
