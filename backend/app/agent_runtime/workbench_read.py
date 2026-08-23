"""Read-only durable projections for the local Agent workbench UI."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from backend.app.agent_runtime.job_binding import (
    AGENT_ORCHESTRATION_JOB_TYPE,
    AgentJobBindingRecord,
)
from backend.app.agent_runtime.persistence import (
    AgentRunRecord,
    AgentRunStore,
    AgentStepRecord,
    HumanActionRecord,
)
from backend.app.db import Database
from backend.app.models.jobs import JobRecord


class AgentWorkbenchReadError(RuntimeError):
    """The requested durable workbench projection cannot be reconstructed."""


class AgentWorkbenchReader:
    """Build narrow, read-only Job/Run projections from durable SQLite facts."""

    def __init__(self, database: Database) -> None:
        self.database = database
        # The local workbench must be restart-safe even before the first Agent
        # run in a new database. Creating the isolated Agent tables here is
        # schema initialization only; it does not claim or mutate any Job.
        AgentRunStore(database)
        AgentJobBindingRecord.__table__.create(bind=database.engine, checkfirst=True)

    def job_view(self, job_id: str) -> dict[str, Any]:
        with self.database.sessions() as session:
            job = session.get(JobRecord, job_id)
            if job is None:
                raise KeyError(f"Job {job_id} does not exist.")
            if job.type != AGENT_ORCHESTRATION_JOB_TYPE:
                raise AgentWorkbenchReadError(
                    f"Job {job_id} is not an Agent orchestration Job."
                )

            bindings = list(
                session.scalars(
                    select(AgentJobBindingRecord)
                    .where(AgentJobBindingRecord.job_id == job_id)
                    .order_by(
                        AgentJobBindingRecord.created_at,
                        AgentJobBindingRecord.run_id,
                    )
                )
            )
            run_ids = [binding.run_id for binding in bindings]
            runs_by_id: dict[str, AgentRunRecord] = {}
            if run_ids:
                runs_by_id = {
                    run.id: run
                    for run in session.scalars(
                        select(AgentRunRecord).where(AgentRunRecord.id.in_(run_ids))
                    )
                }
                missing = [run_id for run_id in run_ids if run_id not in runs_by_id]
                if missing:
                    raise AgentWorkbenchReadError(
                        "Job binding references missing AgentRun records; projection is incomplete."
                    )

            human_actions: list[HumanActionRecord] = []
            steps: list[AgentStepRecord] = []
            if run_ids:
                human_actions = list(
                    session.scalars(
                        select(HumanActionRecord)
                        .where(HumanActionRecord.run_id.in_(run_ids))
                        .order_by(HumanActionRecord.created_at, HumanActionRecord.id)
                    )
                )
                steps = list(
                    session.scalars(
                        select(AgentStepRecord)
                        .where(AgentStepRecord.run_id.in_(run_ids))
                        .order_by(
                            AgentStepRecord.created_at,
                            AgentStepRecord.run_id,
                            AgentStepRecord.step_index,
                        )
                    )
                )

            current_run_id, authority_ambiguous = self._current_binding(bindings)
            evidence_refs = self._evidence_refs(steps)
            pending_actions = [
                self._human_action_summary(action)
                for action in human_actions
                if action.status == "pending"
            ]

            return {
                "job_id": job.id,
                "job_state": job.state,
                "current_stage": job.current_stage,
                "error_category": job.error_category,
                "retry_count": job.retry_count,
                "lease_expires_at": job.lease_expires_at,
                "current_run_id": current_run_id,
                "authority_ambiguous": authority_ambiguous,
                "runs": [self._run_summary(runs_by_id[run_id]) for run_id in run_ids],
                "pending_human_actions": pending_actions,
                "evidence_refs": evidence_refs,
            }

    def run_view(self, run_id: str) -> dict[str, Any]:
        with self.database.sessions() as session:
            binding = session.get(AgentJobBindingRecord, run_id)
            if binding is None:
                raise KeyError(f"Agent run is not bound to a Job: {run_id}")
            run = session.get(AgentRunRecord, run_id)
            if run is None:
                raise AgentWorkbenchReadError(
                    f"Bound AgentRun {run_id} is missing from durable storage."
                )
            steps = list(
                session.scalars(
                    select(AgentStepRecord)
                    .where(AgentStepRecord.run_id == run_id)
                    .order_by(AgentStepRecord.step_index)
                )
            )
            human_actions = list(
                session.scalars(
                    select(HumanActionRecord)
                    .where(HumanActionRecord.run_id == run_id)
                    .order_by(HumanActionRecord.created_at, HumanActionRecord.id)
                )
            )

            return {
                "job_id": binding.job_id,
                **self._run_summary(run),
                "steps": [self._step_summary(step) for step in steps],
                "human_actions": [
                    self._human_action_summary(action) for action in human_actions
                ],
                "evidence_refs": self._evidence_refs(steps),
            }

    @staticmethod
    def _current_binding(
        bindings: list[AgentJobBindingRecord],
    ) -> tuple[str | None, bool]:
        if not bindings:
            return None, False
        latest_created_at = max(binding.created_at for binding in bindings)
        latest = [
            binding.run_id
            for binding in bindings
            if binding.created_at == latest_created_at
        ]
        if len(latest) != 1:
            return None, True
        return latest[0], False

    @staticmethod
    def _evidence_refs(steps: list[AgentStepRecord]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for step in steps:
            for ref in step.evidence_refs_json or []:
                if ref not in seen:
                    seen.add(ref)
                    ordered.append(ref)
        return ordered

    @staticmethod
    def _run_summary(run: AgentRunRecord) -> dict[str, Any]:
        return {
            "run_id": run.id,
            "goal": run.goal,
            "state": run.state,
            "model_name": run.model_name,
            "prompt_version": run.prompt_version,
            "step_count": run.step_count,
            "model_calls": run.model_calls,
            "input_tokens": run.input_tokens,
            "output_tokens": run.output_tokens,
            "final_output": run.final_output_json,
            "error_category": run.error_category,
            "error_detail": run.error_detail,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
            "completed_at": run.completed_at,
        }

    @staticmethod
    def _step_summary(step: AgentStepRecord) -> dict[str, Any]:
        # Deliberately omit input_json/output_json from the generic workbench
        # surface. The UI gets lifecycle/evidence facts, not raw Tool payloads.
        return {
            "step_index": step.step_index,
            "kind": step.kind,
            "tool_name": step.tool_name,
            "tool_call_id": step.tool_call_id,
            "status": step.status,
            "evidence_refs": list(step.evidence_refs_json or []),
            "error_category": step.error_category,
            "error_detail": step.error_detail,
            "created_at": step.created_at,
            "updated_at": step.updated_at,
        }

    @staticmethod
    def _human_action_summary(action: HumanActionRecord) -> dict[str, Any]:
        # request_json/resolution_json may contain Tool arguments or operator
        # notes. Those belong to explicit approval/handoff capabilities, not the
        # generic read projection.
        return {
            "id": action.id,
            "run_id": action.run_id,
            "tool_call_id": action.tool_call_id,
            "tool_name": action.tool_name,
            "status": action.status,
            "created_at": action.created_at,
            "resolved_at": action.resolved_at,
        }
