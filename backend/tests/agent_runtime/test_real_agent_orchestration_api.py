from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

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
from backend.app.agent_runtime.initial_chatgpt_decision import (
    AgentInitialChatGPTDecisionReconciler,
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
from backend.app.agent_runtime.manual_chatgpt_handoff import (
    MANUAL_CHATGPT_REQUIRED,
    MANUAL_CHATGPT_RESULT_READY,
    ManualChatGPTHandoffRecord,
    ManualChatGPTHandoffService,
)
from backend.app.agent_runtime.orchestration_service import AgentOrchestrationService
from backend.app.agent_runtime.physical_tools import build_shop_preflight_tool
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


class FakePhysicalRadar:
    def next_preflight_candidate(self, *, source_date):
        return SimpleNamespace(
            user_id="account-a",
            account_name="账号A",
            candidate_position=1,
            source_date=source_date,
        )


class FakePhysicalShop:
    def __init__(self) -> None:
        self.enqueued = []
        self.device_adapter = SimpleNamespace(
            health=lambda: SimpleNamespace(status="available", device_id="device-1", detail="ready")
        )

    def enqueue(self, payload, *, agent_origin=None):
        self.enqueued.append((payload, agent_origin))
        return SimpleNamespace(job_id="must-not-be-created-yet", status="queued")


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


def _evidence(
    evidence_id: str = "rank-item:1",
    *,
    source_date: str | None = None,
) -> AnalysisEvidenceRead:
    return AnalysisEvidenceRead(
        evidence_id=evidence_id,
        kind="rank_item",
        account_user_id="account-a",
        source_date=source_date,
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
async def test_grounded_start_without_bailian_creates_initial_chatgpt_handoff_atomically(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path))
    initial = app.state.agent_initial_executor
    assert isinstance(initial, AgentInitialExecutor)
    assert initial.accepting is False
    handoffs = app.state.agent_manual_handoff_service
    assert isinstance(handoffs, ManualChatGPTHandoffService)
    app.state.agent_orchestration_service = AgentOrchestrationService(
        analysis_service=NeverCalledAnalysisService(
            [_evidence(source_date="2026-08-24")]
        ),
        initial_executor=initial,
        manual_handoff_service=handoffs,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        capabilities = await client.get("/api/v1/agent-runtime/operator-capabilities")
        response = await client.post(
            "/api/v1/agent-runtime/jobs",
            json={
                "goal": "检查这条候选并决定是否需要真实店铺预检",
                "evidence_ids": ["rank-item:1"],
            },
        )

    assert capabilities.status_code == 200
    assert capabilities.json()["start_grounded_orchestration"] is True
    assert response.status_code == 202
    body = response.json()
    assert body["dispatch_enqueued"] is False
    assert body["handoff_created"] is True
    assert isinstance(body["handoff_id"], str)

    jobs = app.state.job_service
    assert jobs is not None
    job = jobs.get(body["job_id"])
    run = AgentRunStore(app.state.database).get_run(body["run_id"])
    assert job.state is JobState.needs_human
    assert job.current_stage == MANUAL_CHATGPT_REQUIRED
    assert job.error_category == MANUAL_CHATGPT_REQUIRED
    assert job.lease_expires_at is None
    assert run.state == AgentRunState.needs_human.value
    assert run.error_category == MANUAL_CHATGPT_REQUIRED
    assert run.model_calls == 0

    steps = AgentRunStore(app.state.database).list_steps(run.id)
    assert [step.kind for step in steps] == ["checkpoint", "external_handoff"]
    assert steps[0].evidence_refs_json == ["rank-item:1"]
    assert steps[0].output_json["evidence_summaries"][0]["source_date"] == "2026-08-24"
    with pytest.raises(KeyError):
        initial.dispatch_view(run.id)

    with app.state.database.sessions() as session:
        handoff = session.get(ManualChatGPTHandoffRecord, body["handoff_id"])
        assert handoff is not None
        assert handoff.status == "pending"
        assert handoff.task_json["kind"] == "initial_agent_next_action"
        assert handoff.task_json["supported_tool_names"] == ["shop.preflight"]
        package_path = app.state.settings.runtime_dir / handoff.package_path
    assert package_path.is_file()


def test_accepted_initial_chatgpt_preflight_proposal_only_creates_human_approval(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "runtime"
    database = Database(runtime_dir / "workbench.sqlite3", runtime_dir=runtime_dir)
    initial = AgentInitialExecutor(
        database,
        runtime_factory=lambda: (_ for _ in ()).throw(AssertionError("no initial dispatch")),
        launch_enabled=False,
    )
    handoffs = ManualChatGPTHandoffService(database, runtime_dir=runtime_dir)
    service = AgentOrchestrationService(
        analysis_service=NeverCalledAnalysisService(
            [_evidence(source_date="2026-08-24")]
        ),
        initial_executor=initial,
        manual_handoff_service=handoffs,
    )
    started = service.start_grounded_analysis(
        goal="检查候选并决定是否执行真实店铺预检",
        evidence_ids=["rank-item:1"],
    )
    assert started.handoff_created is True
    assert started.handoff_id is not None

    with database.sessions() as session:
        record = session.get(ManualChatGPTHandoffRecord, started.handoff_id)
        assert record is not None
        input_hash = record.input_hash
        schema_version = record.schema_version
    return_path = runtime_dir / "external-results" / f"{started.handoff_id}.json"
    return_path.parent.mkdir(parents=True, exist_ok=True)
    return_path.write_text(
        json.dumps(
            {
                "handoff_id": started.handoff_id,
                "schema_version": schema_version,
                "input_hash": input_hash,
                "result": {
                    "action": "tool",
                    "tool_name": "shop.preflight",
                    "arguments": {
                        "source_date": "2026-08-24",
                        "account_user_id": "account-a",
                    },
                    "tool_call_id": "chatgpt-preflight-proposal",
                    "final_output": None,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    accepted = handoffs.accept_return_file(started.handoff_id)
    assert accepted.source_run_id == started.run_id
    assert AgentRunStore(database).get_run(started.run_id).error_category == MANUAL_CHATGPT_RESULT_READY

    radar = FakePhysicalRadar()
    shop = FakePhysicalShop()
    continuations = JobContinuationCoordinator(database)

    def runtime_factory() -> JobBoundAgentRuntime:
        tools = ToolRegistry()
        tools.register(build_shop_preflight_tool(radar_service=radar, shop_service=shop))
        return JobBoundAgentRuntime(
            store=AgentRunStore(database),
            model=SequenceModel(),
            tools=tools,
            permissions=RuleBasedPermissionPolicy(),
            authority_guard=ActiveRunJobAuthorityGuard(database),
            wait_projector=JobHumanWaitProjector(database),
            continuation_resolver=continuations,
            context_builder=JobHistoryContextBuilder(database),
        )

    applied = AgentInitialChatGPTDecisionReconciler(
        database,
        runtime_factory=runtime_factory,
    ).reconcile_once()

    assert len(applied) == 1
    assert applied[0].status == "approval_required"
    assert applied[0].human_action_id is not None
    assert shop.enqueued == []
    job = JobService(database, runtime_dir=runtime_dir).get(started.job_id)
    run = AgentRunStore(database).get_run(started.run_id)
    assert job.state is JobState.needs_human
    assert job.current_stage == "approval_required"
    assert job.error_category == "approval_required"
    assert run.state == AgentRunState.needs_human.value
    assert run.error_category == "approval_required"
    [human] = [row for row in _human_actions(database, run.id) if row.status == "pending"]
    assert human.tool_name == "shop.preflight"
    assert human.request_json["proposed_by"] == "chatgpt_handoff"
    assert human.request_json["action"]["arguments"] == {
        "source_date": "2026-08-24",
        "account_user_id": "account-a",
    }


def test_accepted_initial_chatgpt_finish_closes_job_once_without_runtime_or_tool(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "runtime"
    database = Database(runtime_dir / "workbench.sqlite3", runtime_dir=runtime_dir)
    initial = AgentInitialExecutor(
        database,
        runtime_factory=lambda: (_ for _ in ()).throw(AssertionError("no initial runtime")),
        launch_enabled=False,
    )
    handoffs = ManualChatGPTHandoffService(database, runtime_dir=runtime_dir)
    service = AgentOrchestrationService(
        analysis_service=NeverCalledAnalysisService(
            [_evidence(source_date="2026-08-24")]
        ),
        initial_executor=initial,
        manual_handoff_service=handoffs,
    )
    started = service.start_grounded_analysis(
        goal="检查证据并决定是否还需要动作",
        evidence_ids=["rank-item:1"],
    )
    assert started.handoff_id is not None

    with database.sessions() as session:
        record = session.get(ManualChatGPTHandoffRecord, started.handoff_id)
        assert record is not None
        input_hash = record.input_hash
        schema_version = record.schema_version
    return_path = runtime_dir / "external-results" / f"{started.handoff_id}.json"
    return_path.parent.mkdir(parents=True, exist_ok=True)
    return_path.write_text(
        json.dumps(
            {
                "handoff_id": started.handoff_id,
                "schema_version": schema_version,
                "input_hash": input_hash,
                "result": {
                    "action": "finish",
                    "tool_name": None,
                    "arguments": {},
                    "tool_call_id": "chatgpt-finish",
                    "final_output": {"decision": "no_further_action"},
                },
            }
        ),
        encoding="utf-8",
    )
    handoffs.accept_return_file(started.handoff_id)

    reconciler = AgentInitialChatGPTDecisionReconciler(
        database,
        runtime_factory=lambda: (_ for _ in ()).throw(
            AssertionError("finish must not build or execute an Agent runtime")
        ),
    )
    [applied] = reconciler.reconcile_once()

    assert applied.status == "finished"
    assert applied.continuation_run_id is not None
    job = JobService(database, runtime_dir=runtime_dir).get(started.job_id)
    assert job.state is JobState.succeeded
    assert AgentRunStore(database).get_run(applied.continuation_run_id).final_output_json == {
        "decision": "no_further_action"
    }
    assert reconciler.reconcile_once() == []


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
