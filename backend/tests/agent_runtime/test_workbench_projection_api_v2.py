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


def _seed_projection_data(app, tmp_path: Path):
    jobs = app.state.job_service
    assert jobs is not None
    database = app.state.database
    assert database is not None

    waiting_job = jobs.create(
        job_type="agent_orchestration",
        input_data={"private_job_input": "must-not-leak"},
        current_stage="agent_start",
    )
    waiting = AgentJobCoordinator(
        database, run_id_factory=lambda: "run-projection-waiting"
    ).claim_and_create_run(
        waiting_job.id,
        goal="build a safe workbench projection",
        budget=RunBudget(max_wall_time_seconds=30),
        model_name="test-model",
        prompt_version="projection-v2",
    )
    store = AgentRunStore(database)
    store.append_step(
        waiting.run_id,
        kind="tool",
        status="succeeded",
        tool_name="evidence.read",
        tool_call_id="evidence-call",
        input_json={"secret_argument": "must-not-leak"},
        output_json={"raw_tool_output": "must-not-leak"},
        evidence_refs=["evidence:one", "evidence:two", "evidence:one"],
    )
    action = store.create_human_action(
        run_id=waiting.run_id,
        tool_call_id="human-call",
        tool_name="quality.review",
        request_json={"private_request": "must-not-leak"},
    )
    store.append_step(
        waiting.run_id,
        kind="tool",
        status="needs_human",
        tool_name="quality.review",
        tool_call_id="human-call",
        input_json={"private_request": "must-not-leak"},
    )
    store.set_state(
        waiting.run_id,
        AgentRunState.needs_human,
        error_category="approval_required",
        error_detail="operator review required",
    )
    jobs.transition(
        waiting_job.id,
        JobState.needs_human,
        current_stage="approval_required",
        error_category="approval_required",
    )

    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    artifact_path = runtime / "agent-report.json"
    artifact_path.write_text('{"private_artifact":"must-not-leak"}', encoding="utf-8")
    jobs.attach_artifact(
        waiting_job.id,
        kind="agent_report",
        path="agent-report.json",
        metadata={"private_metadata": "must-not-leak"},
    )

    running_job = jobs.create(
        job_type="agent_orchestration",
        input_data={"another_private_input": "must-not-leak"},
        current_stage="agent_start",
    )
    running = AgentJobCoordinator(
        database, run_id_factory=lambda: "run-projection-running"
    ).claim_and_create_run(
        running_job.id,
        goal="keep a second run visible",
        budget=RunBudget(max_wall_time_seconds=30),
    )

    ordinary = jobs.create(
        job_type="collection",
        input_data={"ordinary_private_input": "must-not-leak"},
    )
    return waiting_job, waiting, action, running_job, running, ordinary


@pytest.mark.anyio
async def test_projection_collections_are_durable_scoped_and_redacted(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    waiting_job, waiting, action, running_job, running, ordinary = _seed_projection_data(
        app, tmp_path
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        jobs_response = await client.get("/api/v1/agent-runtime/jobs")
        runs_response = await client.get("/api/v1/agent-runtime/runs")
        actions_response = await client.get(
            "/api/v1/agent-runtime/human-actions", params={"status": "pending"}
        )
        artifacts_response = await client.get(
            f"/api/v1/agent-runtime/jobs/{waiting_job.id}/artifacts"
        )
        evidence_response = await client.get(
            f"/api/v1/agent-runtime/jobs/{waiting_job.id}/evidence"
        )

    assert jobs_response.status_code == 200
    job_rows = {row["job_id"]: row for row in jobs_response.json()}
    assert set(job_rows) == {waiting_job.id, running_job.id}
    assert ordinary.id not in job_rows
    waiting_row = job_rows[waiting_job.id]
    assert waiting_row["job_state"] == "needs_human"
    assert waiting_row["current_run_id"] == waiting.run_id
    assert waiting_row["run_count"] == 1
    assert waiting_row["pending_human_action_count"] == 1
    assert waiting_row["evidence_count"] == 2
    assert waiting_row["artifact_count"] == 1

    assert runs_response.status_code == 200
    run_rows = {row["run_id"]: row for row in runs_response.json()}
    assert run_rows[waiting.run_id]["job_id"] == waiting_job.id
    assert run_rows[running.run_id]["job_id"] == running_job.id

    assert actions_response.status_code == 200
    assert len(actions_response.json()) == 1
    assert actions_response.json()[0]["id"] == action.id
    assert actions_response.json()[0]["job_id"] == waiting_job.id
    assert actions_response.json()[0]["status"] == "pending"

    assert artifacts_response.status_code == 200
    assert len(artifacts_response.json()) == 1
    artifact = artifacts_response.json()[0]
    assert artifact["job_id"] == waiting_job.id
    assert artifact["kind"] == "agent_report"
    assert artifact["producer"] == "external"
    assert "path" not in artifact
    assert "metadata" not in artifact

    assert evidence_response.status_code == 200
    assert evidence_response.json() == {
        "job_id": waiting_job.id,
        "evidence_refs": ["evidence:one", "evidence:two"],
    }

    combined = "\n".join(
        response.text
        for response in (
            jobs_response,
            runs_response,
            actions_response,
            artifacts_response,
            evidence_response,
        )
    )
    for secret in (
        "private_job_input",
        "another_private_input",
        "ordinary_private_input",
        "secret_argument",
        "raw_tool_output",
        "private_request",
        "private_metadata",
        "private_artifact",
        "agent-report.json",
        "must-not-leak",
    ):
        assert secret not in combined

    restarted = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=restarted),
        base_url="http://testserver",
    ) as client:
        restarted_jobs = await client.get("/api/v1/agent-runtime/jobs")
        restarted_actions = await client.get(
            "/api/v1/agent-runtime/human-actions", params={"status": "pending"}
        )

    assert restarted_jobs.status_code == 200
    restarted_rows = {row["job_id"]: row for row in restarted_jobs.json()}
    assert restarted_rows[waiting_job.id]["artifact_count"] == 1
    assert restarted_rows[waiting_job.id]["evidence_count"] == 2
    assert restarted_actions.status_code == 200
    assert restarted_actions.json()[0]["id"] == action.id


@pytest.mark.anyio
async def test_human_action_filter_and_non_agent_projection_boundaries(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path))
    waiting_job, waiting, action, _running_job, _running, ordinary = _seed_projection_data(
        app, tmp_path
    )
    store = AgentRunStore(app.state.database)
    store.resolve_human_action(action.id, approved=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        pending = await client.get(
            "/api/v1/agent-runtime/human-actions", params={"status": "pending"}
        )
        approved = await client.get(
            "/api/v1/agent-runtime/human-actions", params={"status": "approved"}
        )
        invalid = await client.get(
            "/api/v1/agent-runtime/human-actions", params={"status": "bogus"}
        )
        wrong_artifacts = await client.get(
            f"/api/v1/agent-runtime/jobs/{ordinary.id}/artifacts"
        )
        wrong_evidence = await client.get(
            f"/api/v1/agent-runtime/jobs/{ordinary.id}/evidence"
        )
        missing_artifacts = await client.get(
            "/api/v1/agent-runtime/jobs/missing-job/artifacts"
        )

    assert pending.status_code == 200
    assert all(row["id"] != action.id for row in pending.json())
    assert approved.status_code == 200
    assert approved.json()[0]["id"] == action.id
    assert approved.json()[0]["job_id"] == waiting_job.id
    assert approved.json()[0]["run_id"] == waiting.run_id
    assert invalid.status_code == 422
    assert wrong_artifacts.status_code == 422
    assert wrong_evidence.status_code == 422
    assert missing_artifacts.status_code == 404
