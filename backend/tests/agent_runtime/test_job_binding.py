from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from backend.app.agent_runtime.job_binding import (
    AgentJobCoordinator,
    JobAuthorityError,
    JobAuthorityGuard,
    build_job_read_tool,
)
from backend.app.agent_runtime.persistence import AgentRunRecord, AgentRunStore
from backend.app.agent_runtime.tools import ToolRegistry
from backend.app.agent_runtime.types import AgentRunState, RunBudget
from backend.app.db import Database
from backend.app.models.jobs import JobState
from backend.app.services.jobs import InvalidJobTransition, JobNotFound, JobService


def _database(tmp_path: Path) -> Database:
    return Database(tmp_path / "workbench.sqlite3")


def test_claim_creates_job_run_and_binding_in_one_committed_state(tmp_path: Path) -> None:
    database = _database(tmp_path)
    jobs = JobService(database)
    job = jobs.create(job_type="agent_orchestration", input_data={"scope": "read-only"})
    coordinator = AgentJobCoordinator(database, run_id_factory=lambda: "run-1")

    bound = coordinator.claim_and_create_run(
        job.id,
        goal="read one durable job",
        budget=RunBudget(max_wall_time_seconds=300),
        prompt_version="agent-job-binding-v1",
    )

    claimed = jobs.get(job.id)
    run = AgentRunStore(database).get_run(bound.run_id)

    assert claimed.state is JobState.running
    assert run.state == AgentRunState.running.value
    assert coordinator.job_id_for_run(bound.run_id) == job.id
    assert coordinator.run_ids_for_job(job.id) == [bound.run_id]
    assert claimed.lease_expires_at == bound.lease_expires_at
    assert claimed.lease_expires_at is not None
    assert claimed.started_at is not None
    assert claimed.lease_expires_at - claimed.started_at >= timedelta(seconds=360)
    assert any(bound.run_id in entry.message for entry in claimed.logs)


def test_missing_job_cannot_create_bound_agent_run(tmp_path: Path) -> None:
    database = _database(tmp_path)
    coordinator = AgentJobCoordinator(database)

    with pytest.raises(JobNotFound):
        coordinator.claim_and_create_run(
            "missing-job",
            goal="must not create orphan",
            budget=RunBudget(),
        )

    with database.sessions() as session:
        assert session.get(AgentRunRecord, "missing-job") is None


def test_persistence_conflict_rolls_back_job_claim_and_run_together(tmp_path: Path) -> None:
    database = _database(tmp_path)
    jobs = JobService(database)
    job = jobs.create(job_type="agent_orchestration", input_data={})

    # Force the coordinator's AgentRun insert to violate the AgentRun PK at
    # transaction flush/commit. The Job CAS must roll back with it.
    with database.sessions.begin() as session:
        session.add(
            AgentRunRecord(
                id="duplicate-run",
                goal="preexisting",
                state=AgentRunState.running.value,
                budget_json=RunBudget().model_dump(mode="json"),
                step_count=0,
                model_calls=0,
                input_tokens=0,
                output_tokens=0,
                created_at=job.created_at,
                updated_at=job.created_at,
            )
        )

    coordinator = AgentJobCoordinator(
        database,
        run_id_factory=lambda: "duplicate-run",
    )

    with pytest.raises(IntegrityError):
        coordinator.claim_and_create_run(
            job.id,
            goal="must roll back",
            budget=RunBudget(),
        )

    unchanged = jobs.get(job.id)
    assert unchanged.state is JobState.queued
    assert unchanged.started_at is None
    assert unchanged.lease_expires_at is None
    assert coordinator.run_ids_for_job(job.id) == []


def test_terminal_job_cannot_be_reclaimed_for_agent_run(tmp_path: Path) -> None:
    database = _database(tmp_path)
    jobs = JobService(database)
    job = jobs.create(job_type="agent_orchestration", input_data={})
    jobs.claim(job.id)
    jobs.transition(job.id, JobState.cancelled)

    coordinator = AgentJobCoordinator(database)
    with pytest.raises(InvalidJobTransition):
        coordinator.claim_and_create_run(
            job.id,
            goal="terminal jobs stay terminal",
            budget=RunBudget(),
        )

    assert jobs.get(job.id).state is JobState.cancelled
    assert coordinator.run_ids_for_job(job.id) == []


def test_needs_human_reclaim_increments_retry_and_allows_continuation_run(tmp_path: Path) -> None:
    database = _database(tmp_path)
    jobs = JobService(database)
    job = jobs.create(job_type="agent_orchestration", input_data={})
    coordinator = AgentJobCoordinator(
        database,
        run_id_factory=iter(("run-1", "run-2")).__next__,
    )

    first = coordinator.claim_and_create_run(
        job.id,
        goal="first attempt",
        budget=RunBudget(max_wall_time_seconds=10),
    )
    jobs.transition(job.id, JobState.needs_human, error_category="approval_required")

    second = coordinator.claim_and_create_run(
        job.id,
        goal="continuation attempt",
        budget=RunBudget(max_wall_time_seconds=10),
    )

    current = jobs.get(job.id)
    assert current.state is JobState.running
    assert current.retry_count == 1
    assert coordinator.run_ids_for_job(job.id) == [first.run_id, second.run_id]


def test_lease_is_derived_from_run_budget_plus_margin(tmp_path: Path) -> None:
    database = _database(tmp_path)
    jobs = JobService(database)
    job = jobs.create(job_type="agent_orchestration", input_data={})
    coordinator = AgentJobCoordinator(database, run_id_factory=lambda: "run-budget")

    bound = coordinator.claim_and_create_run(
        job.id,
        goal="derive lease",
        budget=RunBudget(max_wall_time_seconds=7.2),
        shutdown_margin_seconds=60,
    )

    claimed = jobs.get(job.id)
    assert claimed.started_at is not None
    assert bound.lease_expires_at - claimed.started_at >= timedelta(seconds=68)


def test_job_authority_guard_rejects_cancelled_or_expired_job(tmp_path: Path) -> None:
    database = _database(tmp_path)
    jobs = JobService(database)
    job = jobs.create(job_type="agent_orchestration", input_data={})
    coordinator = AgentJobCoordinator(database, run_id_factory=lambda: "run-guard")
    bound = coordinator.claim_and_create_run(
        job.id,
        goal="guard authority",
        budget=RunBudget(max_wall_time_seconds=10),
    )
    guard = JobAuthorityGuard(database)

    guard.require_running(job.id)

    with pytest.raises(JobAuthorityError):
        guard.require_running(job.id, now=bound.lease_expires_at + timedelta(seconds=1))

    jobs.transition(job.id, JobState.cancelled)
    with pytest.raises(JobAuthorityError):
        guard.require_running(job.id)


def test_job_read_tool_exposes_narrow_authoritative_projection(tmp_path: Path) -> None:
    database = _database(tmp_path)
    jobs = JobService(database)
    job = jobs.create(
        job_type="agent_orchestration",
        input_data={"secret_input": "must not leak through job.read"},
        progress_current=1,
        progress_total=3,
        current_stage="inspect",
    )
    coordinator = AgentJobCoordinator(database, run_id_factory=lambda: "run-read")
    coordinator.claim_and_create_run(
        job.id,
        goal="read job",
        budget=RunBudget(max_wall_time_seconds=10),
    )

    registry = ToolRegistry()
    registry.register(build_job_read_tool(jobs))
    tool = registry.resolve("job.read")
    validated = registry.validate(tool, {"job_id": job.id})
    result = registry.execute_validated(
        tool,
        validated,
        run_id="reader-run",
        tool_call_id="read-job-once",
    )

    assert result.output["id"] == job.id
    assert result.output["state"] == JobState.running.value
    assert result.output["current_stage"] == "inspect"
    assert result.output["retry_count"] == 0
    assert "input" not in result.output
    assert "logs" not in result.output
    assert "artifacts" not in result.output
    assert "secret_input" not in str(result.output)
