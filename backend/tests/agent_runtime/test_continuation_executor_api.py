from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy import select, update

from backend.app.agent_runtime import (
    AgentRunState,
    AgentRunStore,
    ModelTurn,
    NextAction,
    RuleBasedPermissionPolicy,
    RunBudget,
    ToolExecutionResult,
    ToolRegistry,
    ToolSpec,
)
from backend.app.adapters.contracts import ModelResult
from backend.app.agent_runtime.context import RuntimeContext, StepView
from backend.app.agent_runtime.continuation_executor import AgentContinuationExecutor
from backend.app.agent_runtime.job_binding import AgentJobCoordinator
from backend.app.agent_runtime.job_bound_runtime import (
    ActiveRunJobAuthorityGuard,
    JobBoundAgentRuntime,
)
from backend.app.agent_runtime.job_continuation import (
    AgentContinuationDispatchRecord,
    JobContinuationCoordinator,
    JobHistoryContextBuilder,
)
from backend.app.agent_runtime.job_wait_projection import JobHumanWaitProjector
from backend.app.agent_runtime.production_runtime import BailianRuntimeDecisionModel
from backend.app.agent_runtime.workbench_actions import AgentWorkbenchActionService
from backend.app.main import create_app
from backend.app.models.jobs import JobState
from backend.app.settings import Settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class ValueInput(BaseModel):
    value: int


class FinishModel:
    def __init__(self) -> None:
        self.calls = 0

    def next_action(self, _context):
        self.calls += 1
        return ModelTurn(
            action=NextAction(action="finish", final_output={"continued": True}),
            input_tokens=1,
            output_tokens=1,
            model_name="executor-test",
        )


def _settings(tmp_path: Path) -> Settings:
    runtime = tmp_path / "runtime"
    return Settings(runtime_dir=runtime, database_path=runtime / "workbench.sqlite3")


def _seed_wait(app, *, tool_name: str = "approved.write", error_category: str = "approval_required"):
    database = app.state.database
    jobs = app.state.job_service
    assert database is not None
    assert jobs is not None
    job = jobs.create(
        job_type="agent_orchestration",
        input_data={"private": "not-for-workbench"},
        current_stage="approval_required",
    )
    bound = AgentJobCoordinator(
        database,
        run_id_factory=lambda: f"source-{tool_name.replace('.', '-')}",
    ).claim_and_create_run(
        job.id,
        goal="execute exactly the approved continuation",
        budget=RunBudget(
            max_steps=10,
            max_model_calls=4,
            max_input_tokens=1000,
            max_output_tokens=500,
            max_wall_time_seconds=30,
        ),
    )
    action = NextAction(
        action="tool",
        tool_name=tool_name,
        arguments={"value": 7},
        tool_call_id="approved-call",
    )
    store = AgentRunStore(database)
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
        error_detail="approval required",
    )
    jobs.transition(
        job.id,
        JobState.needs_human,
        current_stage=error_category,
        error_category=error_category,
    )
    return job, bound, human


def _executor(app, calls: dict[str, int], model: FinishModel) -> AgentContinuationExecutor:
    database = app.state.database
    assert database is not None

    def runtime_factory() -> JobBoundAgentRuntime:
        registry = ToolRegistry()

        def approved_write(value: ValueInput) -> ToolExecutionResult:
            calls["count"] = calls.get("count", 0) + 1
            calls["value"] = value.value
            return ToolExecutionResult(
                output={"written": value.value},
                evidence_refs=["evidence:approved:executor"],
            )

        registry.register(
            ToolSpec(
                name="approved.write",
                description="Test exact approved side effect.",
                input_model=ValueInput,
                handler=approved_write,
                read_only=False,
                external_side_effect=True,
                requires_approval=True,
                idempotent=False,
                timeout_seconds=5,
            )
        )
        return JobBoundAgentRuntime(
            store=AgentRunStore(database),
            model=model,
            tools=registry,
            permissions=RuleBasedPermissionPolicy(),
            authority_guard=ActiveRunJobAuthorityGuard(database),
            wait_projector=JobHumanWaitProjector(database),
            continuation_resolver=executor.coordinator,
            context_builder=JobHistoryContextBuilder(database),
        )

    executor = AgentContinuationExecutor(
        database,
        runtime_factory=runtime_factory,
        poll_seconds=0.01,
    )
    return executor


class FakeBailianRuntimeAdapter:
    configured = True

    def __init__(self) -> None:
        self.request = None

    def generate_structured(self, request, schema):
        self.request = request
        action = schema(action="finish", final_output={"continued": True})
        return ModelResult(
            model="existing-bailian-adapter",
            output=action.model_dump(mode="json"),
            raw_evidence={"provider": "test"},
            usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        )


def test_production_decision_driver_reuses_existing_adapter_and_real_evidence_scope() -> None:
    adapter = FakeBailianRuntimeAdapter()
    driver = BailianRuntimeDecisionModel(adapter)  # type: ignore[arg-type]
    context = RuntimeContext(
        goal="continue grounded work",
        run_id="continuation-run",
        usage={"model_calls": 0, "input_tokens": 0, "output_tokens": 0},
        remaining_budget={
            "steps": 5,
            "model_calls": 2,
            "input_tokens": 1000,
            "output_tokens": 500,
            "wall_time_seconds": 10.0,
        },
        tools=[],
        recent_steps=[
            StepView(
                step_index=1,
                kind="tool",
                tool_name="analysis.run_grounded",
                status="succeeded",
                evidence_refs=["artifact:1", "account-note:2"],
            )
        ],
    )

    turn = driver.next_action(context)

    assert turn.action.action == "finish"
    assert turn.action.final_output == {"continued": True}
    assert turn.input_tokens == 3
    assert turn.output_tokens == 2
    assert turn.model_name == "existing-bailian-adapter"
    assert adapter.request is not None
    assert adapter.request.evidence_ids == ["account-note:2", "artifact:1"]
    assert adapter.request.prompt_version == "job-bound-agent-v1"


def test_production_decision_driver_refuses_to_fabricate_evidence_scope() -> None:
    adapter = FakeBailianRuntimeAdapter()
    driver = BailianRuntimeDecisionModel(adapter)  # type: ignore[arg-type]
    context = RuntimeContext(
        goal="no evidence means no provider request",
        run_id="continuation-run",
        usage={"model_calls": 0, "input_tokens": 0, "output_tokens": 0},
        remaining_budget={
            "steps": 5,
            "model_calls": 2,
            "input_tokens": 1000,
            "output_tokens": 500,
            "wall_time_seconds": 10.0,
        },
        tools=[],
        recent_steps=[],
    )

    with pytest.raises(ValueError, match="durable evidence ref"):
        driver.next_action(context)
    assert adapter.request is None


def test_app_only_enables_auto_continuation_when_model_is_configured(tmp_path: Path) -> None:
    unconfigured = create_app(_settings(tmp_path / "off"))
    unconfigured_executor = unconfigured.state.agent_continuation_executor
    assert isinstance(unconfigured_executor, AgentContinuationExecutor)
    assert unconfigured_executor.accepting is False
    assert unconfigured.state.agent_workbench_actions.capabilities()["approve_continuation"] is False
    unconfigured_executor.start()
    assert unconfigured_executor.is_alive is False
    unconfigured_executor.close()

    configured_settings = _settings(tmp_path / "on").model_copy(
        update={"bailian_api_key": "test-only-key"}
    )
    configured = create_app(configured_settings)
    executor = configured.state.agent_continuation_executor
    assert isinstance(executor, AgentContinuationExecutor)
    assert configured.state.agent_workbench_actions.capabilities() == {
        "cancel_job": True,
        "deny_permission_action": True,
        "approve_continuation": True,
        "continuation_reason": None,
    }
    runtime = executor.runtime_factory()
    assert sorted(item["name"] for item in runtime.tools.public_definitions()) == [
        "analysis.run_grounded",
        "job.read",
    ]
    executor.close()


def test_unconfigured_restart_fail_closes_existing_queued_dispatch(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    job, source, human = _seed_wait(app)
    executor = app.state.agent_continuation_executor
    database = app.state.database
    assert isinstance(executor, AgentContinuationExecutor)
    assert database is not None
    assert executor.accepting is False

    approved = executor.coordinator.approve_and_create_continuation(
        source.run_id,
        human.id,
    )
    assert executor.dispatch_view(approved.run_id).status == "queued"

    executor.start()
    assert executor.wait_idle(3)

    assert app.state.job_service.get(job.id).state is JobState.needs_human
    assert AgentRunStore(database).get_run(approved.run_id).state == "needs_human"
    dispatch = executor.dispatch_view(approved.run_id)
    assert dispatch.status == "completed"
    assert dispatch.outcome_state == "needs_human"
    executor.close()


def test_restart_projects_already_durable_human_wait_before_closing_dispatch(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    job, source, human = _seed_wait(app)
    executor = app.state.agent_continuation_executor
    database = app.state.database
    assert isinstance(executor, AgentContinuationExecutor)
    assert database is not None

    approved = executor.coordinator.approve_and_create_continuation(
        source.run_id,
        human.id,
    )
    AgentRunStore(database).set_state(
        approved.run_id,
        AgentRunState.needs_human,
        error_category="approval_required",
        error_detail="durable wait committed before crash",
    )

    executor.start()
    assert executor.wait_idle(3)

    current = app.state.job_service.get(job.id)
    assert current.state is JobState.needs_human
    assert current.lease_expires_at is None
    dispatch = executor.dispatch_view(approved.run_id)
    assert dispatch.status == "completed"
    assert dispatch.outcome_state == "needs_human"
    executor.close()


def test_restart_projects_already_durable_terminal_failure_before_closing_dispatch(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    job, source, human = _seed_wait(app)
    executor = app.state.agent_continuation_executor
    database = app.state.database
    assert isinstance(executor, AgentContinuationExecutor)
    assert database is not None

    approved = executor.coordinator.approve_and_create_continuation(
        source.run_id,
        human.id,
    )
    store = AgentRunStore(database)
    store.append_step(
        approved.run_id,
        kind="error",
        status="failed",
        error_category="executor_crash_probe",
        error_detail="terminal proof committed before projection",
    )
    store.set_state(
        approved.run_id,
        AgentRunState.failed,
        error_category="executor_crash_probe",
        error_detail="terminal proof committed before projection",
    )

    executor.start()
    assert executor.wait_idle(3)

    current = app.state.job_service.get(job.id)
    assert current.state is JobState.failed
    assert current.lease_expires_at is None
    dispatch = executor.dispatch_view(approved.run_id)
    assert dispatch.status == "completed"
    assert dispatch.outcome_state == "failed"
    executor.close()


@pytest.mark.anyio
async def test_approve_endpoint_durably_enqueues_and_executes_continuation(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    job, source, human = _seed_wait(app)
    calls: dict[str, int] = {}
    model = FinishModel()
    executor = _executor(app, calls, model)
    database = app.state.database
    assert database is not None
    app.state.agent_continuation_executor = executor
    app.state.agent_workbench_actions = AgentWorkbenchActionService(
        database,
        continuation_executor=executor,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        capabilities = await client.get("/api/v1/agent-runtime/operator-capabilities")
        before_actions = await client.get(
            "/api/v1/agent-runtime/human-actions", params={"status": "pending"}
        )
        response = await client.post(
            f"/api/v1/agent-runtime/human-actions/{human.id}/approve",
            json={"note": "approved exactly"},
        )
        assert executor.wait_idle(3)
        job_view = await client.get(f"/api/v1/agent-runtime/jobs/{job.id}")
        runs = await client.get("/api/v1/agent-runtime/runs")
        second = await client.post(
            f"/api/v1/agent-runtime/human-actions/{human.id}/approve",
            json={},
        )

    assert capabilities.status_code == 200
    assert capabilities.json()["approve_continuation"] is True
    assert capabilities.json()["continuation_reason"] is None
    assert before_actions.json()[0]["can_approve"] is True
    assert response.status_code == 200
    body = response.json()
    assert body["human_action_id"] == human.id
    assert body["job_id"] == job.id
    assert body["source_run_id"] == source.run_id
    assert body["human_action_status"] == "approved"
    assert body["continuation_enqueued"] is True
    continuation_run_id = body["continuation_run_id"]

    assert calls == {"count": 1, "value": 7}
    assert model.calls == 1
    assert job_view.json()["job_state"] == "succeeded"
    assert job_view.json()["current_run_id"] == continuation_run_id
    run_rows = {row["run_id"]: row for row in runs.json()}
    assert run_rows[source.run_id]["state"] == "needs_human"
    assert run_rows[continuation_run_id]["state"] == "succeeded"
    dispatch = executor.dispatch_view(continuation_run_id)
    assert dispatch.status == "completed"
    assert dispatch.attempts == 1
    assert dispatch.outcome_state == "succeeded"
    assert second.status_code == 409
    executor.close()


@pytest.mark.anyio
async def test_approval_and_dispatch_are_one_durable_transaction(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    job, source, human = _seed_wait(app)
    database = app.state.database
    jobs = app.state.job_service
    assert database is not None
    assert jobs is not None

    approved = JobContinuationCoordinator(
        database,
        run_id_factory=lambda: "child-durable-dispatch",
    ).approve_and_create_continuation(source.run_id, human.id)

    with database.sessions() as session:
        dispatch = session.get(AgentContinuationDispatchRecord, approved.run_id)
        assert dispatch is not None
        assert dispatch.status == "queued"
        assert dispatch.attempts == 0
        assert dispatch.lease_expires_at is None
        assert dispatch.outcome_state is None
    assert jobs.get(job.id).state is JobState.running
    assert AgentRunStore(database).get_run(approved.run_id).state == "running"


@pytest.mark.anyio
async def test_executor_restart_requeues_durable_running_dispatch_without_reapproval(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    job, source, human = _seed_wait(app)
    database = app.state.database
    assert database is not None
    approved = JobContinuationCoordinator(
        database,
        run_id_factory=lambda: "child-after-restart",
    ).approve_and_create_continuation(source.run_id, human.id)
    with database.sessions.begin() as session:
        session.execute(
            update(AgentContinuationDispatchRecord)
            .where(AgentContinuationDispatchRecord.run_id == approved.run_id)
            .values(status="running", attempts=1, lease_expires_at=approved.lease_expires_at)
        )

    calls: dict[str, int] = {}
    model = FinishModel()
    executor = _executor(app, calls, model)
    executor.start()
    assert executor.wait_idle(3)

    assert calls["count"] == 1
    assert app.state.job_service.get(job.id).state is JobState.succeeded
    dispatch = executor.dispatch_view(approved.run_id)
    assert dispatch.status == "completed"
    assert dispatch.attempts == 2
    assert dispatch.outcome_state == "succeeded"
    executor.close()


@pytest.mark.anyio
async def test_unsupported_tool_is_rejected_before_durable_reclaim(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    job, source, human = _seed_wait(app, tool_name="unsupported.write")
    database = app.state.database
    assert database is not None
    calls: dict[str, int] = {}
    executor = _executor(app, calls, FinishModel())
    app.state.agent_continuation_executor = executor
    app.state.agent_workbench_actions = AgentWorkbenchActionService(
        database,
        continuation_executor=executor,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            f"/api/v1/agent-runtime/human-actions/{human.id}/approve",
            json={},
        )

    assert response.status_code == 409
    assert "unavailable or invalid" in response.json()["detail"]
    assert app.state.job_service.get(job.id).state is JobState.needs_human
    assert AgentRunStore(database).get_run(source.run_id).state == "needs_human"
    with database.sessions() as session:
        dispatches = list(session.scalars(select(AgentContinuationDispatchRecord)))
    assert dispatches == []
    assert calls == {}
    executor.close()


@pytest.mark.anyio
async def test_manual_chatgpt_wait_cannot_be_approved_by_auto_executor(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    job, source, human = _seed_wait(
        app,
        tool_name="manual_chatgpt",
        error_category="manual_chatgpt_required",
    )
    calls: dict[str, int] = {}
    executor = _executor(app, calls, FinishModel())
    database = app.state.database
    assert database is not None
    app.state.agent_continuation_executor = executor
    app.state.agent_workbench_actions = AgentWorkbenchActionService(
        database,
        continuation_executor=executor,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            f"/api/v1/agent-runtime/human-actions/{human.id}/approve",
            json={},
        )
        action_rows = await client.get(
            "/api/v1/agent-runtime/human-actions", params={"status": "pending"}
        )

    assert response.status_code == 409
    assert "approval_required" in response.json()["detail"]
    assert app.state.job_service.get(job.id).state is JobState.needs_human
    assert AgentRunStore(database).get_run(source.run_id).state == "needs_human"
    assert action_rows.json()[0]["can_approve"] is False
    executor.close()
