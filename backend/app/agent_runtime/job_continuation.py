"""Approved human-wait continuation for Job-bound Agent runs.

This module is intentionally staged and not package-root exported. It owns the
narrow transaction that resolves one exact pending HumanAction, re-claims the
authoritative Job, and creates a *new* AgentRun with only the remaining durable
step/model/token budget.

It does not drive physical Android/XHS/browser workers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import DateTime, String, case, exists, func, select, update
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.agent_runtime.context import DefaultContextBuilder
from backend.app.agent_runtime.job_binding import (
    AGENT_ORCHESTRATION_JOB_TYPE,
    DEFAULT_SHUTDOWN_MARGIN_SECONDS,
    AgentJobBindingBase,
    AgentJobBindingRecord,
)
from backend.app.agent_runtime.persistence import (
    AgentCheckpointRecord,
    AgentRunRecord,
    AgentRunStore,
    HumanActionRecord,
)
from backend.app.agent_runtime.types import AgentRunState, NextAction, RunBudget
from backend.app.db import Database
from backend.app.models.jobs import JobLogRecord, JobRecord, JobState


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ContinuationApprovalError(RuntimeError):
    """An approval cannot safely create a new Job-bound AgentRun."""


class AgentContinuationRecord(AgentJobBindingBase):
    """Durably link a continuation run to the exact approved HumanAction."""

    __tablename__ = "agent_continuations"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    human_action_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    tool_call_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


@dataclass(frozen=True, slots=True)
class ApprovedContinuation:
    job_id: str
    source_run_id: str
    run_id: str
    human_action_id: str
    action: NextAction
    remaining_budget: RunBudget
    lease_expires_at: datetime


@dataclass(slots=True)
class _HistoricalStep:
    step_index: int
    kind: str
    tool_name: str | None
    status: str
    input_json: dict[str, Any] | None
    output_json: dict[str, Any] | None
    evidence_refs_json: list[str] | None


class JobContinuationCoordinator:
    """Atomically approve and create a new continuation AgentRun."""

    def __init__(
        self,
        database: Database,
        *,
        run_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.database = database
        self._run_id_factory = run_id_factory or (lambda: str(uuid4()))
        self.store = AgentRunStore(database)
        AgentJobBindingRecord.__table__.create(bind=database.engine, checkfirst=True)
        AgentContinuationRecord.__table__.create(bind=database.engine, checkfirst=True)

    def approve_and_create_continuation(
        self,
        source_run_id: str,
        human_action_id: str,
        *,
        note: str | None = None,
        shutdown_margin_seconds: int = DEFAULT_SHUTDOWN_MARGIN_SECONDS,
    ) -> ApprovedContinuation:
        """Resolve one exact approval and re-claim the Job in one transaction.

        The old waiting AgentRun is never reopened. The new run receives only
        remaining step/model/token allowances from the root Job budget policy.
        """

        if shutdown_margin_seconds < 1:
            raise ValueError("shutdown_margin_seconds must be positive")

        now = _utcnow()
        new_run_id = self._run_id_factory()

        with self.database.sessions.begin() as session:
            source_binding = session.get(AgentJobBindingRecord, source_run_id)
            if source_binding is None:
                raise ContinuationApprovalError(
                    f"Agent run {source_run_id} has no durable Job binding."
                )
            job_id = source_binding.job_id
            job = session.get(JobRecord, job_id)
            source = session.get(AgentRunRecord, source_run_id)
            human = session.get(HumanActionRecord, human_action_id)

            if job is None or source is None or human is None:
                raise ContinuationApprovalError(
                    "Job, source AgentRun, and HumanAction must all exist."
                )
            if job.type != AGENT_ORCHESTRATION_JOB_TYPE:
                raise ContinuationApprovalError(
                    f"Job {job_id} has ineligible type {job.type!r}."
                )
            if JobState(job.state) is not JobState.needs_human:
                raise ContinuationApprovalError(
                    f"Job {job_id} is {JobState(job.state).value}; approval requires needs_human."
                )
            if job.lease_expires_at is not None:
                raise ContinuationApprovalError(
                    f"Job {job_id} is needs_human but still owns a lease."
                )
            if source.state != AgentRunState.needs_human.value:
                raise ContinuationApprovalError(
                    f"Source run {source_run_id} is {source.state!r}; approval requires needs_human."
                )
            if source.error_category != "approval_required":
                raise ContinuationApprovalError(
                    "Only permission-gated approval_required waits use this continuation path."
                )
            if human.run_id != source_run_id or human.status != "pending":
                raise ContinuationApprovalError(
                    "HumanAction is not the pending approval for the source run."
                )

            try:
                action = NextAction.model_validate(human.request_json["action"])
            except (KeyError, ValueError) as exc:
                raise ContinuationApprovalError(
                    "Pending HumanAction does not contain a valid persisted action."
                ) from exc
            if action.action != "tool" or action.tool_name is None:
                raise ContinuationApprovalError(
                    "Only an exact persisted tool action may be approved for continuation."
                )
            if human.tool_call_id != action.tool_call_id or human.tool_name != action.tool_name:
                raise ContinuationApprovalError(
                    "HumanAction identity does not match its persisted tool action."
                )

            self._require_unique_latest_binding(session, job_id, source_run_id)
            remaining_budget = self._remaining_budget(session, job_id)
            lease_seconds = ceil(
                remaining_budget.max_wall_time_seconds + shutdown_margin_seconds
            )
            lease_expires_at = now + timedelta(seconds=lease_seconds)

            # Close both stale-claim and stale-binding TOCTOU windows at the
            # authoritative Job write boundary. A point-in-time approval check
            # is not a durable capability.
            latest_created_at, latest_binding_count, source_is_latest = (
                self._latest_binding_predicates(job_id, source_run_id)
            )
            _ = latest_created_at  # retained for readability/debugging
            claimed = session.execute(
                update(JobRecord)
                .where(
                    JobRecord.id == job_id,
                    JobRecord.type == AGENT_ORCHESTRATION_JOB_TYPE,
                    JobRecord.state == JobState.needs_human.value,
                    JobRecord.retry_count == job.retry_count,
                    JobRecord.lease_expires_at.is_(None),
                    source_is_latest,
                    latest_binding_count == 1,
                )
                .values(
                    state=JobState.running.value,
                    retry_count=JobRecord.retry_count + 1,
                    started_at=case(
                        (JobRecord.started_at.is_(None), now),
                        else_=JobRecord.started_at,
                    ),
                    lease_expires_at=lease_expires_at,
                    updated_at=now,
                )
            )
            if claimed.rowcount != 1:
                raise ContinuationApprovalError(
                    "Job authority changed during approval; no continuation was committed."
                )

            resolved = session.execute(
                update(HumanActionRecord)
                .where(
                    HumanActionRecord.id == human_action_id,
                    HumanActionRecord.run_id == source_run_id,
                    HumanActionRecord.status == "pending",
                )
                .values(
                    status="approved",
                    resolution_json={"approved": True, "note": note},
                    resolved_at=now,
                )
            )
            if resolved.rowcount != 1:
                raise ContinuationApprovalError(
                    "HumanAction changed during approval; no continuation was committed."
                )

            session.add(
                AgentRunRecord(
                    id=new_run_id,
                    goal=source.goal,
                    state=AgentRunState.running.value,
                    model_name=source.model_name,
                    prompt_version=source.prompt_version,
                    budget_json=remaining_budget.model_dump(mode="json"),
                    step_count=0,
                    model_calls=0,
                    input_tokens=0,
                    output_tokens=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                AgentJobBindingRecord(
                    run_id=new_run_id,
                    job_id=job_id,
                    created_at=now,
                )
            )
            session.add(
                AgentContinuationRecord(
                    run_id=new_run_id,
                    source_run_id=source_run_id,
                    human_action_id=human_action_id,
                    tool_call_id=action.tool_call_id,
                    created_at=now,
                )
            )

            # These mapped tables do not declare ORM relationships that would
            # give SQLAlchemy an insert dependency graph. Flush the AgentRun and
            # its binding/link explicitly before inserting a checkpoint whose
            # SQLite FK references agent_runs. This remains inside the same
            # transaction: any later failure still rolls back Job claim,
            # HumanAction approval, run, binding, and continuation link together.
            session.flush()

            # A checkpoint does not increment Agent step_count, so the audit link
            # does not silently consume the continuation's remaining step budget.
            session.add(
                AgentCheckpointRecord(
                    run_id=new_run_id,
                    step_index=0,
                    state_json={
                        "state": AgentRunState.running.value,
                        "continuation_from_run_id": source_run_id,
                        "human_action_id": human_action_id,
                        "approved_tool_call_id": action.tool_call_id,
                    },
                    created_at=now,
                )
            )
            session.add(
                JobLogRecord(
                    job_id=job_id,
                    level="info",
                    message=(
                        f"HumanAction {human_action_id} approved; continuation Agent run "
                        f"{new_run_id} re-claimed this job from {source_run_id}."
                    ),
                    created_at=now,
                )
            )

        return ApprovedContinuation(
            job_id=job_id,
            source_run_id=source_run_id,
            run_id=new_run_id,
            human_action_id=human_action_id,
            action=action,
            remaining_budget=remaining_budget,
            lease_expires_at=lease_expires_at,
        )

    def approved_action_for_run(self, run_id: str) -> NextAction:
        """Recover the exact durable approved action after a process restart."""

        with self.database.sessions() as session:
            continuation = session.get(AgentContinuationRecord, run_id)
            if continuation is None:
                raise ContinuationApprovalError(
                    f"Agent run {run_id} is not an approved continuation."
                )
            human = session.get(HumanActionRecord, continuation.human_action_id)
            if human is None:
                raise ContinuationApprovalError("Approved continuation lost its HumanAction.")
            if human.run_id != continuation.source_run_id or human.status != "approved":
                raise ContinuationApprovalError(
                    "Continuation HumanAction is not durably approved for its source run."
                )
            resolution = human.resolution_json or {}
            if resolution.get("approved") is not True:
                raise ContinuationApprovalError(
                    "Continuation HumanAction has no affirmative approval resolution."
                )
            try:
                action = NextAction.model_validate(human.request_json["action"])
            except (KeyError, ValueError) as exc:
                raise ContinuationApprovalError(
                    "Approved HumanAction no longer contains a valid action."
                ) from exc
            if (
                action.action != "tool"
                or action.tool_call_id != continuation.tool_call_id
                or action.tool_call_id != human.tool_call_id
                or action.tool_name != human.tool_name
            ):
                raise ContinuationApprovalError(
                    "Approved continuation action identity does not match durable approval."
                )
            return action

    def _remaining_budget(self, session: Any, job_id: str) -> RunBudget:
        bindings = list(
            session.scalars(
                select(AgentJobBindingRecord)
                .where(AgentJobBindingRecord.job_id == job_id)
                .order_by(AgentJobBindingRecord.created_at, AgentJobBindingRecord.run_id)
            )
        )
        if not bindings:
            raise ContinuationApprovalError("Job has no AgentRun history for budget carry-forward.")

        oldest_at = bindings[0].created_at
        if sum(1 for binding in bindings if binding.created_at == oldest_at) != 1:
            raise ContinuationApprovalError(
                "Root AgentRun budget is ambiguous because oldest bindings share a timestamp."
            )

        runs: list[AgentRunRecord] = []
        for binding in bindings:
            run = session.get(AgentRunRecord, binding.run_id)
            if run is None:
                raise ContinuationApprovalError(
                    f"Bound AgentRun {binding.run_id} is missing; budget cannot be reconstructed."
                )
            runs.append(run)

        root = RunBudget.model_validate(runs[0].budget_json)
        remaining = {
            "max_steps": root.max_steps - sum(run.step_count for run in runs),
            "max_model_calls": root.max_model_calls - sum(run.model_calls for run in runs),
            "max_input_tokens": root.max_input_tokens - sum(run.input_tokens for run in runs),
            "max_output_tokens": root.max_output_tokens - sum(run.output_tokens for run in runs),
        }
        exhausted = [name for name, value in remaining.items() if value <= 0]
        if exhausted:
            raise ContinuationApprovalError(
                "Continuation budget exhausted before re-claim: " + ", ".join(exhausted)
            )

        return RunBudget(
            **remaining,
            # Wall time remains an active-claim policy until durable active
            # execution time (excluding human wait) is modeled separately.
            max_wall_time_seconds=root.max_wall_time_seconds,
        )

    @staticmethod
    def _require_unique_latest_binding(session: Any, job_id: str, run_id: str) -> None:
        latest_at = session.scalar(
            select(func.max(AgentJobBindingRecord.created_at)).where(
                AgentJobBindingRecord.job_id == job_id
            )
        )
        if latest_at is None:
            raise ContinuationApprovalError("Job has no latest AgentRun binding.")
        latest_ids = list(
            session.scalars(
                select(AgentJobBindingRecord.run_id).where(
                    AgentJobBindingRecord.job_id == job_id,
                    AgentJobBindingRecord.created_at == latest_at,
                )
            )
        )
        if len(latest_ids) != 1 or latest_ids[0] != run_id:
            raise ContinuationApprovalError(
                "Source AgentRun is not the single unambiguous latest Job binding."
            )

    @staticmethod
    def _latest_binding_predicates(job_id: str, run_id: str):
        table = AgentJobBindingRecord.__table__
        max_binding = table.alias("continuation_max_binding")
        count_binding = table.alias("continuation_count_binding")
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
        source_is_latest = exists().where(
            table.c.job_id == job_id,
            table.c.run_id == run_id,
            table.c.created_at == latest_created_at,
        )
        return latest_created_at, latest_binding_count, source_is_latest


class JobHistoryContextBuilder(DefaultContextBuilder):
    """Build bounded model context across Job-bound historical AgentRuns.

    Old AgentStep rows are never copied into the continuation run. They are read
    and assigned an in-memory monotonic order only for model context rendering.
    """

    def __init__(
        self,
        database: Database,
        *,
        max_recent_steps: int = 20,
        max_step_chars: int = 2_000,
    ) -> None:
        super().__init__(
            max_recent_steps=max_recent_steps,
            max_step_chars=max_step_chars,
        )
        self.database = database
        self.store = AgentRunStore(database)
        AgentJobBindingRecord.__table__.create(bind=database.engine, checkfirst=True)

    def build(
        self,
        *,
        goal: str,
        run_id: str,
        steps: list[Any],
        tools: list[dict[str, Any]],
        usage: dict[str, int],
        remaining_budget: dict[str, int | float],
    ):
        del steps  # current-run rows are included from the durable Job history below
        with self.database.sessions() as session:
            current = session.get(AgentJobBindingRecord, run_id)
            if current is None:
                raise ContinuationApprovalError(
                    f"Agent run {run_id} has no Job binding for historical context."
                )
            bindings = list(
                session.scalars(
                    select(AgentJobBindingRecord)
                    .where(AgentJobBindingRecord.job_id == current.job_id)
                    .order_by(AgentJobBindingRecord.created_at, AgentJobBindingRecord.run_id)
                )
            )

        run_ids = [binding.run_id for binding in bindings]
        if run_id not in run_ids:
            raise ContinuationApprovalError("Current AgentRun disappeared from Job history.")
        # Do not expose any future/superseding run even if this builder is called
        # on stale state. The execution guard separately denies stale authority.
        run_ids = run_ids[: run_ids.index(run_id) + 1]

        history: list[_HistoricalStep] = []
        ordinal = 0
        for historical_run_id in run_ids:
            for step in self.store.list_steps(historical_run_id):
                ordinal += 1
                history.append(
                    _HistoricalStep(
                        step_index=ordinal,
                        kind=step.kind,
                        tool_name=step.tool_name,
                        status=step.status,
                        input_json=step.input_json,
                        output_json=step.output_json,
                        evidence_refs_json=list(step.evidence_refs_json or []),
                    )
                )

        return super().build(
            goal=goal,
            run_id=run_id,
            steps=history,
            tools=tools,
            usage=usage,
            remaining_budget=remaining_budget,
        )
