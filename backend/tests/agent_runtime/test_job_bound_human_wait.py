from pathlib import Path

import pytest
from pydantic import BaseModel

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
from backend.app.agent_runtime.job_binding import AgentJobCoordinator, JobAuthorityError
from backend.app.agent_runtime.job_bound_runtime import (
    ActiveRunJobAuthorityGuard,
    JobBoundAgentRuntime,
)
from backend.app.agent_runtime.job_wait_projection import (
    JobHumanWaitProjector,
    JobWaitProjectionError,
)
from backend.app.db import Database
from backend.app.models.jobs import JobRecord, JobState
from backend.app.services.jobs import JobService


class EmptyInput(BaseModel):
    pass


class ApprovalToolModel:
    def __init__(self) -> None:
        self.calls = 0

    def next_action(self, _context):
        self.calls += 1
        return ModelTurn(
            action=NextAction(
                action="tool",
                tool_name="approval.probe",
                tool_call_id="approval-call",
                arguments={},
            ),
            input_tokens=1,
            output_tokens=1,
            model_name="human-wait-test",
        )


class FinishModel:
    def next_action(self, _context):
        return ModelTurn(
            action=NextAction(action="finish", final_output={"ok": True}),
            input_tokens=1,
            output_tokens=1,
            model_name="human-wait-test",
        )


def _database(tmp_path: Path) -> Database:
    return Database(tmp_path / "workbench.sqlite3")


def _claim(database: Database, run_id: str = "run-1"):
    jobs = JobService(database)
    job = jobs.create(job_type="agent_orchestration", input_data={})
    coordinator = AgentJobCoordinator(database, run_id_factory=lambda: run_id)
    bound = coordinator.claim_and_create_run(
        job.id,
        goal="exercise human wait projection",
        budget=RunBudget(max_wall_time_seconds=30),
    )
    return jobs, job, coordinator, bound


def _approval_runtime(database: Database, run_id: str):
    calls = {"handler": 0}

    def handler(_value: EmptyInput) -> ToolExecutionResult:
        calls["handler"] += 1
        return ToolExecutionResult(output={"unexpected": True})

    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            name="approval.probe",
            description="Must wait for operator approval.",
            input_model=EmptyInput,
            handler=handler,
            read_only=True,
            requires_approval=True,
        )
    )
    projector = JobHumanWaitProjector(database)
    runtime = JobBoundAgentRuntime(
        store=AgentRunStore(database),
        model=ApprovalToolModel(),
        tools=tools,
        permissions=RuleBasedPermissionPolicy(),
        authority_guard=ActiveRunJobAuthorityGuard(database),
        wait_projector=projector,
    )
    return runtime, projector, calls


def test_runtime_projects_persisted_approval_wait_to_job_and_clears_lease(tmp_path: Path) -> None:
    database = _database(tmp_path)
    jobs, job, _coordinator, bound = _claim(database)
    runtime, _projector, calls = _approval_runtime(database, bound.run_id)

    outcome = runtime.run_bound(bound.run_id)

    waiting_run = AgentRunStore(database).get_run(bound.run_id)
    waiting_job = jobs.get(job.id)
    human_action = AgentRunStore(database).pending_human_action(bound.run_id)

    assert outcome.state is AgentRunState.needs_human
    assert waiting_run.state == AgentRunState.needs_human.value
    assert human_action is not None
    assert human_action.status == "pending"
    assert waiting_job.state is JobState.needs_human
    assert waiting_job.lease_expires_at is None
    assert waiting_job.error_category == waiting_run.error_category
    assert calls["handler"] == 0


def test_restart_reconciles_agent_wait_when_crash_prevented_job_projection(tmp_path: Path) -> None:
    database = _database(tmp_path)
    jobs, job, _coordinator, bound = _claim(database)
    store = AgentRunStore(database)

    # Simulate the durable half of the crash window: Agent Runtime persisted its
    # approval/checkpoint/wait, but the process died before Job projection.
    store.create_human_action(
        run_id=bound.run_id,
        tool_call_id="approval-call",
        tool_name="approval.probe",
        request_json={"arguments": {}},
    )
    store.checkpoint(bound.run_id, state_json={"reason": "approval_required"})
    store.set_state(
        bound.run_id,
        AgentRunState.needs_human,
        error_category="approval_required",
        error_detail="operator approval required",
    )
    assert jobs.get(job.id).state is JobState.running
    assert jobs.get(job.id).lease_expires_at is not None

    reopened = Database(database.database_path)
    result = JobHumanWaitProjector(reopened).reconcile_wait_after_restart(bound.run_id)

    waiting_job = JobService(reopened).get(job.id)
    assert result.projected is True
    assert result.terminal_job_won is False
    assert waiting_job.state is JobState.needs_human
    assert waiting_job.lease_expires_at is None


def test_wait_projection_is_idempotent_after_restart(tmp_path: Path) -> None:
    database = _database(tmp_path)
    jobs, job, _coordinator, bound = _claim(database)
    store = AgentRunStore(database)
    store.set_state(
        bound.run_id,
        AgentRunState.needs_human,
        error_category="operator_review",
    )
    projector = JobHumanWaitProjector(database)

    first = projector.project_wait(bound.run_id)
    second = projector.reconcile_wait_after_restart(bound.run_id)

    assert first.projected is True
    assert second.projected is False
    assert second.job_state is JobState.needs_human
    assert jobs.get(job.id).lease_expires_at is None


def test_domain_needs_human_without_approval_action_still_tightens_job_authority(tmp_path: Path) -> None:
    database = _database(tmp_path)
    jobs, job, _coordinator, bound = _claim(database)
    store = AgentRunStore(database)
    store.checkpoint(bound.run_id, state_json={"reason": "selector_changed"})
    store.set_state(
        bound.run_id,
        AgentRunState.needs_human,
        error_category="selector_changed",
        error_detail="domain worker needs operator intervention",
    )

    assert store.pending_human_action(bound.run_id) is None
    result = JobHumanWaitProjector(database).project_wait(bound.run_id)

    assert result.projected is True
    assert jobs.get(job.id).state is JobState.needs_human
    assert jobs.get(job.id).lease_expires_at is None
    assert jobs.get(job.id).error_category == "selector_changed"


def test_terminal_job_wins_race_against_wait_projection(tmp_path: Path) -> None:
    database = _database(tmp_path)
    jobs, job, _coordinator, bound = _claim(database)
    store = AgentRunStore(database)
    store.set_state(
        bound.run_id,
        AgentRunState.needs_human,
        error_category="approval_required",
    )
    jobs.transition(job.id, JobState.cancelled)

    result = JobHumanWaitProjector(database).project_wait(bound.run_id)

    assert result.projected is False
    assert result.terminal_job_won is True
    assert result.job_state is JobState.cancelled
    assert jobs.get(job.id).state is JobState.cancelled


def test_old_waiting_run_cannot_project_after_new_continuation_claim(tmp_path: Path) -> None:
    database = _database(tmp_path)
    jobs = JobService(database)
    job = jobs.create(job_type="agent_orchestration", input_data={})
    coordinator = AgentJobCoordinator(
        database,
        run_id_factory=iter(("run-1", "run-2")).__next__,
    )
    first = coordinator.claim_and_create_run(
        job.id,
        goal="first claim",
        budget=RunBudget(max_wall_time_seconds=10),
    )
    store = AgentRunStore(database)
    store.set_state(first.run_id, AgentRunState.needs_human, error_category="approval_required")
    JobHumanWaitProjector(database).project_wait(first.run_id)

    second = coordinator.claim_and_create_run(
        job.id,
        goal="new continuation",
        budget=RunBudget(max_wall_time_seconds=10),
    )

    with pytest.raises(JobAuthorityError, match="superseded"):
        JobHumanWaitProjector(database).reconcile_wait_after_restart(first.run_id)

    assert jobs.get(job.id).state is JobState.running
    assert ActiveRunJobAuthorityGuard(database).require_run_running(second.run_id) == job.id


def test_projection_rejects_agent_run_that_is_not_waiting(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _jobs, _job, _coordinator, bound = _claim(database)

    with pytest.raises(JobWaitProjectionError, match="only needs_human"):
        JobHumanWaitProjector(database).project_wait(bound.run_id)


def test_missing_wait_projector_never_silently_returns_needs_human(tmp_path: Path) -> None:
    database = _database(tmp_path)
    jobs, job, _coordinator, bound = _claim(database)
    calls = {"handler": 0}

    def handler(_value: EmptyInput) -> ToolExecutionResult:
        calls["handler"] += 1
        return ToolExecutionResult(output={"unexpected": True})

    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            name="approval.probe",
            description="Must wait for operator approval.",
            input_model=EmptyInput,
            handler=handler,
            read_only=True,
            requires_approval=True,
        )
    )
    runtime = JobBoundAgentRuntime(
        store=AgentRunStore(database),
        model=ApprovalToolModel(),
        tools=tools,
        permissions=RuleBasedPermissionPolicy(),
        authority_guard=ActiveRunJobAuthorityGuard(database),
    )

    with pytest.raises(RuntimeError, match="without a Job wait projector"):
        runtime.run_bound(bound.run_id)

    # The crash/recovery invariant remains recoverable: Agent wait is durable,
    # Job is still running, and the same projector repairs only Job authority.
    assert AgentRunStore(database).get_run(bound.run_id).state == AgentRunState.needs_human.value
    assert jobs.get(job.id).state is JobState.running
    assert calls["handler"] == 0
    repaired = JobHumanWaitProjector(database).reconcile_wait_after_restart(bound.run_id)
    assert repaired.projected is True
    assert jobs.get(job.id).state is JobState.needs_human


def test_corrupt_needs_human_job_with_live_lease_fails_closed(tmp_path: Path) -> None:
    database = _database(tmp_path)
    jobs, job, _coordinator, bound = _claim(database)
    store = AgentRunStore(database)
    store.set_state(bound.run_id, AgentRunState.needs_human, error_category="approval_required")
    jobs.transition(job.id, JobState.needs_human)

    # Corrupt only the invariant under test. The projector must not use raw SQL
    # to 'repair' an impossible needs_human+live-lease combination silently.
    original_lease = bound.lease_expires_at
    with database.sessions.begin() as session:
        record = session.get(JobRecord, job.id)
        assert record is not None
        record.lease_expires_at = original_lease

    with pytest.raises(JobWaitProjectionError, match="still owns a lease"):
        JobHumanWaitProjector(database).project_wait(bound.run_id)
