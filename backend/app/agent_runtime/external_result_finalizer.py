"""Restart-safe deterministic continuation for already-persisted external results.

This module never invokes a model or physical worker.  It turns one verified
external/domain result into a new AgentRun, preserving the rule that continuation
is a new execution trace rather than resuming the older waiting run.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select

from backend.app.agent_runtime.job_binding import (
    AGENT_ORCHESTRATION_JOB_TYPE,
    AgentJobBindingRecord,
    AgentJobCoordinator,
)
from backend.app.agent_runtime.job_bound_runtime import ActiveRunJobAuthorityGuard
from backend.app.agent_runtime.job_terminal_projection import JobTerminalProjector
from backend.app.agent_runtime.job_wait_projection import JobHumanWaitProjector
from backend.app.agent_runtime.persistence import AgentRunRecord, AgentRunStore
from backend.app.agent_runtime.types import AgentRunState, RunBudget
from backend.app.db import Database
from backend.app.models.jobs import JobRecord, JobState
from backend.app.services.jobs import JobService


_EXTERNAL_RESULT_BUDGET = RunBudget(
    max_steps=4,
    max_model_calls=1,
    max_input_tokens=1,
    max_output_tokens=1,
    max_wall_time_seconds=30.0,
)


class ExternalResultFinalizationError(RuntimeError):
    """A verified external result cannot safely close its bounded Agent Job."""


class DeterministicExternalResultFinalizer:
    """Create/recover a no-model continuation and terminalize the parent Job."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.store = AgentRunStore(database)
        self.jobs = JobService(database)
        self.guard = ActiveRunJobAuthorityGuard(database)
        self.wait_projector = JobHumanWaitProjector(database)
        self.terminal_projector = JobTerminalProjector(database)

    def finalize(
        self,
        *,
        source_run_id: str,
        continuation_key: str,
        final_output: dict[str, Any],
        evidence_refs: list[str],
    ) -> str:
        if not continuation_key or len(continuation_key) > 100:
            raise ValueError("continuation_key must be bounded non-empty text")
        if not evidence_refs or len(evidence_refs) != len(set(evidence_refs)):
            raise ValueError("external-result finalization requires distinct evidence refs")
        marker = f"external-result:{continuation_key}"
        if len(marker) > 128:
            raise ValueError("external-result prompt marker is too long")

        job_id = self._job_id_for_source(source_run_id)
        job = self.jobs.get(job_id)
        if job.state in {JobState.succeeded, JobState.failed, JobState.cancelled}:
            if job.state is JobState.succeeded:
                existing = self._latest_run(job_id)
                if existing is not None and existing.prompt_version == marker:
                    return existing.id
            raise ExternalResultFinalizationError(
                f"Terminal Job {job_id} cannot be reopened for external-result finalization."
            )

        latest = self._latest_run(job_id)
        if latest is not None and latest.prompt_version == marker:
            return self._finish_or_recover_existing(job_id, latest, marker, final_output, evidence_refs)

        if latest is None or latest.id != source_run_id:
            raise ExternalResultFinalizationError(
                "Source AgentRun is no longer the current Job binding for this external result."
            )
        source = self.store.get_run(source_run_id)
        if source.state != AgentRunState.needs_human.value:
            raise ExternalResultFinalizationError(
                "Source AgentRun is not at a durable needs_human boundary."
            )
        if job.state is not JobState.needs_human or job.lease_expires_at is not None:
            raise ExternalResultFinalizationError(
                "Parent Job is not a lease-free needs_human external-result boundary."
            )

        run_id = self._claim_result_run(job_id, marker)
        return self._finish_current_run(run_id, final_output, evidence_refs)

    def _finish_or_recover_existing(
        self,
        job_id: str,
        run: AgentRunRecord,
        marker: str,
        final_output: dict[str, Any],
        evidence_refs: list[str],
    ) -> str:
        if run.state == AgentRunState.succeeded.value:
            self.terminal_projector.project_terminal(run.id)
            return run.id
        if run.state == AgentRunState.running.value:
            job = self.jobs.get(job_id)
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            if job.state is JobState.running and job.lease_expires_at is not None:
                if job.lease_expires_at > now:
                    return self._finish_current_run(run.id, final_output, evidence_refs)
                # No model/tool/physical work exists in this continuation.  Mark
                # the expired trace interrupted, project that durable wait, then
                # create a fresh continuation rather than writing under an expired claim.
                self.store.set_state(
                    run.id,
                    AgentRunState.needs_human,
                    error_category="interrupted",
                    error_detail="Deterministic external-result continuation lease expired.",
                )
                self.wait_projector.project_wait(run.id)
                new_run_id = self._claim_result_run(job_id, marker)
                return self._finish_current_run(new_run_id, final_output, evidence_refs)
        if run.state == AgentRunState.needs_human.value:
            job = self.jobs.get(job_id)
            if job.state is JobState.needs_human and job.lease_expires_at is None:
                new_run_id = self._claim_result_run(job_id, marker)
                return self._finish_current_run(new_run_id, final_output, evidence_refs)
        raise ExternalResultFinalizationError(
            "Existing external-result continuation is not safely recoverable."
        )

    def _claim_result_run(self, job_id: str, marker: str) -> str:
        bound = AgentJobCoordinator(self.database).claim_and_create_run(
            job_id,
            goal="应用已经持久化并验证的外部推理结果，不执行新的物理操作。",
            budget=_EXTERNAL_RESULT_BUDGET,
            model_name="chatgpt-external-result",
            prompt_version=marker,
        )
        return bound.run_id

    def _finish_current_run(
        self,
        run_id: str,
        final_output: dict[str, Any],
        evidence_refs: list[str],
    ) -> str:
        run = self.store.get_run(run_id)
        if run.state == AgentRunState.succeeded.value:
            self.terminal_projector.project_terminal(run_id)
            return run_id
        if run.state != AgentRunState.running.value:
            raise ExternalResultFinalizationError(
                "External-result continuation is not running."
            )
        outputs = [
            step
            for step in self.store.list_steps(run_id)
            if step.kind == "output" and step.status == "succeeded"
        ]
        if outputs:
            if outputs[-1].output_json != final_output:
                raise ExternalResultFinalizationError(
                    "Existing external-result output does not match the verified result."
                )
        else:
            self.store.append_step(
                run_id,
                kind="output",
                status="succeeded",
                output_json=final_output,
                evidence_refs=evidence_refs,
            )
        self.store.set_state(
            run_id,
            AgentRunState.succeeded,
            final_output=final_output,
        )
        self.terminal_projector.project_terminal(run_id)
        return run_id

    def _job_id_for_source(self, source_run_id: str) -> str:
        with self.database.sessions() as session:
            binding = session.get(AgentJobBindingRecord, source_run_id)
            if binding is None:
                raise ExternalResultFinalizationError(
                    "External result source run has no durable Job binding."
                )
            job = session.get(JobRecord, binding.job_id)
            if job is None or job.type != AGENT_ORCHESTRATION_JOB_TYPE:
                raise ExternalResultFinalizationError(
                    "External result source is not bound to an Agent orchestration Job."
                )
            return job.id

    def _latest_run(self, job_id: str) -> AgentRunRecord | None:
        with self.database.sessions() as session:
            latest_at = session.scalar(
                select(func.max(AgentJobBindingRecord.created_at)).where(
                    AgentJobBindingRecord.job_id == job_id
                )
            )
            if latest_at is None:
                return None
            run_ids = list(
                session.scalars(
                    select(AgentJobBindingRecord.run_id).where(
                        AgentJobBindingRecord.job_id == job_id,
                        AgentJobBindingRecord.created_at == latest_at,
                    )
                )
            )
            if len(run_ids) != 1:
                raise ExternalResultFinalizationError(
                    "Agent Job has ambiguous latest run authority."
                )
            run = session.get(AgentRunRecord, run_ids[0])
            if run is None:
                raise ExternalResultFinalizationError(
                    "Latest AgentRun binding points to a missing run."
                )
            session.expunge(run)
            return run
