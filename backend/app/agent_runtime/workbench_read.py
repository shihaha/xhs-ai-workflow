"""Read-only durable projections for the local Agent workbench UI."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from backend.app.agent_runtime.job_binding import (
    AGENT_ORCHESTRATION_JOB_TYPE,
    AgentJobBindingRecord,
)
from backend.app.agent_runtime.manual_chatgpt_handoff import (
    MANUAL_CHATGPT_REQUIRED,
    MANUAL_CHATGPT_RESULT_READY,
    MANUAL_CHATGPT_TOOL_NAME,
    ManualChatGPTHandoffRecord,
)
from backend.app.agent_runtime.persistence import (
    AgentRunRecord,
    AgentRunStore,
    AgentStepRecord,
    HumanActionRecord,
)
from backend.app.agent_runtime.types import AgentRunState
from backend.app.db import Database
from backend.app.models.jobs import JobArtifactRecord, JobRecord, JobState


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
        ManualChatGPTHandoffRecord.__table__.create(
            bind=database.engine, checkfirst=True
        )

    def list_jobs(self) -> list[dict[str, Any]]:
        """List durable Agent orchestration Jobs without exposing Job inputs."""
        with self.database.sessions() as session:
            job_ids = list(
                session.scalars(
                    select(JobRecord.id)
                    .where(JobRecord.type == AGENT_ORCHESTRATION_JOB_TYPE)
                    .order_by(JobRecord.created_at.desc(), JobRecord.id)
                )
            )

        rows: list[dict[str, Any]] = []
        for job_id in job_ids:
            view = self.job_view(job_id)
            rows.append(
                {
                    "job_id": view["job_id"],
                    "job_state": view["job_state"],
                    "current_stage": view["current_stage"],
                    "error_category": view["error_category"],
                    "retry_count": view["retry_count"],
                    "current_run_id": view["current_run_id"],
                    "authority_ambiguous": view["authority_ambiguous"],
                    "run_count": len(view["runs"]),
                    "pending_human_action_count": len(
                        view["pending_human_actions"]
                    ),
                    "evidence_count": len(view["evidence_refs"]),
                    "artifact_count": len(view["artifacts"]),
                    "created_at": view["created_at"],
                    "updated_at": view["updated_at"],
                }
            )
        return rows

    def list_runs(self) -> list[dict[str, Any]]:
        """List all durable Job-bound AgentRuns with their authoritative Job id."""
        with self.database.sessions() as session:
            bindings = list(
                session.scalars(
                    select(AgentJobBindingRecord)
                    .join(JobRecord, JobRecord.id == AgentJobBindingRecord.job_id)
                    .where(JobRecord.type == AGENT_ORCHESTRATION_JOB_TYPE)
                    .order_by(
                        AgentJobBindingRecord.created_at.desc(),
                        AgentJobBindingRecord.run_id,
                    )
                )
            )
            run_ids = [binding.run_id for binding in bindings]
            if not run_ids:
                return []
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
            return [
                {
                    "job_id": binding.job_id,
                    **self._run_summary(runs_by_id[binding.run_id]),
                }
                for binding in bindings
            ]

    def list_human_actions(self, *, status: str | None = None) -> list[dict[str, Any]]:
        """List HumanActions attached to authoritative Agent orchestration Jobs."""
        with self.database.sessions() as session:
            bindings = list(
                session.scalars(
                    select(AgentJobBindingRecord)
                    .join(JobRecord, JobRecord.id == AgentJobBindingRecord.job_id)
                    .where(JobRecord.type == AGENT_ORCHESTRATION_JOB_TYPE)
                )
            )
            if not bindings:
                return []
            run_to_job = {binding.run_id: binding.job_id for binding in bindings}
            statement = select(HumanActionRecord).where(
                HumanActionRecord.run_id.in_(list(run_to_job))
            )
            if status is not None:
                statement = statement.where(HumanActionRecord.status == status)
            actions = list(
                session.scalars(
                    statement.order_by(
                        HumanActionRecord.created_at.desc(), HumanActionRecord.id
                    )
                )
            )
            return [
                {
                    "job_id": run_to_job[action.run_id],
                    **self._human_action_summary(session, action, run_to_job[action.run_id]),
                }
                for action in actions
            ]

    def list_chatgpt_handoffs(
        self, *, status: str | None = None
    ) -> list[dict[str, Any]]:
        """List durable manual-ChatGPT task identities without raw task/result data."""
        with self.database.sessions() as session:
            statement = select(ManualChatGPTHandoffRecord)
            if status is not None:
                statement = statement.where(ManualChatGPTHandoffRecord.status == status)
            records = list(
                session.scalars(
                    statement.order_by(
                        ManualChatGPTHandoffRecord.created_at.desc(),
                        ManualChatGPTHandoffRecord.id,
                    )
                )
            )
            return [self._chatgpt_handoff_summary(session, record) for record in records]

    def chatgpt_handoff_view(self, handoff_id: str) -> dict[str, Any]:
        with self.database.sessions() as session:
            record = session.get(ManualChatGPTHandoffRecord, handoff_id)
            if record is None:
                raise KeyError(f"ChatGPT handoff {handoff_id} does not exist.")
            return self._chatgpt_handoff_summary(session, record)

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
            artifacts = list(
                session.scalars(
                    select(JobArtifactRecord)
                    .where(JobArtifactRecord.job_id == job_id)
                    .order_by(JobArtifactRecord.id)
                )
            )

            current_run_id, authority_ambiguous = self._current_binding(bindings)
            evidence_refs = self._evidence_refs(steps)
            pending_actions = [
                self._human_action_summary(session, action, job.id)
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
                "created_at": job.created_at,
                "updated_at": job.updated_at,
                "current_run_id": current_run_id,
                "authority_ambiguous": authority_ambiguous,
                "runs": [self._run_summary(runs_by_id[run_id]) for run_id in run_ids],
                "pending_human_actions": pending_actions,
                "evidence_refs": evidence_refs,
                "artifacts": [self._artifact_summary(artifact) for artifact in artifacts],
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
                    self._human_action_summary(session, action, binding.job_id) for action in human_actions
                ],
                "evidence_refs": self._evidence_refs(steps),
            }

    def job_artifacts(self, job_id: str) -> list[dict[str, Any]]:
        return list(self.job_view(job_id)["artifacts"])

    def job_evidence(self, job_id: str) -> dict[str, Any]:
        view = self.job_view(job_id)
        return {"job_id": job_id, "evidence_refs": view["evidence_refs"]}

    def _chatgpt_handoff_summary(
        self, session: Any, record: ManualChatGPTHandoffRecord
    ) -> dict[str, Any]:
        binding = session.get(AgentJobBindingRecord, record.source_run_id)
        source = session.get(AgentRunRecord, record.source_run_id)
        human = session.get(HumanActionRecord, record.human_action_id)
        if binding is None or source is None or human is None:
            raise AgentWorkbenchReadError(
                "ChatGPT handoff lost its Job binding, AgentRun, or HumanAction; projection is incomplete."
            )
        job = session.get(JobRecord, binding.job_id)
        if job is None or job.type != AGENT_ORCHESTRATION_JOB_TYPE:
            raise AgentWorkbenchReadError(
                "ChatGPT handoff is not attached to a durable Agent orchestration Job."
            )
        bindings = list(
            session.scalars(
                select(AgentJobBindingRecord)
                .where(AgentJobBindingRecord.job_id == job.id)
                .order_by(
                    AgentJobBindingRecord.created_at,
                    AgentJobBindingRecord.run_id,
                )
            )
        )
        current_run_id, binding_ambiguous = self._current_binding(bindings)
        identity_valid = (
            human.run_id == record.source_run_id
            and human.tool_name == MANUAL_CHATGPT_TOOL_NAME
            and human.tool_call_id == f"manual-chatgpt:{record.id}"
        )
        is_current_binding = not binding_ambiguous and current_run_id == record.source_run_id
        lease_free = job.lease_expires_at is None
        needs_chatgpt = (
            record.status == "pending"
            and identity_valid
            and is_current_binding
            and human.status == "pending"
            and source.state == "needs_human"
            and source.error_category == MANUAL_CHATGPT_REQUIRED
            and job.state == JobState.needs_human.value
            and job.current_stage == MANUAL_CHATGPT_REQUIRED
            and lease_free
        )
        result_ready = (
            record.status == "accepted"
            and identity_valid
            and is_current_binding
            and human.status == "completed"
            and source.state == "needs_human"
            and source.error_category == MANUAL_CHATGPT_RESULT_READY
            and job.state == JobState.needs_human.value
            and job.current_stage == MANUAL_CHATGPT_RESULT_READY
            and lease_free
        )
        return {
            "handoff_id": record.id,
            "job_id": job.id,
            "source_run_id": record.source_run_id,
            "human_action_id": record.human_action_id,
            "status": record.status,
            "human_action_status": human.status,
            "job_state": job.state,
            "current_stage": job.current_stage,
            "stage_revision": record.stage_revision,
            "schema_version": record.schema_version,
            "input_hash": record.input_hash,
            "context_ref_count": len(record.context_refs_json or []),
            "has_result": record.result_json is not None,
            "is_current_binding": is_current_binding,
            "authority_ambiguous": binding_ambiguous or not identity_valid,
            "needs_chatgpt": needs_chatgpt,
            "result_ready": result_ready,
            "created_at": record.created_at,
            "accepted_at": record.accepted_at,
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

    def _human_action_summary(
        self, session: Any, action: HumanActionRecord, job_id: str
    ) -> dict[str, Any]:
        # request_json/resolution_json may contain Tool arguments or operator
        # notes. Those belong to explicit approval/handoff capabilities, not the
        # generic read projection. ``can_deny`` is a backend-derived command
        # capability: React must not infer permission authority from labels.
        run = session.get(AgentRunRecord, action.run_id)
        job = session.get(JobRecord, job_id)
        bindings = list(
            session.scalars(
                select(AgentJobBindingRecord).where(
                    AgentJobBindingRecord.job_id == job_id
                )
            )
        )
        current_run_id, authority_ambiguous = self._current_binding(bindings)
        can_deny = bool(
            action.status == "pending"
            and run is not None
            and run.state == AgentRunState.needs_human.value
            and run.error_category == "approval_required"
            and job is not None
            and job.type == AGENT_ORCHESTRATION_JOB_TYPE
            and JobState(job.state) is JobState.needs_human
            and job.lease_expires_at is None
            and not authority_ambiguous
            and current_run_id == action.run_id
        )
        return {
            "id": action.id,
            "run_id": action.run_id,
            "tool_call_id": action.tool_call_id,
            "tool_name": action.tool_name,
            "status": action.status,
            "can_deny": can_deny,
            "created_at": action.created_at,
            "resolved_at": action.resolved_at,
        }

    @staticmethod
    def _artifact_summary(artifact: JobArtifactRecord) -> dict[str, Any]:
        # Generic workbench projections expose artifact identity/lifecycle only.
        # Local paths and arbitrary metadata remain behind explicit artifact
        # capabilities because either may contain machine- or account-specific
        # information.
        return {
            "id": artifact.id,
            "job_id": artifact.job_id,
            "kind": artifact.kind,
            "producer": artifact.producer,
            "created_at": artifact.created_at,
        }
