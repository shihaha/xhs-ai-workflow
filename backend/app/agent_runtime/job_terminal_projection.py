"""Project durable terminal AgentRun proof into the authoritative Job state.

The Agent execution trace may reach ``succeeded``/``failed`` before the Job row
is finalized.  This module closes that crash window without letting a stale run
overwrite cancellation, a newer continuation claim, or an expired claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import exists, func, select, update

from backend.app.agent_runtime.job_binding import (
    AGENT_ORCHESTRATION_JOB_TYPE,
    AgentJobBindingRecord,
)
from backend.app.agent_runtime.persistence import AgentRunStore
from backend.app.agent_runtime.types import AgentRunState
from backend.app.db import Database
from backend.app.models.jobs import JobLogRecord, JobRecord, JobState
from backend.app.services.jobs import JobService


_TERMINAL_JOB_STATES = {JobState.succeeded, JobState.failed, JobState.cancelled}
_UNRESOLVED_TOOL_STATUSES = {"started", "uncertain", "needs_human"}


class JobTerminalProjectionError(RuntimeError):
    """A terminal AgentRun cannot safely finalize its authoritative Job."""


@dataclass(frozen=True, slots=True)
class JobTerminalProjectionResult:
    run_id: str
    job_id: str
    job_state: JobState
    projected: bool
    terminal_job_won: bool = False
    claim_expired_before_terminal: bool = False
    evidence_refs: tuple[str, ...] = ()


class JobTerminalProjector:
    """CAS one proven terminal AgentRun into one authoritative Job terminal state.

    Safety rules:
    - only ``agent_orchestration`` Jobs are eligible;
    - the run must still be the single unambiguous latest binding;
    - success requires durable final output plus its durable output step;
    - failure requires a durable matching error step;
    - no pending HumanAction or unresolved Tool step may remain;
    - optional evidence/artifact requirements are read from durable Job input;
    - the AgentRun terminal timestamp must be within the observed Job lease;
    - the final UPDATE re-checks claim generation and latest-binding identity.

    Because ``completed_at <= lease_expires_at`` is durable proof that the run
    finished while it still owned that claim, restart reconciliation may safely
    finalize even when wall-clock time is now past the lease expiry.  If the run
    itself completed after lease expiry, projection is refused and normal Job
    lease recovery remains authoritative.
    """

    def __init__(self, database: Database) -> None:
        self.database = database
        self.store = AgentRunStore(database)
        self.jobs = JobService(database)
        AgentJobBindingRecord.__table__.create(bind=database.engine, checkfirst=True)

    def project_terminal(self, run_id: str) -> JobTerminalProjectionResult:
        job_id = self._require_current_binding(run_id)
        run = self.store.get_run(run_id)
        if run.state not in {AgentRunState.succeeded.value, AgentRunState.failed.value}:
            raise JobTerminalProjectionError(
                f"Agent run {run_id} is {run.state!r}; only succeeded/failed may be projected."
            )
        if run.completed_at is None:
            raise JobTerminalProjectionError(
                f"Terminal Agent run {run_id} has no durable completed_at timestamp."
            )

        desired = (
            JobState.succeeded
            if run.state == AgentRunState.succeeded.value
            else JobState.failed
        )
        current = self.jobs.get(job_id)

        if current.state in _TERMINAL_JOB_STATES:
            return JobTerminalProjectionResult(
                run_id=run_id,
                job_id=job_id,
                job_state=current.state,
                projected=False,
                terminal_job_won=current.state is not desired,
            )
        if current.state is not JobState.running:
            raise JobTerminalProjectionError(
                f"Job {job_id} is {current.state.value}; terminal Agent result cannot overwrite it."
            )
        if current.lease_expires_at is None:
            raise JobTerminalProjectionError(
                f"Job {job_id} is running without a lease; refusing terminal projection."
            )

        steps = self.store.list_steps(run_id)
        pending_human = self.store.pending_human_action(run_id)
        if pending_human is not None:
            raise JobTerminalProjectionError(
                f"Terminal Agent run {run_id} still has pending HumanAction {pending_human.id}."
            )
        unresolved = [
            step
            for step in steps
            if step.kind == "tool" and step.status in _UNRESOLVED_TOOL_STATUSES
        ]
        if unresolved:
            raise JobTerminalProjectionError(
                f"Terminal Agent run {run_id} still has unresolved Tool step(s)."
            )

        evidence_refs = tuple(
            dict.fromkeys(
                ref
                for step in steps
                if step.status in {"succeeded", "reused"}
                for ref in (step.evidence_refs_json or [])
            )
        )

        if desired is JobState.succeeded:
            self._require_success_proof(run, steps)
            self._require_completion_policy(current.input, current.artifacts, evidence_refs)
        else:
            self._require_failure_proof(run, steps)

        # A provider/error path may complete after its claim expires.  In that
        # case the Agent trace is retained, but it may not terminalize the Job;
        # lease recovery will move the Job to needs_human instead.
        if run.completed_at > current.lease_expires_at:
            return JobTerminalProjectionResult(
                run_id=run_id,
                job_id=job_id,
                job_state=current.state,
                projected=False,
                claim_expired_before_terminal=True,
                evidence_refs=evidence_refs,
            )

        binding_table = AgentJobBindingRecord.__table__
        max_binding = binding_table.alias("terminal_max_binding")
        count_binding = binding_table.alias("terminal_count_binding")
        latest_created_at = (
            select(func.max(max_binding.c.created_at))
            .where(max_binding.c.job_id == job_id)
            .scalar_subquery()
        )
        latest_binding_count = (
            select(func.count())
            .select_from(count_binding)
            .where(
                count_binding.c.job_id == job_id,
                count_binding.c.created_at == latest_created_at,
            )
            .scalar_subquery()
        )
        current_run_is_latest = exists().where(
            binding_table.c.job_id == job_id,
            binding_table.c.run_id == run_id,
            binding_table.c.created_at == latest_created_at,
        )

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        values: dict[str, Any] = {
            "state": desired.value,
            "updated_at": now,
            "completed_at": now,
            "lease_expires_at": None,
            "error_category": None if desired is JobState.succeeded else run.error_category,
        }
        with self.database.sessions.begin() as session:
            changed = session.execute(
                update(JobRecord)
                .where(
                    JobRecord.id == job_id,
                    JobRecord.type == AGENT_ORCHESTRATION_JOB_TYPE,
                    JobRecord.state == JobState.running.value,
                    JobRecord.retry_count == current.retry_count,
                    JobRecord.lease_expires_at == current.lease_expires_at,
                    current_run_is_latest,
                    latest_binding_count == 1,
                )
                .values(**values)
            )
            if changed.rowcount == 1:
                session.add(
                    JobLogRecord(
                        job_id=job_id,
                        level="info" if desired is JobState.succeeded else "error",
                        message=(
                            f"Authoritative Agent run {run_id} finalized this Job as "
                            f"{desired.value}; terminal proof was already durable."
                        ),
                        created_at=now,
                    )
                )
            projected = changed.rowcount == 1

        if not projected:
            latest = self.jobs.get(job_id)
            if latest.state in _TERMINAL_JOB_STATES:
                return JobTerminalProjectionResult(
                    run_id=run_id,
                    job_id=job_id,
                    job_state=latest.state,
                    projected=False,
                    terminal_job_won=latest.state is not desired,
                    evidence_refs=evidence_refs,
                )
            raise JobTerminalProjectionError(
                f"Job {job_id} claim/binding changed during terminal projection; "
                "stale Agent terminal state was not applied."
            )

        latest = self.jobs.get(job_id)
        if latest.state is not desired or latest.lease_expires_at is not None:
            raise JobTerminalProjectionError(
                f"Job {job_id} terminal projection committed an invalid lifecycle state."
            )
        return JobTerminalProjectionResult(
            run_id=run_id,
            job_id=job_id,
            job_state=latest.state,
            projected=True,
            evidence_refs=evidence_refs,
        )

    def reconcile_terminal_after_restart(self, run_id: str) -> JobTerminalProjectionResult:
        """Re-apply only the terminal Job CAS; never replay model or Tool work."""

        return self.project_terminal(run_id)

    def _require_current_binding(self, run_id: str) -> str:
        with self.database.sessions() as session:
            binding = session.get(AgentJobBindingRecord, run_id)
            if binding is None:
                raise JobTerminalProjectionError(
                    f"Agent run {run_id} has no durable Job binding."
                )
            job = session.get(JobRecord, binding.job_id)
            if job is None:
                raise JobTerminalProjectionError(
                    f"Bound Job {binding.job_id} does not exist."
                )
            if job.type != AGENT_ORCHESTRATION_JOB_TYPE:
                raise JobTerminalProjectionError(
                    f"Bound Job {job.id} has ineligible type {job.type!r}."
                )
            latest_at = session.scalar(
                select(func.max(AgentJobBindingRecord.created_at)).where(
                    AgentJobBindingRecord.job_id == job.id
                )
            )
            latest_ids = [] if latest_at is None else list(
                session.scalars(
                    select(AgentJobBindingRecord.run_id).where(
                        AgentJobBindingRecord.job_id == job.id,
                        AgentJobBindingRecord.created_at == latest_at,
                    )
                )
            )
        if len(latest_ids) != 1:
            raise JobTerminalProjectionError(
                f"Bound Job {binding.job_id} has ambiguous latest AgentRun authority."
            )
        if latest_ids[0] != run_id:
            raise JobTerminalProjectionError(
                f"Agent run {run_id} has been superseded by a newer continuation run."
            )
        return binding.job_id

    @staticmethod
    def _require_success_proof(run: Any, steps: list[Any]) -> None:
        if run.final_output_json is None:
            raise JobTerminalProjectionError(
                "Succeeded AgentRun has no durable final_output_json."
            )
        outputs = [
            step for step in steps if step.kind == "output" and step.status == "succeeded"
        ]
        if not outputs or outputs[-1].output_json != run.final_output_json:
            raise JobTerminalProjectionError(
                "Succeeded AgentRun final output is not backed by a matching durable output step."
            )

    @staticmethod
    def _require_failure_proof(run: Any, steps: list[Any]) -> None:
        if not run.error_category or not run.error_detail:
            raise JobTerminalProjectionError(
                "Failed AgentRun has no durable error category/detail."
            )
        errors = [
            step for step in steps if step.kind == "error" and step.status == "failed"
        ]
        if not errors:
            raise JobTerminalProjectionError(
                "Failed AgentRun has no durable error step."
            )
        last = errors[-1]
        if last.error_category != run.error_category or last.error_detail != run.error_detail:
            raise JobTerminalProjectionError(
                "Failed AgentRun terminal error does not match its durable error step."
            )

    @staticmethod
    def _require_completion_policy(
        job_input: dict[str, Any],
        artifacts: list[Any],
        evidence_refs: tuple[str, ...],
    ) -> None:
        raw = job_input.get("agent_completion", {})
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise JobTerminalProjectionError("Job agent_completion policy must be an object.")

        require_evidence = raw.get("require_evidence_refs", False)
        if not isinstance(require_evidence, bool):
            raise JobTerminalProjectionError(
                "agent_completion.require_evidence_refs must be boolean."
            )
        if require_evidence and not evidence_refs:
            raise JobTerminalProjectionError(
                "Job requires durable evidence refs before Agent success may finalize it."
            )

        required_kinds = raw.get("required_artifact_kinds", [])
        if not isinstance(required_kinds, list) or any(
            not isinstance(kind, str) or not kind for kind in required_kinds
        ):
            raise JobTerminalProjectionError(
                "agent_completion.required_artifact_kinds must be a list of non-empty strings."
            )
        actual = {artifact.kind for artifact in artifacts}
        missing = sorted(set(required_kinds) - actual)
        if missing:
            raise JobTerminalProjectionError(
                "Job is missing required durable artifact kind(s): " + ", ".join(missing)
            )
