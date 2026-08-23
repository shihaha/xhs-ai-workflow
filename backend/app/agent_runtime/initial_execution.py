"""Durable initial Agent orchestration launch and execution.

This module owns the missing Stage-5 boundary before continuation exists:
operator-selected evidence -> authoritative Agent Job -> bound AgentRun ->
durable dispatch -> app-owned executor.  It does not own physical workers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil
from threading import Event, RLock, Thread
from typing import Callable
from uuid import uuid4

from sqlalchemy import DateTime, Integer, JSON, String, select, update
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.agent_runtime.job_binding import (
    AGENT_ORCHESTRATION_JOB_TYPE,
    DEFAULT_SHUTDOWN_MARGIN_SECONDS,
    AgentJobBindingBase,
    AgentJobBindingRecord,
)
from backend.app.agent_runtime.job_bound_runtime import (
    ActiveRunJobAuthorityGuard,
    JobBoundAgentRuntime,
)
from backend.app.agent_runtime.job_terminal_projection import JobTerminalProjector
from backend.app.agent_runtime.job_wait_projection import JobHumanWaitProjector
from backend.app.agent_runtime.persistence import AgentRunRecord, AgentRunStore, AgentStepRecord
from backend.app.agent_runtime.types import AgentRunState, RunBudget
from backend.app.db import Database
from backend.app.models.jobs import JobLogRecord, JobState
from backend.app.services.jobs import JobService


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class InitialAgentLaunchError(RuntimeError):
    """An initial Agent orchestration run cannot be admitted safely."""


class AgentInitialDispatchRecord(AgentJobBindingBase):
    """Durable admission record for one initial AgentRun."""

    __tablename__ = "agent_initial_dispatches"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    evidence_refs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    outcome_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


@dataclass(frozen=True, slots=True)
class InitialAgentLaunch:
    job_id: str
    run_id: str
    evidence_refs: tuple[str, ...]
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class InitialDispatchView:
    run_id: str
    job_id: str
    status: str
    attempts: int
    outcome_state: str | None


class AgentInitialLaunchCoordinator:
    """Atomically create Job + AgentRun + evidence seed + dispatch."""

    def __init__(
        self,
        database: Database,
        *,
        job_id_factory: Callable[[], str] | None = None,
        run_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.database = database
        self.jobs = JobService(database)
        self._job_id_factory = job_id_factory or (lambda: str(uuid4()))
        self._run_id_factory = run_id_factory or (lambda: str(uuid4()))
        AgentRunStore(database)
        AgentJobBindingRecord.__table__.create(bind=database.engine, checkfirst=True)
        AgentInitialDispatchRecord.__table__.create(bind=database.engine, checkfirst=True)

    def create_and_enqueue(
        self,
        *,
        goal: str,
        evidence_refs: list[str],
        evidence_summaries: list[dict[str, object]],
        budget: RunBudget,
        model_name: str | None = None,
        prompt_version: str | None = None,
        shutdown_margin_seconds: int = DEFAULT_SHUTDOWN_MARGIN_SECONDS,
    ) -> InitialAgentLaunch:
        normalized_goal = goal.strip()
        if not normalized_goal:
            raise InitialAgentLaunchError("Agent goal must not be blank.")
        refs = tuple(dict.fromkeys(evidence_refs))
        if not refs or len(refs) != len(evidence_refs):
            raise InitialAgentLaunchError("Evidence scope must be non-empty and unique.")
        summary_ids = [item.get("evidence_id") for item in evidence_summaries]
        if summary_ids != list(refs):
            raise InitialAgentLaunchError(
                "Evidence summaries must exactly match the ordered durable evidence scope."
            )
        if shutdown_margin_seconds < 1:
            raise InitialAgentLaunchError("shutdown_margin_seconds must be positive.")

        now = _utcnow()
        lease_seconds = ceil(budget.max_wall_time_seconds + shutdown_margin_seconds)
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        job_id = self._job_id_factory()
        run_id = self._run_id_factory()

        # JobService remains the Job lifecycle authority. Its transaction-aware
        # admission method lets this coordinator commit Job + AgentRun + dispatch
        # together without an orphan/fake-running crash window.
        with self.database.sessions.begin() as session:
            self.jobs.create_running_in_session(
                session,
                job_id=job_id,
                job_type=AGENT_ORCHESTRATION_JOB_TYPE,
                input_data={
                    "launch_kind": "grounded_analysis_orchestration_v1",
                    "evidence_refs": list(refs),
                },
                current_stage="agent_orchestration",
                lease_expires_at=lease_expires_at,
                now=now,
            )
            session.add(
                AgentRunRecord(
                    id=run_id,
                    goal=normalized_goal,
                    state=AgentRunState.running.value,
                    model_name=model_name,
                    prompt_version=prompt_version,
                    budget_json=budget.model_dump(mode="json"),
                    step_count=1,
                    model_calls=0,
                    input_tokens=0,
                    output_tokens=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                AgentStepRecord(
                    run_id=run_id,
                    step_index=1,
                    kind="checkpoint",
                    tool_name=None,
                    tool_call_id=None,
                    input_json=None,
                    output_json={
                        "scope": "operator_selected_evidence",
                        "evidence_count": len(refs),
                        "evidence_summaries": evidence_summaries,
                    },
                    evidence_refs_json=list(refs),
                    status="succeeded",
                    error_category=None,
                    error_detail=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                AgentJobBindingRecord(
                    run_id=run_id,
                    job_id=job_id,
                    created_at=now,
                )
            )
            session.add(
                AgentInitialDispatchRecord(
                    run_id=run_id,
                    job_id=job_id,
                    evidence_refs_json=list(refs),
                    status="queued",
                    attempts=0,
                    lease_expires_at=None,
                    outcome_state=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                JobLogRecord(
                    job_id=job_id,
                    level="info",
                    message=f"Initial Agent run {run_id} admitted with {len(refs)} evidence refs.",
                    created_at=now,
                )
            )

        return InitialAgentLaunch(
            job_id=job_id,
            run_id=run_id,
            evidence_refs=refs,
            lease_expires_at=lease_expires_at,
        )


class AgentInitialExecutor:
    """Single-process executor for durable initial Agent dispatches."""

    def __init__(
        self,
        database: Database,
        *,
        runtime_factory: Callable[[], JobBoundAgentRuntime],
        launch_enabled: bool = True,
        poll_seconds: float = 0.1,
    ) -> None:
        self.database = database
        self.runtime_factory = runtime_factory
        self.launch_enabled = bool(launch_enabled)
        self.poll_seconds = max(0.01, min(float(poll_seconds), 1.0))
        self.coordinator = AgentInitialLaunchCoordinator(database)
        self.store = AgentRunStore(database)
        self.jobs = JobService(database)
        self.guard = ActiveRunJobAuthorityGuard(database)
        self.wait_projector = JobHumanWaitProjector(database)
        self.terminal_projector = JobTerminalProjector(database)
        self._lock = RLock()
        self._stop = Event()
        self._wake = Event()
        self._idle = Event()
        self._idle.set()
        self._stopped = Event()
        self._thread: Thread | None = None
        self._accepting = True

    @property
    def accepting(self) -> bool:
        with self._lock:
            return self._accepting and self.launch_enabled

    @property
    def is_alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if not self._accepting or self._thread is not None:
                return
            self._recover_previous_process_dispatches()
            if not self.launch_enabled and not self._has_recoverable_dispatch():
                self._idle.set()
                return
            self._idle.clear()
            self._thread = Thread(
                target=self._run,
                name="agent-initial-executor",
                daemon=True,
            )
            self._thread.start()

    def close(self) -> bool:
        with self._lock:
            self._accepting = False
            self._stop.set()
            self._wake.set()
            thread = self._thread
        if thread is None:
            self._stopped.set()
            return True
        thread.join()
        return not thread.is_alive()

    def wait_idle(self, timeout: float) -> bool:
        return self._idle.wait(max(0.0, timeout))

    def launch(
        self,
        *,
        goal: str,
        evidence_refs: list[str],
        evidence_summaries: list[dict[str, object]],
        budget: RunBudget,
    ) -> InitialAgentLaunch:
        with self._lock:
            if not self._accepting or not self.launch_enabled:
                raise InitialAgentLaunchError(
                    "Automatic initial Agent execution is not configured."
                )
            # Build once before authority changes so unavailable production
            # configuration cannot create a running Job with no viable runtime.
            self.runtime_factory()
            self.start()
            launch = self.coordinator.create_and_enqueue(
                goal=goal,
                evidence_refs=evidence_refs,
                evidence_summaries=evidence_summaries,
                budget=budget,
            )
            self._idle.clear()
            self._wake.set()
            return launch

    def dispatch_view(self, run_id: str) -> InitialDispatchView:
        with self.database.sessions() as session:
            record = session.get(AgentInitialDispatchRecord, run_id)
            if record is None:
                raise KeyError(f"Initial Agent dispatch not found: {run_id}")
            return InitialDispatchView(
                run_id=record.run_id,
                job_id=record.job_id,
                status=record.status,
                attempts=record.attempts,
                outcome_state=record.outcome_state,
            )

    def _recover_previous_process_dispatches(self) -> None:
        now = _utcnow()
        with self.database.sessions.begin() as session:
            session.execute(
                update(AgentInitialDispatchRecord)
                .where(AgentInitialDispatchRecord.status == "running")
                .values(status="queued", lease_expires_at=None, updated_at=now)
            )

    def _has_recoverable_dispatch(self) -> bool:
        with self.database.sessions() as session:
            return (
                session.scalar(
                    select(AgentInitialDispatchRecord.run_id)
                    .where(AgentInitialDispatchRecord.status == "queued")
                    .limit(1)
                )
                is not None
            )

    def _claim_next(self) -> str | None:
        with self.database.sessions() as session:
            candidates = list(
                session.scalars(
                    select(AgentInitialDispatchRecord)
                    .where(AgentInitialDispatchRecord.status == "queued")
                    .order_by(
                        AgentInitialDispatchRecord.created_at,
                        AgentInitialDispatchRecord.run_id,
                    )
                )
            )
            for record in candidates:
                session.expunge(record)
        for record in candidates:
            run_id = record.run_id
            # Unlike an approved continuation, an initial run has no exact
            # durable action identity that can be safely replayed. If a prior
            # process already started this dispatch, restart must fail closed to
            # human review rather than re-entering the model/tool loop.
            if record.attempts > 0:
                self._complete_without_execution(run_id)
                continue
            try:
                job_id = self.guard.require_run_running(run_id)
                job = self.jobs.get(job_id)
            except Exception:
                self._complete_without_execution(run_id)
                continue
            if job.state is not JobState.running or job.lease_expires_at is None:
                self._complete_without_execution(run_id)
                continue
            now = _utcnow()
            with self.database.sessions.begin() as session:
                changed = session.execute(
                    update(AgentInitialDispatchRecord)
                    .where(
                        AgentInitialDispatchRecord.run_id == run_id,
                        AgentInitialDispatchRecord.status == "queued",
                    )
                    .values(
                        status="running",
                        attempts=AgentInitialDispatchRecord.attempts + 1,
                        lease_expires_at=job.lease_expires_at,
                        updated_at=now,
                    )
                )
                if changed.rowcount == 1:
                    return run_id
        return None

    def _reconcile_run_projection(self, run_id: str):
        run = self.store.get_run(run_id)
        if run.state == AgentRunState.running.value:
            run = self.store.recover_interrupted(run_id)
        if run.state == AgentRunState.needs_human.value:
            try:
                self.wait_projector.project_wait(run_id)
            except Exception:
                pass
        elif run.state in {AgentRunState.succeeded.value, AgentRunState.failed.value}:
            try:
                self.terminal_projector.reconcile_terminal_after_restart(run_id)
            except Exception:
                pass
        return self.store.get_run(run_id)

    def _complete_without_execution(self, run_id: str) -> None:
        run = self._reconcile_run_projection(run_id)
        self._mark_completed(run_id, run.state)

    def _mark_completed(self, run_id: str, outcome_state: str) -> None:
        now = _utcnow()
        with self.database.sessions.begin() as session:
            session.execute(
                update(AgentInitialDispatchRecord)
                .where(AgentInitialDispatchRecord.run_id == run_id)
                .values(
                    status="completed",
                    lease_expires_at=None,
                    outcome_state=outcome_state,
                    updated_at=now,
                )
            )

    def _recover_executor_error(self, run_id: str) -> None:
        try:
            run = self._reconcile_run_projection(run_id)
            self._mark_completed(run_id, run.state)
        except Exception:
            return

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                run_id = self._claim_next()
                if run_id is None:
                    self._idle.set()
                    self._wake.wait(self.poll_seconds)
                    self._wake.clear()
                    continue
                self._idle.clear()
                try:
                    runtime = self.runtime_factory()
                    outcome = runtime.run_bound(run_id)
                    self._mark_completed(run_id, outcome.state.value)
                except Exception:
                    self._recover_executor_error(run_id)
        finally:
            self._idle.set()
            self._stopped.set()
