from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from backend.app.agent_runtime import (
    AgentRunState,
    AgentRunStore,
    ModelTurn,
    NextAction,
    RuleBasedPermissionPolicy,
    RunBudget,
    ToolRegistry,
)
from backend.app.agent_runtime.continuation_executor import AgentContinuationExecutor
from backend.app.agent_runtime.chatgpt_scope_result import AgentChatGPTScopeResultReconciler
from backend.app.agent_runtime.job_binding import AgentJobCoordinator
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
from backend.app.agent_runtime.manual_chatgpt_handoff import ManualChatGPTHandoffRecord
from backend.app.agent_runtime.physical_followup import (
    AgentPhysicalFollowupReconciler,
    PHYSICAL_JOB_PENDING,
)
from backend.app.agent_runtime.physical_tools import (
    SHOP_PREFLIGHT_TOOL_NAME,
    ShopPreflightInput,
    ShopPreflightTargetChanged,
    build_shop_preflight_tool,
)
from backend.app.agent_runtime.production_runtime import build_production_job_bound_runtime
from backend.app.agent_runtime.tools import ToolNeedsHumanError, ToolUnavailableError
from backend.app.agent_runtime.workbench_read import AgentWorkbenchReader
from backend.app.db import Database
from backend.app.features.analysis.service import AnalysisService
from backend.app.features.radar.models import RankItemRecord, RankSnapshotRecord
from backend.app.features.shops.service import ShopCollectionService
from backend.app.models.jobs import JobArtifactRecord, JobState
from backend.app.services.jobs import JobService


class FakeDeviceAdapter:
    def __init__(self, status: str = "available") -> None:
        self.status = status

    def health(self):
        return SimpleNamespace(status=self.status, device_id="device-1", detail="ready")


class FakeRadarService:
    def __init__(self, account_user_id: str = "account-a") -> None:
        self.account_user_id = account_user_id

    def next_preflight_candidate(self, *, source_date):
        return SimpleNamespace(
            user_id=self.account_user_id,
            account_name=f"name-{self.account_user_id}",
            candidate_position=2,
            source_date=source_date,
        )


class FakeShopService:
    def __init__(self, *, device_status: str = "available") -> None:
        self.device_adapter = FakeDeviceAdapter(device_status)
        self.enqueued = []
        self.origins = []

    def enqueue(self, payload, *, agent_origin=None):
        self.enqueued.append(payload)
        self.origins.append(agent_origin)
        return SimpleNamespace(job_id="physical-job-1", status="queued")


class FinishModel:
    def __init__(self) -> None:
        self.calls = 0

    def next_action(self, _context):
        self.calls += 1
        return ModelTurn(
            action=NextAction(action="finish", final_output={"continued": True}),
            model_name="physical-boundary-test",
        )


class UnconfiguredBailian:
    configured = False

    def __init__(self) -> None:
        self.calls = 0

    def generate_structured(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("Bailian must not be called for approved shop.preflight")


def _tool(*, radar: FakeRadarService | None = None, shop: FakeShopService | None = None):
    return build_shop_preflight_tool(
        radar_service=radar or FakeRadarService(),
        shop_service=shop or FakeShopService(),
    )


def _seed_waiting_preflight(tmp_path: Path):
    database = Database(tmp_path / "workbench.sqlite3")
    jobs = JobService(database)
    job = jobs.create(job_type="agent_orchestration", input_data={})
    store = AgentRunStore(database)
    bound = AgentJobCoordinator(
        database,
        run_id_factory=lambda: "source-physical-run",
    ).claim_and_create_run(
        job.id,
        goal="检查下一候选并在获批后执行真实店铺预检",
        budget=RunBudget(
            max_steps=10,
            max_model_calls=5,
            max_input_tokens=1000,
            max_output_tokens=500,
            max_wall_time_seconds=30,
        ),
    )
    action = NextAction(
        action="tool",
        tool_name=SHOP_PREFLIGHT_TOOL_NAME,
        tool_call_id="physical-preflight-call",
        arguments={
            "source_date": "2026-08-24",
            "account_user_id": "account-a",
        },
    )
    human = store.create_human_action(
        run_id=bound.run_id,
        tool_call_id=action.tool_call_id,
        tool_name=SHOP_PREFLIGHT_TOOL_NAME,
        request_json={"action": action.model_dump(mode="json"), "reason": "physical approval"},
    )
    store.set_state(
        bound.run_id,
        AgentRunState.needs_human,
        error_category="approval_required",
        error_detail="physical approval",
    )
    jobs.transition(
        job.id,
        JobState.needs_human,
        current_stage="approval_required",
        error_category="approval_required",
    )
    return database, jobs, job, store, bound, action, human


def test_shop_preflight_tool_is_permission_gated_external_and_health_bounded() -> None:
    radar = FakeRadarService()
    shop = FakeShopService()
    tool = _tool(radar=radar, shop=shop)

    assert tool.name == SHOP_PREFLIGHT_TOOL_NAME
    assert tool.requires_approval is True
    assert tool.external_side_effect is True
    assert tool.read_only is False
    assert tool.idempotent is False
    assert tool.retry_limit == 0
    assert tool.available() is True

    validated = ShopPreflightInput.model_validate(
        {"source_date": "2026-08-24", "account_user_id": "account-a"}
    )
    assert tool.pre_approval_validate is not None
    tool.pre_approval_validate(validated)
    registry = ToolRegistry()
    registry.register(tool)
    with pytest.raises(ToolNeedsHumanError) as error:
        registry.execute_validated(
            tool,
            validated,
            run_id="run-physical",
            tool_call_id="call-physical",
        )
    result = error.value.result
    assert result is not None

    assert len(shop.enqueued) == 1
    assert shop.origins == [("run-physical", "call-physical")]
    queued_payload = shop.enqueued[0]
    assert queued_payload.account_user_id == "account-a"
    assert queued_payload.account_name == "name-account-a"
    assert queued_payload.collection_mode == "preflight"
    assert result.output == {
        "job_id": "physical-job-1",
        "status": "queued",
        "source_date": "2026-08-24",
        "account_user_id": "account-a",
        "candidate_position": 2,
        "collection_mode": "preflight",
    }
    assert error.value.category == "physical_job_pending"

    unavailable = _tool(shop=FakeShopService(device_status="needs_human"))
    registry = ToolRegistry()
    registry.register(unavailable)
    with pytest.raises(ToolUnavailableError):
        registry.resolve(SHOP_PREFLIGHT_TOOL_NAME)


def test_shop_preflight_exact_target_must_still_be_next_candidate() -> None:
    radar = FakeRadarService(account_user_id="account-b")
    shop = FakeShopService()
    tool = _tool(radar=radar, shop=shop)
    validated = ShopPreflightInput.model_validate(
        {"source_date": "2026-08-24", "account_user_id": "account-a"}
    )

    assert tool.pre_approval_validate is not None
    with pytest.raises(ShopPreflightTargetChanged):
        tool.pre_approval_validate(validated)
    assert shop.enqueued == []


def test_stale_physical_target_is_rejected_before_approval_mutates_authority(tmp_path: Path) -> None:
    database, jobs, job, store, bound, _action, human = _seed_waiting_preflight(tmp_path)
    radar = FakeRadarService(account_user_id="account-b")
    shop = FakeShopService()
    registry = ToolRegistry()
    registry.register(_tool(radar=radar, shop=shop))

    executor = AgentContinuationExecutor(
        database,
        runtime_factory=lambda: SimpleNamespace(tools=registry),
        approval_enabled=True,
        poll_seconds=0.01,
    )
    try:
        with pytest.raises(ContinuationApprovalError):
            executor.approve(human.id)
    finally:
        executor.close()

    with database.sessions() as session:
        persisted_human = session.get(type(human), human.id)
        assert persisted_human is not None
        assert persisted_human.status == "pending"
    assert store.get_run(bound.run_id).state == AgentRunState.needs_human.value
    current_job = jobs.get(job.id)
    assert current_job.state is JobState.needs_human
    assert current_job.lease_expires_at is None
    assert shop.enqueued == []


def test_started_shop_preflight_is_never_replayed_after_uncertain_restart(tmp_path: Path) -> None:
    database, jobs, job, store, bound, action, human = _seed_waiting_preflight(tmp_path)
    continuations = JobContinuationCoordinator(
        database,
        run_id_factory=lambda: "continuation-physical-run",
    )
    approved = continuations.approve_and_create_continuation(bound.run_id, human.id)
    store.append_step(
        approved.run_id,
        kind="tool",
        status="started",
        tool_name=SHOP_PREFLIGHT_TOOL_NAME,
        tool_call_id=action.tool_call_id,
        input_json={
            "arguments": action.arguments,
            "idempotent": False,
            "timeout_seconds": 15.0,
        },
    )

    radar = FakeRadarService(account_user_id="account-a")
    shop = FakeShopService()
    registry = ToolRegistry()
    registry.register(_tool(radar=radar, shop=shop))
    model = FinishModel()
    runtime = JobBoundAgentRuntime(
        store=AgentRunStore(database),
        model=model,
        tools=registry,
        permissions=RuleBasedPermissionPolicy(),
        authority_guard=ActiveRunJobAuthorityGuard(database),
        wait_projector=JobHumanWaitProjector(database),
        continuation_resolver=continuations,
        context_builder=JobHistoryContextBuilder(database),
    )

    outcome = runtime.run_approved_continuation(approved.run_id)

    assert outcome.state is AgentRunState.needs_human
    assert outcome.error_category == "uncertain_tool_side_effect"
    assert shop.enqueued == []
    assert model.calls == 0
    current_job = jobs.get(job.id)
    assert current_job.state is JobState.needs_human
    assert current_job.lease_expires_at is None


def test_approved_shop_preflight_runs_without_bailian_and_stops_before_model(
    tmp_path: Path,
) -> None:
    database, jobs, job, store, _bound, _action, human = _seed_waiting_preflight(tmp_path)
    radar = FakeRadarService(account_user_id="account-a")
    shop = FakeShopService()
    bailian = UnconfiguredBailian()
    analysis = AnalysisService(database, object(), runtime_dir=tmp_path / "runtime")
    executor: AgentContinuationExecutor

    def runtime_factory():
        return build_production_job_bound_runtime(
            database=database,
            job_service=jobs,
            analysis_service=analysis,
            bailian_adapter=bailian,
            continuations=executor.coordinator,
            radar_service=radar,
            shop_service=shop,
        )

    executor = AgentContinuationExecutor(
        database,
        runtime_factory=runtime_factory,
        approval_enabled=False,
        approval_admission=lambda action: action.tool_name == SHOP_PREFLIGHT_TOOL_NAME,
        poll_seconds=0.01,
    )
    try:
        approved = executor.approve(human.id)
        assert executor.wait_idle(2.0) is True
    finally:
        executor.close()

    assert bailian.calls == 0
    assert len(shop.enqueued) == 1
    assert shop.origins == [(approved.run_id, "physical-preflight-call")]
    continuation = store.get_run(approved.run_id)
    assert continuation.state == AgentRunState.needs_human.value
    assert continuation.error_category == PHYSICAL_JOB_PENDING
    current_job = jobs.get(job.id)
    assert current_job.state is JobState.needs_human
    assert current_job.error_category == PHYSICAL_JOB_PENDING
    assert current_job.lease_expires_at is None


def test_workbench_exposes_safe_physical_approval_summary_without_raw_arguments(tmp_path: Path) -> None:
    database, _jobs, _job, _store, _bound, _action, human = _seed_waiting_preflight(tmp_path)

    projected = AgentWorkbenchReader(database).list_human_actions(status="pending")

    assert len(projected) == 1
    action = projected[0]
    assert action["id"] == human.id
    assert action["tool_name"] == SHOP_PREFLIGHT_TOOL_NAME
    assert action["external_side_effect"] is True
    assert action["approval_summary"] == (
        "将对账号 account-a 启动真实 Android 小红书店铺预检（候选日期 2026-08-24）。"
    )
    assert "request_json" not in action
    assert "resolution_json" not in action


def test_rank_evidence_exposes_safe_source_date_for_exact_physical_target(tmp_path: Path) -> None:
    database = Database(tmp_path / "workbench.sqlite3")
    with database.sessions.begin() as session:
        snapshot = RankSnapshotRecord(
            source_date="2026-08-24",
            collected_at="2026-08-24T00:20:00Z",
            board="阅读榜",
            dimension="优秀账号",
            source_url="https://example.com/rank",
            raw_evidence={"response_id": "safe-test"},
            submitted_count=1,
        )
        session.add(snapshot)
        session.flush()
        item = RankItemRecord(
            snapshot_id=snapshot.id,
            stable_key="account-a-rank",
            rank_no=1,
            user_id="account-a",
            source_url="https://example.com/account-a",
            raw_evidence={"row": 1},
        )
        session.add(item)
        session.flush()
        evidence_id = f"rank-item:{item.id}"

    analysis = AnalysisService(database, object(), runtime_dir=tmp_path / "runtime")
    evidence = {row.evidence_id: row for row in analysis.list_evidence()}

    assert evidence[evidence_id].account_user_id == "account-a"
    assert evidence[evidence_id].source_date == "2026-08-24"


def _seed_physical_result_wait(
    tmp_path: Path,
    *,
    with_scope: bool,
    scope_classification: str = "needs_human",
):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    database = Database(runtime_dir / "workbench.sqlite3", runtime_dir=runtime_dir)
    jobs = JobService(database, runtime_dir=runtime_dir)
    parent = jobs.create(job_type="agent_orchestration", input_data={})
    store = AgentRunStore(database)
    bound = AgentJobCoordinator(
        database,
        run_id_factory=lambda: "physical-result-source-run",
    ).claim_and_create_run(
        parent.id,
        goal="真实店铺预检后根据证据继续",
        budget=RunBudget(
            max_steps=10,
            max_model_calls=5,
            max_input_tokens=1000,
            max_output_tokens=500,
            max_wall_time_seconds=30,
        ),
    )
    child = jobs.create(
        job_type="android_shop_collection",
        input_data={
            "account_user_id": "account-a",
            "account_name": "账号A",
            "collection_mode": "preflight",
            "_agent_origin": {
                "kind": "agent_tool",
                "run_id": bound.run_id,
                "tool_call_id": "physical-preflight-call",
                "tool_name": SHOP_PREFLIGHT_TOOL_NAME,
            },
        },
        progress_total=3,
        current_stage="device_pending",
    )
    store.append_step(
        bound.run_id,
        kind="tool",
        status="needs_human",
        tool_name=SHOP_PREFLIGHT_TOOL_NAME,
        tool_call_id="physical-preflight-call",
        input_json={"arguments": {"account_user_id": "account-a"}},
        output_json={
            "output": {"job_id": child.id, "status": "queued"},
            "evidence_refs": [],
            "summary": f"shop_preflight_queued:{child.id}",
        },
        error_category=PHYSICAL_JOB_PENDING,
        error_detail="waiting for child",
    )
    store.set_state(
        bound.run_id,
        AgentRunState.needs_human,
        error_category=PHYSICAL_JOB_PENDING,
        error_detail="waiting for child",
    )
    jobs.transition(
        parent.id,
        JobState.needs_human,
        current_stage=PHYSICAL_JOB_PENDING,
        error_category=PHYSICAL_JOB_PENDING,
    )
    jobs.claim(child.id)
    screenshot_path = runtime_dir / "evidence" / "android" / child.id / "product-1.png"
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    screenshot_path.write_bytes(b"fake-png")
    screenshot = jobs.attach_artifact(
        child.id,
        kind="android_screenshot",
        path=f"evidence/android/{child.id}/product-1.png",
        metadata={"sha256": "abc"},
    )
    with database.sessions() as session:
        screenshot_id = session.scalar(
            select(JobArtifactRecord.id)
            .where(
                JobArtifactRecord.job_id == child.id,
                JobArtifactRecord.kind == "android_screenshot",
            )
            .order_by(JobArtifactRecord.id.desc())
            .limit(1)
        )
    assert screenshot_id is not None
    scope_artifact = None
    if with_scope:
        scope_path = runtime_dir / "evidence" / "shops" / child.id / "scope-gate.json"
        scope_path.parent.mkdir(parents=True, exist_ok=True)
        scope_path.write_text("{}", encoding="utf-8")
        scope_artifact = jobs.attach_artifact(
            child.id,
            kind="shop_scope_gate_result",
            path=f"evidence/shops/{child.id}/scope-gate.json",
            metadata={
                "result": {
                    "job_id": child.id,
                    "account_user_id": "account-a",
                    "classification": scope_classification,
                    "reason": (
                        "scope_evidence_ambiguous"
                        if scope_classification == "needs_human"
                        else "physical_goods_detected"
                    ),
                    "representative_product_count": 3,
                    "deep_collection_allowed": False,
                    "decision_source": "rule",
                    "evidence_refs": [f"artifact:{screenshot_id}"],
                }
            },
        )
        if scope_classification == "needs_human":
            jobs.transition(
                child.id,
                JobState.needs_human,
                current_stage="shop_needs_human",
                error_category="shop_scope_needs_human",
            )
        else:
            jobs.transition(
                child.id,
                JobState.succeeded,
                current_stage=f"scope_gate_{scope_classification}",
            )
    else:
        jobs.transition(
            child.id,
            JobState.needs_human,
            current_stage="shop_needs_human",
            error_category="selector_changed",
        )
    return database, jobs, parent, bound, child, screenshot_id, scope_artifact, runtime_dir


def test_ambiguous_physical_result_becomes_chatgpt_handoff_without_replay(tmp_path: Path) -> None:
    database, jobs, parent, bound, child, screenshot_id, scope_artifact, runtime_dir = (
        _seed_physical_result_wait(tmp_path, with_scope=True)
    )
    assert scope_artifact is not None

    reconciler = AgentPhysicalFollowupReconciler(database, runtime_dir=runtime_dir)
    first = reconciler.reconcile_once()

    assert len(first) == 1
    assert first[0].status == "chatgpt_handoff_created"
    assert first[0].child_job_id == child.id
    current_parent = jobs.get(parent.id)
    assert current_parent.state is JobState.needs_human
    assert current_parent.current_stage == "manual_chatgpt_required"
    assert current_parent.error_category == "manual_chatgpt_required"
    assert current_parent.lease_expires_at is None
    assert jobs.get(child.id).state is JobState.needs_human

    with database.sessions() as session:
        handoffs = list(session.scalars(select(ManualChatGPTHandoffRecord)))
        assert len(handoffs) == 1
        handoff = handoffs[0]
        assert handoff.source_run_id == bound.run_id
        assert handoff.task_json["kind"] == "shop_scope_review"
        assert handoff.task_json["requires_scope_judgment"] is True
        assert f"artifact:{screenshot_id}" in handoff.context_refs_json
        scope_artifact_id = session.scalar(
            select(JobArtifactRecord.id)
            .where(
                JobArtifactRecord.job_id == child.id,
                JobArtifactRecord.kind == "shop_scope_gate_result",
            )
            .order_by(JobArtifactRecord.id.desc())
            .limit(1)
        )
        assert scope_artifact_id is not None
        assert f"artifact:{scope_artifact_id}" in handoff.context_refs_json

    package = runtime_dir / "chatgpt-handoffs" / "outbox" / f"{handoff.id}.json"
    assert package.is_file()
    assert reconciler.reconcile_once() == []


def test_accepted_chatgpt_scope_result_is_persisted_and_closes_parent_without_phone_replay(
    tmp_path: Path,
) -> None:
    database, jobs, parent, bound, child, screenshot_id, _scope_artifact, runtime_dir = (
        _seed_physical_result_wait(tmp_path, with_scope=True)
    )
    physical = AgentPhysicalFollowupReconciler(database, runtime_dir=runtime_dir)
    [created] = physical.reconcile_once()
    assert created.status == "chatgpt_handoff_created"
    assert created.handoff_id is not None

    with database.sessions() as session:
        handoff = session.get(ManualChatGPTHandoffRecord, created.handoff_id)
        assert handoff is not None
        scope_artifact_id = session.scalar(
            select(JobArtifactRecord.id)
            .where(
                JobArtifactRecord.job_id == child.id,
                JobArtifactRecord.kind == "shop_scope_gate_result",
            )
            .order_by(JobArtifactRecord.id.desc())
            .limit(1)
        )
        assert scope_artifact_id is not None
        input_hash = handoff.input_hash
        schema_version = handoff.schema_version

    return_path = runtime_dir / "external-results" / f"{created.handoff_id}.json"
    return_path.parent.mkdir(parents=True, exist_ok=True)
    return_path.write_text(
        json.dumps(
            {
                "handoff_id": created.handoff_id,
                "schema_version": schema_version,
                "input_hash": input_hash,
                "result": {
                    "classification": "out_of_scope_physical",
                    "reason": "商品截图和店铺证据显示为需要物流交付的实体商品。",
                    "evidence_refs": [
                        f"artifact:{screenshot_id}",
                        f"artifact:{scope_artifact_id}",
                    ],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    accepted = physical.handoffs.accept_return_file(created.handoff_id)
    assert accepted.source_run_id == bound.run_id

    shop_service = ShopCollectionService(
        job_service=jobs,
        device_adapter=FakeDeviceAdapter(),
        scope_model_adapter=None,
        submitter=lambda *_args, **_kwargs: None,
    )
    result_reconciler = AgentChatGPTScopeResultReconciler(
        database,
        shop_service=shop_service,
    )
    [applied] = result_reconciler.reconcile_once()

    assert applied.status == "applied"
    assert applied.classification == "out_of_scope_physical"
    assert applied.physical_job_id == child.id
    assert applied.scope_decision_job_id is not None
    assert applied.continuation_run_id is not None
    assert jobs.get(parent.id).state is JobState.succeeded
    assert jobs.get(child.id).state is JobState.needs_human
    assert [job.id for job in jobs.list() if job.type == "android_shop_collection"] == [child.id]
    decision = shop_service.get_account_scope_decision("account-a")
    assert decision is not None
    assert decision.classification == "out_of_scope_physical"
    assert decision.decision_source == "chatgpt"
    assert result_reconciler.reconcile_once() == []
    shop_service.close()


def test_physical_selector_failure_does_not_become_reasoning_handoff(tmp_path: Path) -> None:
    database, jobs, parent, _bound, child, _screenshot, _scope, runtime_dir = (
        _seed_physical_result_wait(tmp_path, with_scope=False)
    )

    result = AgentPhysicalFollowupReconciler(
        database, runtime_dir=runtime_dir
    ).reconcile_once()

    assert len(result) == 1
    assert result[0].status == "physical_result_not_reasonable"
    assert jobs.get(parent.id).error_category == PHYSICAL_JOB_PENDING
    assert jobs.get(child.id).error_category == "selector_changed"
    with database.sessions() as session:
        assert list(session.scalars(select(ManualChatGPTHandoffRecord))) == []


def test_deterministic_physical_scope_does_not_create_chatgpt_handoff(tmp_path: Path) -> None:
    database, jobs, parent, _bound, child, _screenshot, _scope, runtime_dir = (
        _seed_physical_result_wait(
            tmp_path,
            with_scope=True,
            scope_classification="out_of_scope_physical",
        )
    )

    reconciler = AgentPhysicalFollowupReconciler(database, runtime_dir=runtime_dir)
    result = reconciler.reconcile_once()

    assert len(result) == 1
    assert result[0].status == "deterministic_scope_applied"
    current_parent = jobs.get(parent.id)
    assert current_parent.state is JobState.succeeded
    assert current_parent.current_stage == "physical_result_ready"
    assert current_parent.error_category is None
    assert jobs.get(child.id).state is JobState.succeeded
    assert [job.id for job in jobs.list() if job.type == "android_shop_collection"] == [child.id]
    with database.sessions() as session:
        assert list(session.scalars(select(ManualChatGPTHandoffRecord))) == []

    # The deterministic no-model continuation terminalizes the parent without
    # asking ChatGPT or replaying the already-completed physical child Job.
    assert reconciler.reconcile_once() == []
