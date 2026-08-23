from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from backend.app.agent_runtime import (
    AgentRunState,
    AgentRunStore,
    ModelTurn,
    NextAction,
    RuleBasedPermissionPolicy,
    ToolRegistry,
)
from backend.app.agent_runtime.continuation_executor import AgentContinuationExecutor
from backend.app.agent_runtime.domain_tools import build_analysis_run_tool
from backend.app.agent_runtime.initial_execution import (
    AgentInitialDispatchRecord,
    AgentInitialExecutor,
    AgentInitialLaunchCoordinator,
)
from backend.app.agent_runtime.job_bound_runtime import (
    ActiveRunJobAuthorityGuard,
    JobBoundAgentRuntime,
)
from backend.app.agent_runtime.job_continuation import (
    ContinuationApprovalError,
    JobContinuationCoordinator,
    JobHistoryContextBuilder,
)
from backend.app.agent_runtime.job_wait_projection import JobHumanWaitProjector
from backend.app.agent_runtime.orchestration_service import AgentOrchestrationService
from backend.app.agent_runtime.persistence import AgentRunRecord, HumanActionRecord
from backend.app.agent_runtime.types import RunBudget
from backend.app.db import Database
from backend.app.features.analysis.schemas import AnalysisEvidenceRead
from backend.app.main import create_app
from backend.app.models.jobs import JobState
from backend.app.settings import Settings
from backend.app.services.jobs import JobNotFound, JobService


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class SequenceModel:
    def __init__(self, *actions: NextAction) -> None:
        self.actions = list(actions)
        self.contexts = []

    def next_action(self, context):
        self.contexts.append(context)
        if not self.actions:
            raise AssertionError("unexpected extra Agent model call")
        return ModelTurn(
            action=self.actions.pop(0),
            input_tokens=3,
            output_tokens=2,
            model_name="stage5-sequence-model",
        )


class NeverCalledAnalysisService:
    def __init__(self, evidence: list[AnalysisEvidenceRead] | None = None) -> None:
        self.evidence = evidence or []
        self.create_calls = []

    def list_evidence(self):
        return list(self.evidence)

    def create(self, payload):
        self.create_calls.append(payload)
        raise AssertionError("grounded analysis must wait for explicit approval")


def _settings(tmp_path: Path) -> Settings:
    runtime = tmp_path / "runtime"
    return Settings(runtime_dir=runtime, database_path=runtime / "workbench.sqlite3")


def _runtime_factory(
    database: Database,
    model: SequenceModel,
    analysis_service: NeverCalledAnalysisService | None = None,
):
    continuations = JobContinuationCoordinator(database)

    def build() -> JobBoundAgentRuntime:
        tools = ToolRegistry()
        if analysis_service is not None:
            tools.register(build_analysis_run_tool(analysis_service))
        return JobBoundAgentRuntime(
            store=AgentRunStore(database),
            model=model,
            tools=tools,
            permissions=RuleBasedPermissionPolicy(),
            authority_guard=ActiveRunJobAuthorityGuard(database),
            wait_projector=JobHumanWaitProjector(database),
            continuation_resolver=continuations,
            context_builder=JobHistoryContextBuilder(database),
        )

    return build


def _evidence(evidence_id: str = "rank-item:1") -> AnalysisEvidenceRead:
    return AnalysisEvidenceRead(
        evidence_id=evidence_id,
        kind="rank_item",
        account_user_id="account-a",
        eligible_for_opportunity=False,
    )


def _human_actions(database: Database, run_id: str) -> list[HumanActionRecord]:
    with database.sessions() as session:
        rows = list(session.query(HumanActionRecord).filter_by(run_id=run_id))
        for row in rows:
            session.expunge(row)
        return rows


def _launch(
    executor: AgentInitialExecutor,
    *,
    evidence_id: str = "rank-item:1",
):
    return executor.launch(
        goal="inspect existing evidence and decide whether grounded analysis is needed",
        evidence_refs=[evidence_id],
        evidence_summaries=[
            {
                "evidence_id": evidence_id,
                "kind": "rank_item",
                "account_user_id": "account-a",
                "eligible_for_opportunity": False,
            }
        ],
        budget=RunBudget(
            max_steps=8,
            max_model_calls=4,
            max_input_tokens=2000,
            max_output_tokens=1000,
            max_wall_time_seconds=30,
        ),
    )


def test_initial_launch_atomically_seeds_job_run_evidence_and_dispatch(tmp_path: Path) -> None:
    database = Database(tmp_path / "workbench.sqlite3")
    coordinator = AgentInitialLaunchCoordinator(
        database,
        job_id_factory=lambda: "job-initial",
        run_id_factory=lambda: "run-initial",
    )

    launch = coordinator.create_and_enqueue(
        goal="grounded initial run",
        evidence_refs=["rank-item:1"],
        evidence_summaries=[
            {
                "evidence_id": "rank-item:1",
                "kind": "rank_item",
                "account_user_id": "account-a",
                "eligible_for_opportunity": False,
            }
        ],
        budget=RunBudget(max_wall_time_seconds=30),
    )

    from backend.app.services.jobs import JobService

    job = JobService(database).get(launch.job_id)
    run = AgentRunStore(database).get_run(launch.run_id)
    [seed] = AgentRunStore(database).list_steps(launch.run_id)
    dispatch = AgentInitialExecutor(
        database,
        runtime_factory=lambda: (_ for _ in ()).throw(AssertionError("not executing")),
        launch_enabled=False,
    ).dispatch_view(launch.run_id)

    assert job.state is JobState.running
    assert job.type == "agent_orchestration"
    assert run.state == "running"
    assert run.step_count == 1
    assert seed.kind == "checkpoint"
    assert seed.status == "succeeded"
    assert seed.evidence_refs_json == ["rank-item:1"]
    assert seed.output_json == {
        "scope": "operator_selected_evidence",
        "evidence_count": 1,
        "evidence_summaries": [
            {
                "evidence_id": "rank-item:1",
                "kind": "rank_item",
                "account_user_id": "account-a",
                "eligible_for_opportunity": False,
            }
        ],
    }
    assert dispatch.job_id == job.id
    assert dispatch.status == "queued"
    assert dispatch.attempts == 0


def test_initial_launch_rolls_back_job_when_agent_run_insert_fails(tmp_path: Path) -> None:
    database = Database(tmp_path / "workbench.sqlite3")
    store = AgentRunStore(database)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with database.sessions.begin() as session:
        session.add(
            AgentRunRecord(
                id="duplicate-run",
                goal="preexisting run",
                state="running",
                model_name=None,
                prompt_version=None,
                budget_json=RunBudget(max_wall_time_seconds=30).model_dump(mode="json"),
                created_at=now,
                updated_at=now,
            )
        )
    coordinator = AgentInitialLaunchCoordinator(
        database,
        job_id_factory=lambda: "job-must-rollback",
        run_id_factory=lambda: "duplicate-run",
    )

    with pytest.raises(IntegrityError):
        coordinator.create_and_enqueue(
            goal="must roll back atomically",
            evidence_refs=["rank-item:1"],
            evidence_summaries=[
                {
                    "evidence_id": "rank-item:1",
                    "kind": "rank_item",
                    "account_user_id": "account-a",
                    "eligible_for_opportunity": False,
                }
            ],
            budget=RunBudget(max_wall_time_seconds=30),
        )

    with pytest.raises(JobNotFound):
        JobService(database).get("job-must-rollback")
    assert store.get_run("duplicate-run").goal == "preexisting run"


def test_initial_executor_runs_from_seeded_evidence_and_projects_success(tmp_path: Path) -> None:
    database = Database(tmp_path / "workbench.sqlite3")
    model = SequenceModel(NextAction(action="finish", final_output={"result": "grounded"}))
    executor = AgentInitialExecutor(
        database,
        runtime_factory=_runtime_factory(database, model),
        poll_seconds=0.01,
    )

    launch = _launch(executor)
    assert executor.wait_idle(3)

    from backend.app.services.jobs import JobService

    assert JobService(database).get(launch.job_id).state is JobState.succeeded
    assert AgentRunStore(database).get_run(launch.run_id).state == "succeeded"
    assert model.contexts
    assert model.contexts[0].recent_steps[0].evidence_refs == ["rank-item:1"]
    dispatch = executor.dispatch_view(launch.run_id)
    assert dispatch.status == "completed"
    assert dispatch.attempts == 1
    assert dispatch.outcome_state == "succeeded"
    executor.close()


def test_initial_executor_restart_never_replays_a_started_dispatch(tmp_path: Path) -> None:
    database = Database(tmp_path / "workbench.sqlite3")
    coordinator = AgentInitialLaunchCoordinator(
        database,
        job_id_factory=lambda: "job-restart",
        run_id_factory=lambda: "run-restart",
    )
    launch = coordinator.create_and_enqueue(
        goal="do not replay after process loss",
        evidence_refs=["rank-item:1"],
        evidence_summaries=[
            {
                "evidence_id": "rank-item:1",
                "kind": "rank_item",
                "account_user_id": "account-a",
                "eligible_for_opportunity": False,
            }
        ],
        budget=RunBudget(max_wall_time_seconds=30),
    )
    with database.sessions.begin() as session:
        session.execute(
            update(AgentInitialDispatchRecord)
            .where(AgentInitialDispatchRecord.run_id == launch.run_id)
            .values(status="running", attempts=1, lease_expires_at=launch.lease_expires_at)
        )

    model = SequenceModel(NextAction(action="finish", final_output={"must": "not run"}))
    restarted = AgentInitialExecutor(
        database,
        runtime_factory=_runtime_factory(database, model),
        poll_seconds=0.01,
    )
    restarted.start()
    assert restarted.wait_idle(3)

    from backend.app.services.jobs import JobService

    assert model.contexts == []
    assert AgentRunStore(database).get_run(launch.run_id).state == "needs_human"
    assert JobService(database).get(launch.job_id).state is JobState.needs_human
    dispatch = restarted.dispatch_view(launch.run_id)
    assert dispatch.status == "completed"
    assert dispatch.attempts == 1
    assert dispatch.outcome_state == "needs_human"
    restarted.close()


def test_initial_agent_analysis_proposal_waits_for_human_before_domain_service(tmp_path: Path) -> None:
    database = Database(tmp_path / "workbench.sqlite3")
    analysis = NeverCalledAnalysisService()
    model = SequenceModel(
        NextAction(
            action="tool",
            tool_name="analysis.run_grounded",
            tool_call_id="analysis-request",
            arguments={
                "analysis_type": "account_report",
                "account_user_id": "account-a",
                "account_user_ids": [],
                "evidence_ids": ["rank-item:1"],
            },
        )
    )
    executor = AgentInitialExecutor(
        database,
        runtime_factory=_runtime_factory(database, model, analysis),
        poll_seconds=0.01,
    )

    launch = _launch(executor)
    assert executor.wait_idle(3)

    from backend.app.services.jobs import JobService

    assert JobService(database).get(launch.job_id).state is JobState.needs_human
    run = AgentRunStore(database).get_run(launch.run_id)
    assert run.state == "needs_human"
    assert run.error_category == "approval_required"
    assert analysis.create_calls == []
    rows = _human_actions(database, launch.run_id)
    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert rows[0].tool_name == "analysis.run_grounded"
    executor.close()


def test_approval_rejects_analysis_evidence_scope_expansion_before_reclaim(tmp_path: Path) -> None:
    database = Database(tmp_path / "workbench.sqlite3")
    analysis = NeverCalledAnalysisService()
    model = SequenceModel(
        NextAction(
            action="tool",
            tool_name="analysis.run_grounded",
            tool_call_id="scope-expansion",
            arguments={
                "analysis_type": "account_report",
                "account_user_id": "account-a",
                "account_user_ids": [],
                "evidence_ids": ["rank-item:2"],
            },
        )
    )
    runtime_factory = _runtime_factory(database, model, analysis)
    initial = AgentInitialExecutor(database, runtime_factory=runtime_factory, poll_seconds=0.01)
    launch = _launch(initial, evidence_id="rank-item:1")
    assert initial.wait_idle(3)
    initial.close()

    [human] = _human_actions(database, launch.run_id)
    continuation = AgentContinuationExecutor(
        database,
        runtime_factory=runtime_factory,
        poll_seconds=0.01,
    )
    with pytest.raises(ContinuationApprovalError, match="durable Job history"):
        continuation.approve(human.id)

    from backend.app.services.jobs import JobService

    assert JobService(database).get(launch.job_id).state is JobState.needs_human
    assert AgentRunStore(database).get_run(launch.run_id).state == "needs_human"
    assert _human_actions(database, launch.run_id)[0].status == "pending"
    assert analysis.create_calls == []
    continuation.close()


@pytest.mark.anyio
async def test_grounded_agent_start_api_uses_existing_evidence_and_is_auditable(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    database = app.state.database
    assert database is not None
    model = SequenceModel(NextAction(action="finish", final_output={"done": True}))
    initial = AgentInitialExecutor(
        database,
        runtime_factory=_runtime_factory(database, model),
        poll_seconds=0.01,
    )
    analysis = NeverCalledAnalysisService([_evidence()])
    app.state.agent_initial_executor = initial
    app.state.agent_orchestration_service = AgentOrchestrationService(
        analysis_service=analysis,
        initial_executor=initial,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/api/v1/agent-runtime/jobs",
            json={
                "goal": "inspect the selected durable evidence",
                "evidence_ids": ["rank-item:1"],
            },
        )
        assert initial.wait_idle(3)
        jobs = await client.get("/api/v1/agent-runtime/jobs")
        runs = await client.get("/api/v1/agent-runtime/runs")

    assert response.status_code == 202
    body = response.json()
    assert body["dispatch_enqueued"] is True
    assert body["evidence_count"] == 1
    assert any(row["job_id"] == body["job_id"] for row in jobs.json())
    assert any(row["run_id"] == body["run_id"] for row in runs.json())
    initial.close()


@pytest.mark.anyio
async def test_grounded_agent_start_rejects_unknown_evidence_and_generic_job_bypass(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    database = app.state.database
    assert database is not None
    model = SequenceModel(NextAction(action="finish", final_output={"done": True}))
    initial = AgentInitialExecutor(
        database,
        runtime_factory=_runtime_factory(database, model),
        poll_seconds=0.01,
    )
    app.state.agent_initial_executor = initial
    app.state.agent_orchestration_service = AgentOrchestrationService(
        analysis_service=NeverCalledAnalysisService([_evidence()]),
        initial_executor=initial,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        unknown = await client.post(
            "/api/v1/agent-runtime/jobs",
            json={"goal": "should not start", "evidence_ids": ["rank-item:999"]},
        )
        bypass = await client.post(
            "/api/v1/jobs",
            json={"type": "agent_orchestration", "input": {}},
        )

    assert unknown.status_code == 409
    assert "Unknown or currently unavailable durable evidence" in unknown.json()["detail"]
    assert bypass.status_code == 422
    assert "agent-runtime/jobs" in bypass.json()["detail"]
    assert app.state.agent_workbench_reader.list_jobs() == []
    initial.close()


@pytest.mark.anyio
async def test_fastapi_lifespan_owns_initial_and_continuation_executors(tmp_path: Path) -> None:
    settings = _settings(tmp_path).model_copy(update={"bailian_api_key": "test-only-key"})
    app = create_app(settings)
    initial = app.state.agent_initial_executor
    continuation = app.state.agent_continuation_executor
    assert isinstance(initial, AgentInitialExecutor)
    assert isinstance(continuation, AgentContinuationExecutor)
    assert initial.is_alive is False
    assert continuation.is_alive is False
    assert app.state.agent_workbench_actions.capabilities()["start_grounded_orchestration"] is True

    async with app.router.lifespan_context(app):
        assert initial.is_alive is True
        assert continuation.is_alive is True

    assert initial.is_alive is False
    assert continuation.is_alive is False
