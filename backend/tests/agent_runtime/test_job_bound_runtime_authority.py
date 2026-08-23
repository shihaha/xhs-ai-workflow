from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import BaseModel

from backend.app.agent_runtime import (
    AgentRunState,
    AgentRunStore,
    ModelTurn,
    NextAction,
    PermissionDecision,
    RuleBasedPermissionPolicy,
    RunBudget,
    ToolExecutionResult,
    ToolRegistry,
    ToolSpec,
)
from backend.app.agent_runtime.job_binding import (
    AgentJobBindingRecord,
    AgentJobCoordinator,
    JobAuthorityError,
)
from backend.app.agent_runtime.job_bound_runtime import (
    ActiveRunJobAuthorityGuard,
    JobBoundAgentRuntime,
)
from backend.app.agent_runtime.permissions import PermissionResult
from backend.app.db import Database
from backend.app.models.jobs import JobRecord, JobState
from backend.app.services.jobs import JobService


class EmptyInput(BaseModel):
    pass


class FinishModel:
    def __init__(self, callback=None) -> None:
        self.calls = 0
        self.callback = callback

    def next_action(self, _context):
        self.calls += 1
        if self.callback is not None:
            self.callback()
        return ModelTurn(
            action=NextAction(action="finish", final_output={"ok": True}),
            input_tokens=1,
            output_tokens=1,
            model_name="job-authority-test",
        )


class ToolModel:
    def __init__(self) -> None:
        self.calls = 0

    def next_action(self, _context):
        self.calls += 1
        return ModelTurn(
            action=NextAction(
                action="tool",
                tool_name="probe.read",
                arguments={},
                tool_call_id="probe-call",
            ),
            input_tokens=1,
            output_tokens=1,
            model_name="job-authority-test",
        )


class CancelOnPermissionPolicy:
    def __init__(self, jobs: JobService, job_id: str) -> None:
        self.jobs = jobs
        self.job_id = job_id

    def decide(self, _request):
        self.jobs.transition(self.job_id, JobState.cancelled)
        return PermissionResult(PermissionDecision.allow, "test cancellation after model")


def _database(tmp_path: Path) -> Database:
    return Database(tmp_path / "workbench.sqlite3")


def _claim(
    database: Database,
    *,
    run_id: str = "run-1",
    budget: RunBudget | None = None,
):
    jobs = JobService(database)
    job = jobs.create(job_type="agent_orchestration", input_data={})
    coordinator = AgentJobCoordinator(database, run_id_factory=lambda: run_id)
    bound = coordinator.claim_and_create_run(
        job.id,
        goal="exercise Job authority",
        budget=budget or RunBudget(max_wall_time_seconds=30),
    )
    return jobs, job, coordinator, bound


def _runtime(
    database: Database,
    *,
    model,
    tools: ToolRegistry | None = None,
    permissions=None,
) -> JobBoundAgentRuntime:
    return JobBoundAgentRuntime(
        store=AgentRunStore(database),
        model=model,
        tools=tools or ToolRegistry(),
        permissions=permissions or RuleBasedPermissionPolicy(),
        authority_guard=ActiveRunJobAuthorityGuard(database),
    )


def test_cancelled_job_blocks_model_before_provider_call(tmp_path: Path) -> None:
    database = _database(tmp_path)
    jobs, job, _coordinator, bound = _claim(database)
    jobs.transition(job.id, JobState.cancelled)
    model = FinishModel()

    outcome = _runtime(database, model=model).run_bound(bound.run_id)

    assert outcome.state is AgentRunState.failed
    assert outcome.error_category == "job_authority_lost"
    assert "before_model" in (outcome.error_detail or "")
    assert model.calls == 0
    assert jobs.get(job.id).state is JobState.cancelled


def test_cancel_during_model_blocks_stale_finish_result(tmp_path: Path) -> None:
    database = _database(tmp_path)
    jobs, job, _coordinator, bound = _claim(database)
    model = FinishModel(callback=lambda: jobs.transition(job.id, JobState.cancelled))

    outcome = _runtime(database, model=model).run_bound(bound.run_id)

    assert outcome.state is AgentRunState.failed
    assert outcome.error_category == "job_authority_lost"
    assert "after_model" in (outcome.error_detail or "")
    assert outcome.final_output is None
    assert model.calls == 1
    assert jobs.get(job.id).state is JobState.cancelled


def test_cancel_after_permission_blocks_tool_handler(tmp_path: Path) -> None:
    database = _database(tmp_path)
    jobs, job, _coordinator, bound = _claim(database)
    calls = {"handler": 0}

    def handler(_value: EmptyInput) -> ToolExecutionResult:
        calls["handler"] += 1
        return ToolExecutionResult(output={"unexpected": True})

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="probe.read",
            description="Authority boundary probe.",
            input_model=EmptyInput,
            handler=handler,
            read_only=True,
        )
    )
    model = ToolModel()
    runtime = _runtime(
        database,
        model=model,
        tools=registry,
        permissions=CancelOnPermissionPolicy(jobs, job.id),
    )

    outcome = runtime.run_bound(bound.run_id)

    assert outcome.state is AgentRunState.failed
    assert outcome.error_category == "job_authority_lost"
    assert "before_tool" in (outcome.error_detail or "")
    assert model.calls == 1
    assert calls["handler"] == 0
    assert jobs.get(job.id).state is JobState.cancelled
    assert not [step for step in runtime.store.list_steps(bound.run_id) if step.kind == "tool"]


def test_expired_lease_blocks_model_before_provider_call(tmp_path: Path) -> None:
    database = _database(tmp_path)
    jobs, job, _coordinator, bound = _claim(database)
    with database.sessions.begin() as session:
        record = session.get(JobRecord, job.id)
        assert record is not None
        record.lease_expires_at = (
            datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
        )

    model = FinishModel()
    outcome = _runtime(database, model=model).run_bound(bound.run_id)

    assert outcome.state is AgentRunState.failed
    assert outcome.error_category == "job_authority_lost"
    assert model.calls == 0
    assert jobs.get(job.id).state is JobState.running


def test_new_continuation_supersedes_old_run_authority(tmp_path: Path) -> None:
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
    jobs.transition(job.id, JobState.needs_human)
    second = coordinator.claim_and_create_run(
        job.id,
        goal="continuation claim",
        budget=RunBudget(max_wall_time_seconds=10),
    )
    guard = ActiveRunJobAuthorityGuard(database)

    with pytest.raises(JobAuthorityError, match="superseded"):
        guard.require_run_running(first.run_id)

    assert guard.require_run_running(second.run_id) == job.id


def test_ambiguous_latest_binding_timestamp_fails_closed_for_every_run(tmp_path: Path) -> None:
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
    jobs.transition(job.id, JobState.needs_human)
    second = coordinator.claim_and_create_run(
        job.id,
        goal="second claim",
        budget=RunBudget(max_wall_time_seconds=10),
    )

    tied_at = datetime.now(timezone.utc).replace(tzinfo=None)
    with database.sessions.begin() as session:
        first_binding = session.get(AgentJobBindingRecord, first.run_id)
        second_binding = session.get(AgentJobBindingRecord, second.run_id)
        assert first_binding is not None and second_binding is not None
        first_binding.created_at = tied_at
        second_binding.created_at = tied_at

    guard = ActiveRunJobAuthorityGuard(database)
    with pytest.raises(JobAuthorityError, match="ambiguous"):
        guard.require_run_running(first.run_id)
    with pytest.raises(JobAuthorityError, match="ambiguous"):
        guard.require_run_running(second.run_id)


def test_guard_rejects_non_running_agent_run_even_when_job_lease_is_valid(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _jobs, _job, _coordinator, bound = _claim(database)
    AgentRunStore(database).set_state(bound.run_id, AgentRunState.cancelled)

    with pytest.raises(JobAuthorityError, match="not running"):
        ActiveRunJobAuthorityGuard(database).require_run_running(bound.run_id)


def test_job_bound_runtime_forbids_unbound_start_and_old_resume(tmp_path: Path) -> None:
    database = _database(tmp_path)
    runtime = _runtime(database, model=FinishModel())

    with pytest.raises(RuntimeError, match="AgentJobCoordinator"):
        runtime.start("must not create an unbound AgentRun")
    with pytest.raises(RuntimeError, match="continuation"):
        runtime.resume("old-run", approved=True)
