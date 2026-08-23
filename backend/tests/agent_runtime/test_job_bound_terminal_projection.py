from datetime import datetime, timedelta, timezone
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
from backend.app.agent_runtime.job_binding import (
    AgentJobBindingRecord,
    AgentJobCoordinator,
)
from backend.app.agent_runtime.job_bound_runtime import ActiveRunJobAuthorityGuard
from backend.app.agent_runtime.job_terminal_projection import (
    JobTerminalProjectionError,
    JobTerminalProjector,
)
from backend.app.agent_runtime.job_terminal_runtime import (
    TerminalProjectingJobBoundAgentRuntime,
)
from backend.app.agent_runtime.persistence import AgentRunRecord
from backend.app.db import Database
from backend.app.models.jobs import JobRecord, JobState
from backend.app.services.jobs import JobService


class EmptyInput(BaseModel):
    pass


class FinishModel:
    def __init__(self, *, callback=None, output=None) -> None:
        self.callback = callback
        self.output = output or {"ok": True}
        self.calls = 0

    def next_action(self, _context):
        self.calls += 1
        if self.callback is not None:
            self.callback()
        return ModelTurn(
            action=NextAction(action="finish", final_output=self.output),
            input_tokens=1,
            output_tokens=1,
            model_name="terminal-projection-test",
        )


class ExplodingModel:
    def next_action(self, _context):
        raise RuntimeError("provider exploded")


class EvidenceThenFinishModel:
    def __init__(self) -> None:
        self.calls = 0

    def next_action(self, _context):
        self.calls += 1
        if self.calls == 1:
            return ModelTurn(
                action=NextAction(
                    action="tool",
                    tool_name="evidence.read",
                    arguments={},
                    tool_call_id="evidence-call",
                ),
                input_tokens=1,
                output_tokens=1,
                model_name="terminal-projection-test",
            )
        return ModelTurn(
            action=NextAction(action="finish", final_output={"ok": True}),
            input_tokens=1,
            output_tokens=1,
            model_name="terminal-projection-test",
        )


def _database(tmp_path: Path) -> Database:
    return Database(tmp_path / "workbench.sqlite3")


def _claim(database: Database, *, job_input=None, run_id="run-1"):
    jobs = JobService(database)
    job = jobs.create(
        job_type="agent_orchestration",
        input_data=job_input or {},
    )
    coordinator = AgentJobCoordinator(database, run_id_factory=lambda: run_id)
    bound = coordinator.claim_and_create_run(
        job.id,
        goal="prove terminal projection",
        budget=RunBudget(max_wall_time_seconds=30),
    )
    return jobs, job, bound


def _runtime(database: Database, *, model, tools=None):
    return TerminalProjectingJobBoundAgentRuntime(
        store=AgentRunStore(database),
        model=model,
        tools=tools or ToolRegistry(),
        permissions=RuleBasedPermissionPolicy(),
        authority_guard=ActiveRunJobAuthorityGuard(database),
    )


def _persist_success(store: AgentRunStore, run_id: str, output=None) -> None:
    final_output = output or {"ok": True}
    store.append_step(
        run_id,
        kind="output",
        status="succeeded",
        output_json=final_output,
    )
    store.set_state(
        run_id,
        AgentRunState.succeeded,
        final_output=final_output,
    )


def _persist_failure(store: AgentRunStore, run_id: str, *, category="model_error") -> None:
    detail = "RuntimeError: provider exploded"
    store.append_step(
        run_id,
        kind="error",
        status="failed",
        error_category=category,
        error_detail=detail,
    )
    store.set_state(
        run_id,
        AgentRunState.failed,
        error_category=category,
        error_detail=detail,
    )


def test_runtime_success_projects_job_succeeded_after_durable_output(tmp_path: Path) -> None:
    database = _database(tmp_path)
    jobs, job, bound = _claim(database)

    outcome = _runtime(database, model=FinishModel()).run_bound(bound.run_id)

    assert outcome.state is AgentRunState.succeeded
    assert outcome.final_output == {"ok": True}
    finished = jobs.get(job.id)
    assert finished.state is JobState.succeeded
    assert finished.lease_expires_at is None
    assert finished.completed_at is not None
    assert any("finalized this Job as succeeded" in log.message for log in finished.logs)


def test_runtime_failure_projects_job_failed_after_matching_error_step(tmp_path: Path) -> None:
    database = _database(tmp_path)
    jobs, job, bound = _claim(database)

    outcome = _runtime(database, model=ExplodingModel()).run_bound(bound.run_id)

    assert outcome.state is AgentRunState.failed
    assert outcome.error_category == "model_error"
    finished = jobs.get(job.id)
    assert finished.state is JobState.failed
    assert finished.error_category == "model_error"
    assert finished.lease_expires_at is None


def test_job_cancellation_wins_and_stale_agent_failure_cannot_overwrite_it(tmp_path: Path) -> None:
    database = _database(tmp_path)
    jobs, job, bound = _claim(database)
    model = FinishModel(callback=lambda: jobs.transition(job.id, JobState.cancelled))

    outcome = _runtime(database, model=model).run_bound(bound.run_id)

    assert outcome.state is AgentRunState.failed
    assert outcome.error_category == "job_authority_lost"
    assert jobs.get(job.id).state is JobState.cancelled


def test_restart_reconciles_terminal_run_that_completed_before_now_expired_lease(tmp_path: Path) -> None:
    database = _database(tmp_path)
    jobs, job, bound = _claim(database)
    store = AgentRunStore(database)
    _persist_success(store, bound.run_id)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with database.sessions.begin() as session:
        run = session.get(AgentRunRecord, bound.run_id)
        record = session.get(JobRecord, job.id)
        assert run is not None and record is not None
        run.completed_at = now - timedelta(seconds=20)
        record.lease_expires_at = now - timedelta(seconds=10)

    result = JobTerminalProjector(database).reconcile_terminal_after_restart(bound.run_id)

    assert result.projected is True
    assert jobs.get(job.id).state is JobState.succeeded


def test_terminal_run_after_lease_expiry_cannot_finalize_job(tmp_path: Path) -> None:
    database = _database(tmp_path)
    jobs, job, bound = _claim(database)
    store = AgentRunStore(database)
    _persist_failure(store, bound.run_id)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with database.sessions.begin() as session:
        run = session.get(AgentRunRecord, bound.run_id)
        record = session.get(JobRecord, job.id)
        assert run is not None and record is not None
        record.lease_expires_at = now - timedelta(seconds=20)
        run.completed_at = now - timedelta(seconds=10)

    result = JobTerminalProjector(database).project_terminal(bound.run_id)

    assert result.projected is False
    assert result.claim_expired_before_terminal is True
    assert jobs.get(job.id).state is JobState.running


def test_succeeded_run_without_matching_output_step_is_rejected(tmp_path: Path) -> None:
    database = _database(tmp_path)
    jobs, job, bound = _claim(database)
    AgentRunStore(database).set_state(
        bound.run_id,
        AgentRunState.succeeded,
        final_output={"ok": True},
    )

    with pytest.raises(JobTerminalProjectionError, match="matching durable output step"):
        JobTerminalProjector(database).project_terminal(bound.run_id)

    assert jobs.get(job.id).state is JobState.running


def test_required_evidence_blocks_success_when_no_committed_evidence_exists(tmp_path: Path) -> None:
    database = _database(tmp_path)
    jobs, job, bound = _claim(
        database,
        job_input={"agent_completion": {"require_evidence_refs": True}},
    )

    with pytest.raises(JobTerminalProjectionError, match="requires durable evidence refs"):
        _runtime(database, model=FinishModel()).run_bound(bound.run_id)

    assert AgentRunStore(database).get_run(bound.run_id).state == AgentRunState.succeeded.value
    assert jobs.get(job.id).state is JobState.running


def test_required_evidence_allows_success_after_committed_tool_evidence(tmp_path: Path) -> None:
    database = _database(tmp_path)
    jobs, job, bound = _claim(
        database,
        job_input={"agent_completion": {"require_evidence_refs": True}},
    )
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="evidence.read",
            description="Return one durable evidence reference.",
            input_model=EmptyInput,
            handler=lambda _value: ToolExecutionResult(
                output={"fact": "grounded"},
                evidence_refs=["evidence:test:1"],
            ),
            read_only=True,
        )
    )

    outcome = _runtime(
        database,
        model=EvidenceThenFinishModel(),
        tools=registry,
    ).run_bound(bound.run_id)

    assert outcome.state is AgentRunState.succeeded
    assert jobs.get(job.id).state is JobState.succeeded


def test_unresolved_tool_step_blocks_terminal_success(tmp_path: Path) -> None:
    database = _database(tmp_path)
    jobs, job, bound = _claim(database)
    store = AgentRunStore(database)
    store.append_step(
        bound.run_id,
        kind="tool",
        status="started",
        tool_name="side.effect",
        tool_call_id="side-effect-1",
        input_json={"arguments": {}},
    )
    _persist_success(store, bound.run_id)

    with pytest.raises(JobTerminalProjectionError, match="unresolved Tool"):
        JobTerminalProjector(database).project_terminal(bound.run_id)

    assert jobs.get(job.id).state is JobState.running


def test_newer_binding_prevents_old_terminal_run_from_finalizing_job(tmp_path: Path) -> None:
    database = _database(tmp_path)
    jobs, job, bound = _claim(database, run_id="run-old")
    store = AgentRunStore(database)
    _persist_success(store, bound.run_id)

    newer_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=1)
    with database.sessions.begin() as session:
        session.add(
            AgentRunRecord(
                id="run-new",
                goal="new authority",
                state=AgentRunState.running.value,
                budget_json=RunBudget(max_wall_time_seconds=30).model_dump(mode="json"),
                step_count=0,
                model_calls=0,
                input_tokens=0,
                output_tokens=0,
                created_at=newer_at,
                updated_at=newer_at,
            )
        )
        session.flush()
        session.add(
            AgentJobBindingRecord(
                run_id="run-new",
                job_id=job.id,
                created_at=newer_at,
            )
        )

    with pytest.raises(JobTerminalProjectionError, match="superseded"):
        JobTerminalProjector(database).project_terminal(bound.run_id)

    assert jobs.get(job.id).state is JobState.running


def test_terminal_projection_is_idempotent_after_success(tmp_path: Path) -> None:
    database = _database(tmp_path)
    jobs, job, bound = _claim(database)
    store = AgentRunStore(database)
    _persist_success(store, bound.run_id)
    projector = JobTerminalProjector(database)

    first = projector.project_terminal(bound.run_id)
    second = projector.reconcile_terminal_after_restart(bound.run_id)

    assert first.projected is True
    assert second.projected is False
    assert second.job_state is JobState.succeeded
    assert jobs.get(job.id).state is JobState.succeeded
