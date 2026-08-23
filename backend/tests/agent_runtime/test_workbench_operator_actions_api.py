from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from backend.app.agent_runtime import AgentRunState, AgentRunStore, NextAction, RunBudget
from backend.app.agent_runtime.job_binding import AgentJobCoordinator
from backend.app.agent_runtime.job_continuation import JobContinuationCoordinator
from backend.app.main import create_app
from backend.app.models.jobs import JobState
from backend.app.settings import Settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _settings(tmp_path: Path) -> Settings:
    runtime = tmp_path / "runtime"
    return Settings(runtime_dir=runtime, database_path=runtime / "workbench.sqlite3")


def _seed_permission_wait(app, *, error_category: str = "approval_required", tool_name: str = "analysis.run_grounded"):
    database = app.state.database
    jobs = app.state.job_service
    assert database is not None
    assert jobs is not None

    job = jobs.create(
        job_type="agent_orchestration",
        input_data={"private": "never-expose"},
        current_stage="agent_running",
    )
    bound = AgentJobCoordinator(
        database,
        run_id_factory=lambda: f"run-{error_category}",
    ).claim_and_create_run(
        job.id,
        goal="test safe operator mutation",
        budget=RunBudget(max_wall_time_seconds=30),
    )
    store = AgentRunStore(database)
    action = NextAction(
        action="tool",
        tool_name=tool_name,
        arguments={"scope": "exact"},
        tool_call_id=f"call-{error_category}",
    )
    human = store.create_human_action(
        run_id=bound.run_id,
        tool_call_id=action.tool_call_id,
        tool_name=tool_name,
        request_json={"action": action.model_dump(mode="json")},
    )
    store.set_state(
        bound.run_id,
        AgentRunState.needs_human,
        error_category=error_category,
        error_detail="operator decision required",
    )
    jobs.transition(
        job.id,
        JobState.needs_human,
        current_stage=error_category,
        error_category=error_category,
    )
    return job, bound, human


@pytest.mark.anyio
async def test_operator_capabilities_fail_closed_on_continuation(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/api/v1/agent-runtime/operator-capabilities")

    assert response.status_code == 200
    assert response.json()["cancel_job"] is True
    assert response.json()["deny_permission_action"] is True
    assert response.json()["approve_continuation"] is False
    assert "executor" in response.json()["continuation_reason"].lower()


@pytest.mark.anyio
async def test_cancel_agent_job_reconciles_run_and_pending_human_action_atomically(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    job, bound, human = _seed_permission_wait(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(f"/api/v1/agent-runtime/jobs/{job.id}/cancel")
        second = await client.post(f"/api/v1/agent-runtime/jobs/{job.id}/cancel")
        job_view = await client.get(f"/api/v1/agent-runtime/jobs/{job.id}")
        run_view = await client.get(f"/api/v1/agent-runtime/runs/{bound.run_id}")
        denied_actions = await client.get(
            "/api/v1/agent-runtime/human-actions", params={"status": "denied"}
        )

    assert response.status_code == 200
    assert response.json()["job_state"] == "cancelled"
    assert response.json()["cancelled_run_ids"] == [bound.run_id]
    assert response.json()["resolved_human_action_ids"] == [human.id]
    assert response.json()["already_cancelled"] is False
    assert second.status_code == 200
    assert second.json()["already_cancelled"] is True
    assert job_view.json()["job_state"] == "cancelled"
    assert job_view.json()["lease_expires_at"] is None
    assert run_view.json()["state"] == "cancelled"
    assert run_view.json()["error_category"] == "operator_cancelled"
    assert denied_actions.json()[0]["id"] == human.id


@pytest.mark.anyio
async def test_cancel_reconciles_legacy_already_cancelled_job_trace(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    job, bound, human = _seed_permission_wait(app)
    jobs = app.state.job_service
    assert jobs is not None
    jobs.transition(
        job.id,
        JobState.cancelled,
        current_stage="legacy_cancelled",
        error_category="legacy_cancelled",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(f"/api/v1/agent-runtime/jobs/{job.id}/cancel")
        run_view = await client.get(f"/api/v1/agent-runtime/runs/{bound.run_id}")

    assert response.status_code == 200
    assert response.json()["already_cancelled"] is True
    assert response.json()["cancelled_run_ids"] == [bound.run_id]
    assert response.json()["resolved_human_action_ids"] == [human.id]
    assert run_view.json()["state"] == "cancelled"
    assert run_view.json()["human_actions"][0]["status"] == "denied"


@pytest.mark.anyio
async def test_cancel_preserves_historical_source_run_after_continuation(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    job, source, human = _seed_permission_wait(app)
    database = app.state.database
    assert database is not None
    child = JobContinuationCoordinator(
        database, run_id_factory=lambda: "continuation-current"
    ).approve_and_create_continuation(source.run_id, human.id, note="approved before cancel")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(f"/api/v1/agent-runtime/jobs/{job.id}/cancel")
        source_view = await client.get(f"/api/v1/agent-runtime/runs/{source.run_id}")
        child_view = await client.get(f"/api/v1/agent-runtime/runs/{child.run_id}")

    assert response.status_code == 200
    assert response.json()["cancelled_run_ids"] == [child.run_id]
    assert source_view.json()["state"] == "needs_human"
    assert source_view.json()["human_actions"][0]["status"] == "approved"
    assert child_view.json()["state"] == "cancelled"


@pytest.mark.anyio
async def test_deny_permission_action_stops_exact_latest_wait(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    job, bound, human = _seed_permission_wait(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            f"/api/v1/agent-runtime/human-actions/{human.id}/deny",
            json={"note": "do not execute this action"},
        )
        job_view = await client.get(f"/api/v1/agent-runtime/jobs/{job.id}")
        run_view = await client.get(f"/api/v1/agent-runtime/runs/{bound.run_id}")

    assert response.status_code == 200
    assert response.json() == {
        "human_action_id": human.id,
        "job_id": job.id,
        "run_id": bound.run_id,
        "human_action_status": "denied",
        "job_state": "failed",
        "run_state": "failed",
    }
    assert job_view.json()["job_state"] == "failed"
    assert job_view.json()["current_stage"] == "human_denied"
    assert run_view.json()["state"] == "failed"
    assert run_view.json()["human_actions"][0]["status"] == "denied"
    assert run_view.json()["steps"][-1]["kind"] == "error"
    assert run_view.json()["steps"][-1]["error_category"] == "human_denied"


@pytest.mark.anyio
async def test_manual_chatgpt_wait_cannot_be_routed_through_permission_denial(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    job, bound, human = _seed_permission_wait(
        app,
        error_category="manual_chatgpt_required",
        tool_name="manual_chatgpt",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            f"/api/v1/agent-runtime/human-actions/{human.id}/deny",
            json={},
        )
        job_view = await client.get(f"/api/v1/agent-runtime/jobs/{job.id}")
        run_view = await client.get(f"/api/v1/agent-runtime/runs/{bound.run_id}")

    assert response.status_code == 409
    assert "approval_required" in response.json()["detail"]
    assert job_view.json()["job_state"] == "needs_human"
    assert run_view.json()["state"] == "needs_human"
    assert run_view.json()["human_actions"][0]["status"] == "pending"
    assert run_view.json()["human_actions"][0]["can_deny"] is False


@pytest.mark.anyio
async def test_generic_jobs_api_cannot_bypass_agent_lifecycle_authority(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    jobs = app.state.job_service
    assert jobs is not None
    job = jobs.create(job_type="agent_orchestration", input_data={"private": True})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        claim = await client.post(f"/api/v1/jobs/{job.id}/claim")
        transition = await client.post(
            f"/api/v1/jobs/{job.id}/transition",
            json={"state": "cancelled"},
        )

    assert claim.status_code == 422
    assert transition.status_code == 422
    assert jobs.get(job.id).state is JobState.queued
