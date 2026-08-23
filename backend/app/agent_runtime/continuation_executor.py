"""App-owned executor for durable approved Agent continuations.

Approval authority and execution admission are separate durable facts:

* ``JobContinuationCoordinator`` atomically approves the HumanAction, re-claims
  the Job, creates the child AgentRun, and inserts a queued dispatch row.
* this executor consumes only those durable dispatch rows and drives the already
  bound run through ``JobBoundAgentRuntime.run_approved_continuation``.

The executor owns no Android/XHS/browser physical lifecycle.  Any such work must
remain behind existing durable domain Job/worker services.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Event, RLock, Thread
from typing import Callable

from sqlalchemy import select, update

from backend.app.agent_runtime.job_bound_runtime import (
    ActiveRunJobAuthorityGuard,
    JobBoundAgentRuntime,
)
from backend.app.agent_runtime.job_binding import AgentJobBindingRecord
from backend.app.agent_runtime.job_continuation import (
    AgentContinuationDispatchRecord,
    ApprovedContinuation,
    ContinuationApprovalError,
    JobContinuationCoordinator,
)
from backend.app.agent_runtime.job_terminal_projection import JobTerminalProjector
from backend.app.agent_runtime.job_wait_projection import JobHumanWaitProjector
from backend.app.agent_runtime.persistence import (
    AgentRunRecord,
    AgentRunStore,
    AgentStepRecord,
    HumanActionRecord,
)
from backend.app.agent_runtime.tools import ToolInputError, ToolUnavailableError
from backend.app.agent_runtime.types import AgentRunState, NextAction
from backend.app.db import Database
from backend.app.models.jobs import JobState
from backend.app.services.jobs import JobService


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ContinuationExecutorClosed(RuntimeError):
    """No new approval may be admitted after application shutdown starts."""


@dataclass(frozen=True, slots=True)
class ContinuationDispatchView:
    run_id: str
    status: str
    attempts: int
    outcome_state: str | None


class AgentContinuationExecutor:
    """Single-process local executor backed by durable dispatch rows.

    The local workbench has one FastAPI process.  At startup, any dispatch left
    ``running`` by the previous process is returned to ``queued``.  Re-driving
    the child run is safe because the canonical continuation runtime inspects
    durable Tool steps and never replays an uncertain or already-committed Tool
    result.
    """

    def __init__(
        self,
        database: Database,
        *,
        runtime_factory: Callable[[], JobBoundAgentRuntime],
        approval_enabled: bool = True,
        poll_seconds: float = 0.1,
    ) -> None:
        self.database = database
        self.runtime_factory = runtime_factory
        self.approval_enabled = bool(approval_enabled)
        self.poll_seconds = max(0.01, min(float(poll_seconds), 1.0))
        self.coordinator = JobContinuationCoordinator(database)
        self.store = AgentRunStore(database)
        self.jobs = JobService(database)
        self.guard = ActiveRunJobAuthorityGuard(database)
        self.wait_projector = JobHumanWaitProjector(database)
        self.terminal_projector = JobTerminalProjector(database)
        AgentContinuationDispatchRecord.__table__.create(
            bind=database.engine, checkfirst=True
        )
        self._lock = RLock()
        self._stop = Event()
        self._wake = Event()
        self._idle = Event()
        self._idle.set()
        self._stopped = Event()
        self._thread: Thread | None = None
        self._accepting = True
        self._active_run_id: str | None = None

    @property
    def is_alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def accepting(self) -> bool:
        with self._lock:
            return self._accepting and self.approval_enabled

    def start(self) -> None:
        with self._lock:
            if not self._accepting or self._thread is not None:
                return
            self._recover_previous_process_dispatches()
            # An unconfigured app still owns restart reconciliation, but it must
            # not create an idle SQLite polling thread when there is no durable
            # continuation to reconcile. This keeps unrelated workers isolated.
            if not self.approval_enabled and not self._has_recoverable_dispatch():
                self._idle.set()
                return
            # Startup recovery may have returned durable work to queued. Clear
            # idle before the thread starts so observers cannot race ahead of
            # the first durable claim.
            self._idle.clear()
            self._thread = Thread(
                target=self._run,
                name="agent-continuation-executor",
                daemon=True,
            )
            self._thread.start()

    def close(self) -> bool:
        """Close admission and wait for the bounded active run to reach a durable boundary."""

        with self._lock:
            self._accepting = False
            self._stop.set()
            self._wake.set()
            thread = self._thread
        if thread is None:
            self._stopped.set()
            return True
        # Production Agent tools/model calls are bounded. Waiting rather than
        # mutating Job authority underneath an in-flight model/tool avoids a
        # shutdown race that could otherwise create a stale side effect.
        thread.join()
        return not thread.is_alive()

    def wait_idle(self, timeout: float) -> bool:
        return self._idle.wait(max(0.0, timeout))

    def wait_stopped(self, timeout: float) -> bool:
        return self._stopped.wait(max(0.0, timeout))

    def approve(
        self,
        human_action_id: str,
        *,
        note: str | None = None,
    ) -> ApprovedContinuation:
        """Approve exactly one pending HumanAction and durably enqueue its child run."""

        with self._lock:
            if not self._accepting or not self.approval_enabled:
                raise ContinuationExecutorClosed(
                    "Continuation executor is not accepting new approvals."
                )
            self.start()
            with self.database.sessions() as session:
                human = session.get(HumanActionRecord, human_action_id)
                if human is None:
                    raise KeyError(f"HumanAction not found: {human_action_id}")
                source = session.get(AgentRunRecord, human.run_id)
                if (
                    human.status != "pending"
                    or source is None
                    or source.state != AgentRunState.needs_human.value
                    or source.error_category != "approval_required"
                ):
                    raise ContinuationApprovalError(
                        "Only a current pending approval_required HumanAction may continue."
                    )
                source_run_id = human.run_id
                try:
                    action = NextAction.model_validate(human.request_json["action"])
                except (KeyError, ValueError) as error:
                    raise ContinuationApprovalError(
                        "Pending HumanAction does not contain a valid persisted action."
                    ) from error

            # Validate the production executor surface before the coordinator
            # changes any durable authority. The canonical Runtime validates the
            # exact action again immediately before execution, closing dynamic
            # availability/argument races without creating an unsupported child.
            try:
                runtime = self.runtime_factory()
                if action.action != "tool" or action.tool_name is None:
                    raise ContinuationApprovalError(
                        "Only an exact persisted Tool action may be approved."
                    )
                tool = runtime.tools.resolve(action.tool_name)
                validated = runtime.tools.validate(tool, action.arguments)
                if action.tool_name == "analysis.run_grounded":
                    requested_ids = set(
                        validated.model_dump(mode="json").get("evidence_ids", [])
                    )
                    allowed_ids = self._job_history_evidence_refs(source_run_id)
                    if not requested_ids or not requested_ids.issubset(allowed_ids):
                        raise ContinuationApprovalError(
                            "Grounded analysis may use only evidence already present in the durable Job history."
                        )
            except ContinuationApprovalError:
                raise
            except (ToolUnavailableError, ToolInputError, ValueError) as error:
                raise ContinuationApprovalError(
                    "Approved Tool is unavailable or invalid for the configured Agent runtime."
                ) from error

            approved = self.coordinator.approve_and_create_continuation(
                source_run_id,
                human_action_id,
                note=note,
            )
            self._idle.clear()
            self._wake.set()
            return approved

    def _job_history_evidence_refs(self, source_run_id: str) -> set[str]:
        """Return the durable evidence ceiling for this exact Job history."""

        job_id = self.guard.require_current_binding(source_run_id)
        with self.database.sessions() as session:
            run_ids = list(
                session.scalars(
                    select(AgentJobBindingRecord.run_id).where(
                        AgentJobBindingRecord.job_id == job_id
                    )
                )
            )
            if not run_ids:
                return set()
            steps = list(
                session.scalars(
                    select(AgentStepRecord).where(AgentStepRecord.run_id.in_(run_ids))
                )
            )
        return {
            evidence_id
            for step in steps
            for evidence_id in list(step.evidence_refs_json or [])
        }

    def dispatch_view(self, run_id: str) -> ContinuationDispatchView:
        with self.database.sessions() as session:
            record = session.get(AgentContinuationDispatchRecord, run_id)
            if record is None:
                raise KeyError(f"Continuation dispatch not found: {run_id}")
            return ContinuationDispatchView(
                run_id=record.run_id,
                status=record.status,
                attempts=record.attempts,
                outcome_state=record.outcome_state,
            )

    def _recover_previous_process_dispatches(self) -> None:
        """Local single-process restart fence: durable running work becomes queued again."""

        now = _utcnow()
        with self.database.sessions.begin() as session:
            session.execute(
                update(AgentContinuationDispatchRecord)
                .where(AgentContinuationDispatchRecord.status == "running")
                .values(
                    status="queued",
                    lease_expires_at=None,
                    updated_at=now,
                )
            )

    def _has_recoverable_dispatch(self) -> bool:
        with self.database.sessions() as session:
            return (
                session.scalar(
                    select(AgentContinuationDispatchRecord.run_id)
                    .where(AgentContinuationDispatchRecord.status == "queued")
                    .limit(1)
                )
                is not None
            )

    def _claim_next(self) -> str | None:
        with self.database.sessions() as session:
            candidates = list(
                session.scalars(
                    select(AgentContinuationDispatchRecord.run_id)
                    .where(AgentContinuationDispatchRecord.status == "queued")
                    .order_by(
                        AgentContinuationDispatchRecord.created_at,
                        AgentContinuationDispatchRecord.run_id,
                    )
                )
            )

        for run_id in candidates:
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
                    update(AgentContinuationDispatchRecord)
                    .where(
                        AgentContinuationDispatchRecord.run_id == run_id,
                        AgentContinuationDispatchRecord.status == "queued",
                    )
                    .values(
                        status="running",
                        attempts=AgentContinuationDispatchRecord.attempts + 1,
                        lease_expires_at=job.lease_expires_at,
                        updated_at=now,
                    )
                )
                if changed.rowcount == 1:
                    return run_id
        return None

    def _complete_without_execution(self, run_id: str) -> None:
        """Close a stale dispatch without inventing Job/Run lifecycle authority."""

        run = self._reconcile_run_projection(run_id)
        self._mark_completed(run_id, outcome_state=run.state)

    def _reconcile_run_projection(self, run_id: str):
        """Close the Run->Job crash window from durable Run proof only."""

        run = self.store.get_run(run_id)
        if run.state == AgentRunState.running.value:
            # A queued continuation that lost Job authority must not remain a
            # misleading runnable trace. Canonical recovery is fail-closed and
            # will never replay a started Tool side effect.
            run = self.store.recover_interrupted(run_id)

        if run.state == AgentRunState.needs_human.value:
            try:
                self.wait_projector.project_wait(run_id)
            except Exception:
                # A newer/terminal Job authority may already have won. The
                # durable Run remains reviewable and must not be rewritten.
                pass
        elif run.state in {
            AgentRunState.succeeded.value,
            AgentRunState.failed.value,
        }:
            try:
                self.terminal_projector.reconcile_terminal_after_restart(run_id)
            except Exception:
                # Lease recovery, cancellation, or a newer binding may make
                # this terminal Run historical. Never overwrite that authority.
                pass
        return self.store.get_run(run_id)

    def _mark_completed(self, run_id: str, *, outcome_state: str) -> None:
        now = _utcnow()
        with self.database.sessions.begin() as session:
            session.execute(
                update(AgentContinuationDispatchRecord)
                .where(AgentContinuationDispatchRecord.run_id == run_id)
                .values(
                    status="completed",
                    lease_expires_at=None,
                    outcome_state=outcome_state,
                    updated_at=now,
                )
            )

    def _recover_executor_error(self, run_id: str) -> None:
        """Unexpected executor errors fail closed to a durable human-review boundary."""

        try:
            run = self._reconcile_run_projection(run_id)
            self._mark_completed(run_id, outcome_state=run.state)
        except Exception:
            # Preserve the running dispatch if even durable recovery cannot be
            # proven. Startup reconciliation can inspect it again; never forge a
            # terminal result in an error handler.
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
                with self._lock:
                    self._active_run_id = run_id
                try:
                    runtime = self.runtime_factory()
                    outcome = runtime.run_approved_continuation(run_id)
                    self._mark_completed(run_id, outcome_state=outcome.state.value)
                except Exception:
                    self._recover_executor_error(run_id)
                finally:
                    with self._lock:
                        self._active_run_id = None
        finally:
            self._idle.set()
            self._stopped.set()
