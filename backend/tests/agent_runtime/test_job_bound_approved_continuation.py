from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

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
from backend.app.agent_runtime.job_binding import AgentJobCoordinator
from backend.app.agent_runtime.job_bound_runtime import (
    ActiveRunJobAuthorityGuard,
    JobBoundAgentRuntime,
)
from backend.app.agent_runtime.job_continuation import (
    AgentContinuationRecord,
    ContinuationApprovalError,
    JobContinuationCoordinator,
    JobHistoryContextBuilder,
)
from backend.app.agent_runtime.job_wait_projection import JobHumanWaitProjector
from backend.app.agent_runtime.persistence import HumanActionRecord
from backend.app.db import Database
from backend.app.models.jobs import JobState
from backend.app.services.jobs import JobService


class ValueInput(BaseModel):
    value: int


class FinishAfterApprovedModel:
    def __init__(self, *, before_finish=None) -> None:
        self.calls = 0
        self.contexts = []
        self.before_finish = before_finish

    def next_action(self, context):
        self.calls += 1
        self.contexts.append(context)
        if self.before_finish is not None:
            self.before_finish(context)
        return ModelTurn(
            action=NextAction(action="finish", final_output={"continued": True}),
            input_tokens=1,
            output_tokens=1,
            model_name="continuation-test",
        )


def _setup_wait(
    tmp_path: Path,
    *,
    budget: RunBudget | None = None,
    source_run_id: str = "source-run",
):
    database = Database(tmp_path / "workbench.sqlite3")
    jobs = JobService(database)
    job = jobs.create(job_type="agent_orchestration", input_data={})
    store = AgentRunStore(database)
    coordinator = AgentJobCoordinator(database, run_id_factory=lambda: source_run_id)
    bound = coordinator.claim_and_create_run(
        job.id,
        goal="continue the approved work",
        budget=budget
        or RunBudget(
            max_steps=10,
            max_model_calls=6,
            max_input_tokens=1000,
            max_output_tokens=500,
            max_wall_time_seconds=30,
        ),
    )
    action = NextAction(
        action="tool",
        tool_name="approved.write",
        arguments={"value": 7},
        tool_call_id="approved-call",
    )
    human = store.create_human_action(
        run_id=bound.run_id,
        tool_call_id=action.tool_call_id,
        tool_name=action.tool_name or "",
        request_json={"action": action.model_dump(mode="json"), "reason": "test approval"},
    )
    store.set_state(
        bound.run_id,
        AgentRunState.needs_human,
        error_category="approval_required",
        error_detail="test approval",
    )
    jobs.transition(job.id, JobState.needs_human, error_category="approval_required")
    return database, jobs, job, store, bound, action, human


def _human(database: Database, action_id: str) -> HumanActionRecord:
    with database.sessions() as session:
        record = session.get(HumanActionRecord, action_id)
        assert record is not None
        session.expunge(record)
        return record


def _registry(calls: dict[str, object]) -> ToolRegistry:
    registry = ToolRegistry()

    def handler(value: ValueInput) -> ToolExecutionResult:
        calls["count"] = int(calls.get("count", 0)) + 1
        calls["value"] = value.value
        return ToolExecutionResult(
            output={"written": value.value},
            evidence_refs=["evidence:approved:1"],
        )

    registry.register(
        ToolSpec(
            name="approved.write",
            description="Side-effect probe for an exact approved continuation.",
            input_model=ValueInput,
            handler=handler,
            read_only=False,
            destructive=False,
            external_side_effect=True,
            requires_approval=True,
            timeout_seconds=5.0,
            idempotent=False,
            retry_limit=0,
        )
    )
    return registry


def _runtime(
    database: Database,
    *,
    model,
    registry: ToolRegistry,
    continuations: JobContinuationCoordinator,
) -> JobBoundAgentRuntime:
    return JobBoundAgentRuntime(
        store=AgentRunStore(database),
        model=model,
        tools=registry,
        permissions=RuleBasedPermissionPolicy(),
        authority_guard=ActiveRunJobAuthorityGuard(database),
        wait_projector=JobHumanWaitProjector(database),
        continuation_resolver=continuations,
        context_builder=JobHistoryContextBuilder(database),
    )


def test_approval_reclaims_job_and_creates_new_run_with_remaining_budget(tmp_path: Path) -> None:
    database, jobs, job, store, bound, action, human = _setup_wait(tmp_path)
    with database.sessions.begin() as session:
        source = session.get(type(store.get_run(bound.run_id)), bound.run_id)
        assert source is not None
        source.step_count = 2
        source.model_calls = 1
        source.input_tokens = 120
        source.output_tokens = 30

    continuations = JobContinuationCoordinator(
        database,
        run_id_factory=lambda: "continuation-run",
    )
    approved = continuations.approve_and_create_continuation(
        bound.run_id,
        human.id,
        note="approved exactly",
    )

    assert approved.job_id == job.id
    assert approved.source_run_id == bound.run_id
    assert approved.run_id == "continuation-run"
    assert approved.action == action
    assert approved.remaining_budget.max_steps == 8
    assert approved.remaining_budget.max_model_calls == 5
    assert approved.remaining_budget.max_input_tokens == 880
    assert approved.remaining_budget.max_output_tokens == 470
    assert approved.remaining_budget.max_wall_time_seconds == 30

    current_job = jobs.get(job.id)
    assert current_job.state is JobState.running
    assert current_job.retry_count == 1
    assert current_job.lease_expires_at is not None
    assert store.get_run(bound.run_id).state == AgentRunState.needs_human.value
    child = store.get_run(approved.run_id)
    assert child.state == AgentRunState.running.value
    assert RunBudget.model_validate(child.budget_json) == approved.remaining_budget

    resolved = _human(database, human.id)
    assert resolved.status == "approved"
    assert resolved.resolution_json == {"approved": True, "note": "approved exactly"}
    assert store.pending_human_action(bound.run_id) is None
    assert continuations.approved_action_for_run(approved.run_id) == action
    with database.sessions() as session:
        link = session.get(AgentContinuationRecord, approved.run_id)
        assert link is not None
        assert link.source_run_id == bound.run_id
        assert link.human_action_id == human.id
        assert link.tool_call_id == action.tool_call_id


def test_budget_exhaustion_does_not_resolve_action_or_reclaim_job(tmp_path: Path) -> None:
    database, jobs, job, store, bound, _action, human = _setup_wait(
        tmp_path,
        budget=RunBudget(
            max_steps=1,
            max_model_calls=2,
            max_input_tokens=100,
            max_output_tokens=100,
            max_wall_time_seconds=30,
        ),
    )
    # Preserve the durable waiting state while making the root step allowance
    # fully consumed. No continuation should be created merely to fail at step 0.
    with database.sessions.begin() as session:
        source = session.get(type(store.get_run(bound.run_id)), bound.run_id)
        assert source is not None
        source.step_count = 1

    continuations = JobContinuationCoordinator(database, run_id_factory=lambda: "never-created")
    with pytest.raises(ContinuationApprovalError, match="budget exhausted"):
        continuations.approve_and_create_continuation(bound.run_id, human.id)

    assert jobs.get(job.id).state is JobState.needs_human
    assert jobs.get(job.id).retry_count == 0
    assert _human(database, human.id).status == "pending"
    with database.sessions() as session:
        assert session.get(AgentContinuationRecord, "never-created") is None


def test_insert_conflict_rolls_back_approval_and_job_reclaim(tmp_path: Path) -> None:
    database, jobs, job, store, bound, _action, human = _setup_wait(tmp_path)
    conflict = store.create_run(goal="unbound conflict", budget=RunBudget())
    continuations = JobContinuationCoordinator(
        database,
        run_id_factory=lambda: conflict.id,
    )

    with pytest.raises(IntegrityError):
        continuations.approve_and_create_continuation(bound.run_id, human.id)

    current = jobs.get(job.id)
    assert current.state is JobState.needs_human
    assert current.retry_count == 0
    assert current.lease_expires_at is None
    assert _human(database, human.id).status == "pending"


def test_non_pending_or_domain_wait_cannot_use_binary_approval_path(tmp_path: Path) -> None:
    database, jobs, job, store, bound, _action, human = _setup_wait(tmp_path)
    store.set_state(
        bound.run_id,
        AgentRunState.needs_human,
        error_category="selector_changed",
        error_detail="domain wait",
    )
    continuations = JobContinuationCoordinator(database)

    with pytest.raises(ContinuationApprovalError, match="approval_required"):
        continuations.approve_and_create_continuation(bound.run_id, human.id)

    assert jobs.get(job.id).state is JobState.needs_human
    assert _human(database, human.id).status == "pending"


def test_history_context_reads_old_steps_without_copying_them_to_child(tmp_path: Path) -> None:
    database, _jobs, _job, store, bound, _action, human = _setup_wait(tmp_path)
    source_step = store.append_step(
        bound.run_id,
        kind="tool",
        status="succeeded",
        tool_name="history.read",
        output_json={"fact": "durable old result"},
        evidence_refs=["evidence:history:1"],
    )
    continuations = JobContinuationCoordinator(
        database,
        run_id_factory=lambda: "continuation-run",
    )
    approved = continuations.approve_and_create_continuation(bound.run_id, human.id)
    assert store.list_steps(approved.run_id) == []

    builder = JobHistoryContextBuilder(database, max_recent_steps=20)
    context = builder.build(
        goal="continue",
        run_id=approved.run_id,
        steps=[],
        tools=[],
        usage={"model_calls": 0, "input_tokens": 0, "output_tokens": 0},
        remaining_budget={
            "steps": approved.remaining_budget.max_steps,
            "model_calls": approved.remaining_budget.max_model_calls,
            "input_tokens": approved.remaining_budget.max_input_tokens,
            "output_tokens": approved.remaining_budget.max_output_tokens,
            "wall_time_seconds": approved.remaining_budget.max_wall_time_seconds,
        },
    )

    historical = [step for step in context.recent_steps if step.tool_name == "history.read"]
    assert len(historical) == 1
    assert historical[0].step_index == 1
    assert historical[0].evidence_refs == ["evidence:history:1"]
    assert "durable old result" in (historical[0].output_summary or "")
    assert source_step.step_index == 1
    assert store.list_steps(approved.run_id) == []


def test_runtime_executes_exact_approved_action_before_model(tmp_path: Path) -> None:
    database, jobs, job, _store, bound, _action, human = _setup_wait(tmp_path)
    continuations = JobContinuationCoordinator(
        database,
        run_id_factory=lambda: "continuation-run",
    )
    approved = continuations.approve_and_create_continuation(bound.run_id, human.id)
    calls: dict[str, object] = {"count": 0}
    registry = _registry(calls)

    def before_finish(context) -> None:
        assert calls["count"] == 1
        assert calls["value"] == 7
        assert any(
            step.tool_name == "approved.write" and step.status == "succeeded"
            for step in context.recent_steps
        )

    model = FinishAfterApprovedModel(before_finish=before_finish)
    runtime = _runtime(
        database,
        model=model,
        registry=registry,
        continuations=continuations,
    )
    outcome = runtime.run_approved_continuation(approved.run_id)

    assert outcome.state is AgentRunState.succeeded
    assert outcome.final_output == {"continued": True}
    assert calls == {"count": 1, "value": 7}
    assert model.calls == 1
    # Terminal Job projection is deliberately a later slice.
    assert jobs.get(job.id).state is JobState.running


def test_restart_after_committed_non_idempotent_tool_never_replays_handler(tmp_path: Path) -> None:
    database, _jobs, _job, store, bound, action, human = _setup_wait(tmp_path)
    continuations = JobContinuationCoordinator(
        database,
        run_id_factory=lambda: "continuation-run",
    )
    approved = continuations.approve_and_create_continuation(bound.run_id, human.id)
    store.append_step(
        approved.run_id,
        kind="tool",
        status="succeeded",
        tool_name=action.tool_name,
        tool_call_id=action.tool_call_id,
        input_json={
            "arguments": action.arguments,
            "idempotent": False,
            "timeout_seconds": 5.0,
        },
        output_json={"output": {"written": 7}, "evidence_refs": ["evidence:approved:1"]},
        evidence_refs=["evidence:approved:1"],
    )
    calls: dict[str, object] = {"count": 0}
    model = FinishAfterApprovedModel()
    runtime = _runtime(
        database,
        model=model,
        registry=_registry(calls),
        continuations=continuations,
    )

    outcome = runtime.run_approved_continuation(approved.run_id)

    assert outcome.state is AgentRunState.succeeded
    assert calls["count"] == 0
    assert model.calls == 1


def test_started_side_effect_is_never_replayed_and_projects_new_wait(tmp_path: Path) -> None:
    database, jobs, job, store, bound, action, human = _setup_wait(tmp_path)
    continuations = JobContinuationCoordinator(
        database,
        run_id_factory=lambda: "continuation-run",
    )
    approved = continuations.approve_and_create_continuation(bound.run_id, human.id)
    store.append_step(
        approved.run_id,
        kind="tool",
        status="started",
        tool_name=action.tool_name,
        tool_call_id=action.tool_call_id,
        input_json={
            "arguments": action.arguments,
            "idempotent": False,
            "timeout_seconds": 5.0,
        },
    )
    calls: dict[str, object] = {"count": 0}
    model = FinishAfterApprovedModel()
    runtime = _runtime(
        database,
        model=model,
        registry=_registry(calls),
        continuations=continuations,
    )

    outcome = runtime.run_approved_continuation(approved.run_id)

    assert outcome.state is AgentRunState.needs_human
    assert outcome.error_category == "uncertain_tool_side_effect"
    assert calls["count"] == 0
    assert model.calls == 0
    current = jobs.get(job.id)
    assert current.state is JobState.needs_human
    assert current.lease_expires_at is None


def test_job_cancellation_before_approved_tool_blocks_handler(tmp_path: Path) -> None:
    database, jobs, job, _store, bound, _action, human = _setup_wait(tmp_path)
    continuations = JobContinuationCoordinator(
        database,
        run_id_factory=lambda: "continuation-run",
    )
    approved = continuations.approve_and_create_continuation(bound.run_id, human.id)
    jobs.transition(job.id, JobState.cancelled)
    calls: dict[str, object] = {"count": 0}
    model = FinishAfterApprovedModel()
    runtime = _runtime(
        database,
        model=model,
        registry=_registry(calls),
        continuations=continuations,
    )

    outcome = runtime.run_approved_continuation(approved.run_id)

    assert outcome.state is AgentRunState.failed
    assert outcome.error_category == "job_authority_lost"
    assert calls["count"] == 0
    assert model.calls == 0
    assert jobs.get(job.id).state is JobState.cancelled
