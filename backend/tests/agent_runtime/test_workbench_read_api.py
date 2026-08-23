from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from backend.app.agent_runtime import AgentRunState, AgentRunStore, RunBudget
from backend.app.agent_runtime.job_binding import AgentJobCoordinator
from backend.app.main import create_app
from backend.app.models.jobs import JobState
from backend.app.settings import Settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _settings(tmp_path: Path) -> Settings:
    runtime = tmp_path / "runtime"
    return Settings(runtime_dir=runtime, database_path=runtime / "workbench.sqlite3")


def _seed_waiting_agent_job(app):
    jobs = app.state.job_service
    assert jobs is not None
    database = app.state.database
    assert database is not None

    job = jobs.create(
        job_type="agent_orchestration",
        input_data={"private_job_input": "must-not-leak"},
        current_stage="agent_start",
    )
    coordinator = AgentJobCoordinator(database, run_id_factory=lambda: "run-read-api")
    bound = coordinator.claim_and_create_run(
        job.id,
        goal="inspect durable workbench state",
        budget=RunBudget(max_wall_time_seconds=30),
        model_name="test-model",
        prompt_version="prompt-v1",
    )
    store = AgentRunStore(database)
    store.append_step(
        bound.run_id,
        kind="tool",
        status="succeeded",
        tool_name="evidence.read",
        tool_call_id="tool-call-1",
        input_json={"secret_argument": "must-not-leak"},
        output_json={"raw_tool_output": "must-not-leak"},
        evidence_refs=["evidence:note:1", "evidence:note:1", "evidence:note:2"],
    )
    action = store.create_human_action(
        run_id=bound.run_id,
        tool_call_id="approval-call-1",
        tool_name="sensitive.write",
        request_json={"arguments": {"secret": "must-not-leak"}},
    )
    store.append_step(
        bound.run_id,
        kind="tool",
        status="needs_human",
        tool_name="sensitive.write",
        tool_call_id="approval-call-1",
        input_json={"arguments": {"secret": "must-not-leak"}},
    )
    store.set_state(
        bound.run_id,
        AgentRunState.needs_human,
        error_category="approval_required",
        error_detail="operator approval required",
    )
    jobs.transition(
        job.id,
        JobState.needs_human,
        current_stage="approval_required",
        error_category="approval_required",
    )
    return job, bound, action


@pytest.mark.anyio
async def test_job_runtime_read_surface_is_durable_and_redacts_raw_inputs(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    first_app = create_app(settings)
    job, bound, action = _seed_waiting_agent_job(first_app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=first_app),
        base_url="http://testserver",
    ) as client:
        response = await client.get(f"/api/v1/agent-runtime/jobs/{job.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == job.id
    assert body["job_state"] == "needs_human"
    assert body["current_stage"] == "approval_required"
    assert body["current_run_id"] == bound.run_id
    assert body["authority_ambiguous"] is False
    assert body["evidence_refs"] == ["evidence:note:1", "evidence:note:2"]
    assert body["runs"][0]["run_id"] == bound.run_id
    assert body["runs"][0]["state"] == "needs_human"
    assert body["runs"][0]["model_name"] == "test-model"
    assert body["pending_human_actions"] == [
        {
            "id": action.id,
            "run_id": bound.run_id,
            "tool_call_id": "approval-call-1",
            "tool_name": "sensitive.write",
            "status": "pending",
            "can_deny": True,
            "created_at": body["pending_human_actions"][0]["created_at"],
            "resolved_at": None,
        }
    ]

    encoded = response.text
    assert "private_job_input" not in encoded
    assert "secret_argument" not in encoded
    assert "raw_tool_output" not in encoded
    assert "must-not-leak" not in encoded
    assert "request_json" not in encoded
    assert "resolution_json" not in encoded

    restarted_app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=restarted_app),
        base_url="http://testserver",
    ) as client:
        restarted = await client.get(f"/api/v1/agent-runtime/jobs/{job.id}")

    assert restarted.status_code == 200
    assert restarted.json()["current_run_id"] == bound.run_id
    assert restarted.json()["evidence_refs"] == ["evidence:note:1", "evidence:note:2"]
    assert restarted.json()["pending_human_actions"][0]["id"] == action.id


@pytest.mark.anyio
async def test_run_detail_exposes_timeline_without_raw_tool_payloads(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    _job, bound, action = _seed_waiting_agent_job(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get(f"/api/v1/agent-runtime/runs/{bound.run_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == bound.run_id
    assert body["state"] == "needs_human"
    assert body["evidence_refs"] == ["evidence:note:1", "evidence:note:2"]
    assert [step["status"] for step in body["steps"]] == ["succeeded", "needs_human"]
    assert body["human_actions"][0]["id"] == action.id
    assert body["human_actions"][0]["status"] == "pending"

    for step in body["steps"]:
        assert "input_json" not in step
        assert "output_json" not in step
    assert "request_json" not in response.text
    assert "resolution_json" not in response.text
    assert "must-not-leak" not in response.text


@pytest.mark.anyio
async def test_agent_runtime_read_api_rejects_non_agent_jobs_and_unknown_runs(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path))
    jobs = app.state.job_service
    assert jobs is not None
    ordinary = jobs.create(job_type="collection", input_data={})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        wrong_type = await client.get(f"/api/v1/agent-runtime/jobs/{ordinary.id}")
        missing_job = await client.get("/api/v1/agent-runtime/jobs/missing-job")
        missing_run = await client.get("/api/v1/agent-runtime/runs/missing-run")

    assert wrong_type.status_code == 422
    assert missing_job.status_code == 404
    assert missing_run.status_code == 404
