from pathlib import Path

from backend.app.adapters.contracts import ModelResult
from backend.app.agent_runtime import (
    AgentRunState,
    AgentRunStore,
    AgentRuntime,
    ModelTurn,
    NextAction,
    PermissionDecision,
    RuleBasedPermissionPolicy,
    ToolRegistry,
)
from backend.app.agent_runtime.domain_tools import (
    ANALYSIS_RUN_TOOL_NAME,
    build_analysis_run_tool,
)
from backend.app.db import Database
from backend.app.features.analysis.schemas import AnalysisCreate
from backend.app.features.analysis.service import AnalysisService
from backend.app.features.radar.models import RankItemRecord, RankSnapshotRecord


class StubDomainModel:
    provider = "stub-provider"
    model = "stub-grounded-analysis"

    def __init__(self, output: dict[str, object]) -> None:
        self.output = output
        self.calls = 0

    @property
    def configured(self) -> bool:
        return True

    def generate_structured(self, _request, _schema) -> ModelResult:
        self.calls += 1
        return ModelResult(
            model=self.model,
            output=self.output,
            raw_evidence={"attempts": []},
            usage={"total_tokens": 10},
            duration_ms=2,
        )


class SequenceRuntimeModel:
    def __init__(self, *turns: ModelTurn) -> None:
        self.turns = list(turns)
        self.calls = 0

    def next_action(self, _context):
        self.calls += 1
        if not self.turns:
            raise AssertionError("runtime model called more times than expected")
        return self.turns.pop(0)


def _turn(action: NextAction) -> ModelTurn:
    return ModelTurn(
        action=action,
        input_tokens=5,
        output_tokens=2,
        model_name="runtime-sequence-model",
    )


def _database(root: Path) -> Database:
    root.mkdir(parents=True, exist_ok=True)
    return Database(root / "workbench.sqlite3")


def _seed_rank_item(database: Database, *, user_id: str, stable_suffix: str) -> str:
    with database.session() as session:
        snapshot = RankSnapshotRecord(
            source_date="2026-08-18",
            collected_at="2026-08-18T10:00:00",
            board="阅读榜",
            dimension="优秀账号",
            source_url="https://example.test/qianfan",
            raw_evidence={"fixture": "agent-runtime-stage2"},
            submitted_count=1,
        )
        session.add(snapshot)
        session.flush()
        item = RankItemRecord(
            snapshot_id=snapshot.id,
            stable_key=f"{user_id}:{stable_suffix}",
            rank_no=1,
            title="受控七宗罪测试",
            author_name=f"受控账号-{user_id}",
            publish_date="2026-08-18",
            read_range="10万以上",
            click_rate_range="10%-20%",
            pay_rate_range="70%-90%",
            gmv_range="1万-5万",
            note_id=f"note-{stable_suffix}",
            user_id=user_id,
            source_url=f"https://www.xiaohongshu.com/explore/{stable_suffix}",
            raw_evidence={"fixture": "agent-runtime-stage2", "user_id": user_id},
        )
        session.add(item)
        session.commit()
        return f"rank-item:{item.id}"


def _successful_output(evidence_id: str) -> dict[str, object]:
    return {
        "claims": [
            {"claim": "受控榜单证据证明该账号存在相关内容。", "evidence_ids": [evidence_id]}
        ],
        "product_clusters": [
            {
                "name": "七宗罪测试",
                "summary": "仅用于 Stage 2 可重复分析对照。",
                "evidence_ids": [evidence_id],
            }
        ],
        "account_demand_profiles": [],
        "cross_account_conclusion": None,
        "opportunities": [],
    }


def _signature(result) -> dict[str, object]:
    return {
        "analysis_type": result.analysis_type,
        "account_user_id": result.account_user_id,
        "account_user_ids": list(result.account_user_ids),
        "status": result.status,
        "prompt_version": result.prompt_version,
        "provider": result.provider,
        "model": result.model,
        "input_digest": result.input_digest,
        "evidence_ids": list(result.evidence_ids),
        "output": result.output.model_dump(mode="json") if result.output is not None else None,
        "usage": dict(result.usage),
        "duration_ms": result.duration_ms,
        "error_category": result.error_category,
        "error_detail": result.error_detail,
    }


def _runtime_for(database: Database, service: AnalysisService, *turns: ModelTurn):
    registry = ToolRegistry()
    registry.register(build_analysis_run_tool(service))
    model = SequenceRuntimeModel(*turns)
    store = AgentRunStore(database)
    runtime = AgentRuntime(
        store=store,
        model=model,
        tools=registry,
        permissions=RuleBasedPermissionPolicy(
            overrides={ANALYSIS_RUN_TOOL_NAME: PermissionDecision.allow}
        ),
    )
    return runtime, store, model


def test_stage2_agent_analysis_matches_existing_service_success(tmp_path: Path) -> None:
    baseline_db = _database(tmp_path / "baseline-success")
    baseline_evidence = _seed_rank_item(
        baseline_db, user_id="account-a", stable_suffix="success"
    )
    baseline_model = StubDomainModel(_successful_output(baseline_evidence))
    baseline_service = AnalysisService(
        baseline_db, baseline_model, runtime_dir=baseline_db.database_path.parent
    )
    payload = AnalysisCreate(
        analysis_type="account_report",
        account_user_id="account-a",
        evidence_ids=[baseline_evidence],
    )
    baseline = baseline_service.create(payload)
    assert baseline.status == "succeeded"

    agent_db = _database(tmp_path / "agent-success")
    agent_evidence = _seed_rank_item(
        agent_db, user_id="account-a", stable_suffix="success"
    )
    assert agent_evidence == baseline_evidence
    agent_model = StubDomainModel(_successful_output(agent_evidence))
    agent_service = AnalysisService(
        agent_db, agent_model, runtime_dir=agent_db.database_path.parent
    )
    runtime, store, runtime_model = _runtime_for(
        agent_db,
        agent_service,
        _turn(
            NextAction(
                action="tool",
                tool_name=ANALYSIS_RUN_TOOL_NAME,
                tool_call_id="stage2-analysis-success",
                arguments=payload.model_dump(mode="json"),
            )
        ),
        _turn(NextAction(action="finish", final_output={"analysis": "succeeded"})),
    )

    outcome = runtime.start("run the existing grounded account analysis")

    assert outcome.state is AgentRunState.succeeded
    assert runtime_model.calls == 2
    agent = agent_service.list()[0]
    assert _signature(agent) == _signature(baseline)
    steps = store.list_steps(outcome.run_id)
    tool_steps = [step for step in steps if step.kind == "tool"]
    assert len(tool_steps) == 1
    assert tool_steps[0].status == "succeeded"
    assert tool_steps[0].evidence_refs_json == [agent_evidence]
    assert tool_steps[0].output_json["output"]["status"] == "succeeded"
    assert any(step.kind == "permission" and step.status == "allow" for step in steps)
    assert any(step.kind == "output" and step.status == "succeeded" for step in steps)


def test_stage2_preserves_deep_verification_needs_human(tmp_path: Path) -> None:
    baseline_db = _database(tmp_path / "baseline-needs-human")
    # The analysis scope contains two accounts, but deliberately only one
    # non-shop evidence row.  That is sufficient to prove the existing
    # product_cluster deep-verification gate without inventing a second
    # ranking snapshot that violates its natural unique scope key.
    baseline_ids = [
        _seed_rank_item(baseline_db, user_id="account-a", stable_suffix="a"),
    ]
    payload = AnalysisCreate(
        analysis_type="product_cluster",
        account_user_ids=["account-a", "account-b"],
        evidence_ids=baseline_ids,
    )
    baseline_model = StubDomainModel(_successful_output(baseline_ids[0]))
    baseline_service = AnalysisService(
        baseline_db, baseline_model, runtime_dir=baseline_db.database_path.parent
    )
    baseline = baseline_service.create(payload)
    assert baseline.status == "needs_human"
    assert baseline.error_category == "deep_verification_incomplete"
    assert baseline_model.calls == 0

    agent_db = _database(tmp_path / "agent-needs-human")
    agent_ids = [
        _seed_rank_item(agent_db, user_id="account-a", stable_suffix="a"),
    ]
    assert agent_ids == baseline_ids
    agent_model = StubDomainModel(_successful_output(agent_ids[0]))
    agent_service = AnalysisService(
        agent_db, agent_model, runtime_dir=agent_db.database_path.parent
    )
    runtime, store, runtime_model = _runtime_for(
        agent_db,
        agent_service,
        _turn(
            NextAction(
                action="tool",
                tool_name=ANALYSIS_RUN_TOOL_NAME,
                tool_call_id="stage2-analysis-needs-human",
                arguments=payload.model_dump(mode="json"),
            )
        ),
    )

    outcome = runtime.start("preserve the existing deep verification gate")

    assert outcome.state is AgentRunState.needs_human
    assert outcome.error_category == baseline.error_category
    assert agent_model.calls == 0
    assert runtime_model.calls == 1
    agent = agent_service.list()[0]
    assert _signature(agent) == _signature(baseline)
    tool_steps = [step for step in store.list_steps(outcome.run_id) if step.kind == "tool"]
    assert len(tool_steps) == 1
    assert tool_steps[0].status == "needs_human"
    assert tool_steps[0].error_category == "deep_verification_incomplete"


def test_stage2_preserves_grounding_failure(tmp_path: Path) -> None:
    baseline_db = _database(tmp_path / "baseline-failed")
    baseline_evidence = _seed_rank_item(
        baseline_db, user_id="account-a", stable_suffix="failed"
    )
    invalid_output = _successful_output("rank-item:999")
    payload = AnalysisCreate(
        analysis_type="account_report",
        account_user_id="account-a",
        evidence_ids=[baseline_evidence],
    )
    baseline_service = AnalysisService(
        baseline_db,
        StubDomainModel(invalid_output),
        runtime_dir=baseline_db.database_path.parent,
    )
    baseline = baseline_service.create(payload)
    assert baseline.status == "failed"
    assert baseline.error_category == "evidence_grounding_failed"

    agent_db = _database(tmp_path / "agent-failed")
    agent_evidence = _seed_rank_item(
        agent_db, user_id="account-a", stable_suffix="failed"
    )
    assert agent_evidence == baseline_evidence
    agent_service = AnalysisService(
        agent_db,
        StubDomainModel(invalid_output),
        runtime_dir=agent_db.database_path.parent,
    )
    runtime, store, runtime_model = _runtime_for(
        agent_db,
        agent_service,
        _turn(
            NextAction(
                action="tool",
                tool_name=ANALYSIS_RUN_TOOL_NAME,
                tool_call_id="stage2-analysis-failed",
                arguments=payload.model_dump(mode="json"),
            )
        ),
    )

    outcome = runtime.start("preserve strict evidence grounding failure")

    assert outcome.state is AgentRunState.failed
    assert outcome.error_category == baseline.error_category
    assert runtime_model.calls == 1
    agent = agent_service.list()[0]
    assert _signature(agent) == _signature(baseline)
    tool_steps = [step for step in store.list_steps(outcome.run_id) if step.kind == "tool"]
    assert len(tool_steps) == 1
    assert tool_steps[0].status == "failed"
    assert tool_steps[0].error_category == "evidence_grounding_failed"
