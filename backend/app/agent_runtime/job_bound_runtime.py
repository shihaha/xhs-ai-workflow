"""Job-bound integration adapter for the canonical Agent Runtime.

This staged adapter keeps the proven canonical ``AgentRuntime`` unchanged while
we verify Job authority at real model/tool execution boundaries.  It is not
exported from ``backend.app.agent_runtime`` and must not own physical workers.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from backend.app.agent_runtime.job_binding import (
    AgentJobBindingRecord,
    JobAuthorityError,
    JobAuthorityGuard,
)
from backend.app.agent_runtime.runtime import AgentRuntime
from backend.app.agent_runtime.types import AgentRunState, NextAction, RuntimeOutcome


class ActiveRunJobAuthorityGuard(JobAuthorityGuard):
    """Require both a runnable Job lease and the current Job claim's latest run.

    A Job may accumulate historical AgentRun bindings across human-wait /
    continuation claims.  Merely checking that the Job is currently ``running``
    is therefore insufficient: an older run must not regain authority after a
    newer continuation run has been created.
    """

    def require_run_running(self, run_id: str, *, now=None) -> str:
        job_id = super().require_run_running(run_id, now=now)
        with self.database.sessions() as session:
            latest_run_id = session.scalar(
                select(AgentJobBindingRecord.run_id)
                .where(AgentJobBindingRecord.job_id == job_id)
                .order_by(
                    AgentJobBindingRecord.created_at.desc(),
                    AgentJobBindingRecord.run_id.desc(),
                )
                .limit(1)
            )
        if latest_run_id != run_id:
            raise JobAuthorityError(
                f"Agent run {run_id} has been superseded by a newer continuation run; "
                "Agent work must stop."
            )
        return job_id


class _AuthorityCheckedModel:
    """Check Job authority immediately before and after one provider call."""

    def __init__(self, delegate: Any, guard: ActiveRunJobAuthorityGuard) -> None:
        self._delegate = delegate
        self._guard = guard

    def next_action(self, context):
        try:
            self._guard.require_run_running(context.run_id)
        except JobAuthorityError as exc:
            raise JobAuthorityError(f"before_model: {exc}") from exc

        turn = self._delegate.next_action(context)

        # A cancel/lease loss can happen while the provider is in flight.  Do
        # not apply a stale finish/tool decision after authority was revoked.
        try:
            self._guard.require_run_running(context.run_id)
        except JobAuthorityError as exc:
            raise JobAuthorityError(f"after_model: {exc}") from exc
        return turn


class JobBoundAgentRuntime(AgentRuntime):
    """Canonical runtime plus fail-closed Job authority boundaries.

    This class is intentionally an integration spike and is not package-root
    exported.  New bound runs must be created by ``AgentJobCoordinator`` first;
    this adapter only drives an already-bound, currently-authoritative run.
    """

    def __init__(
        self,
        *args: Any,
        authority_guard: ActiveRunJobAuthorityGuard,
        **kwargs: Any,
    ) -> None:
        self.authority_guard = authority_guard
        super().__init__(*args, **kwargs)
        self.model = _AuthorityCheckedModel(self.model, authority_guard)

    def start(self, goal: str) -> RuntimeOutcome:
        raise RuntimeError(
            "Job-bound Agent runs must be created atomically by AgentJobCoordinator; "
            "use run_bound(run_id) after claim_and_create_run()."
        )

    def resume(self, run_id: str, *, approved: bool, note: str | None = None) -> RuntimeOutcome:
        raise RuntimeError(
            "Job-bound continuation must re-claim the needs_human Job and create a new AgentRun; "
            "resuming an older bound run is forbidden."
        )

    def resume_interrupted(
        self,
        run_id: str,
        *,
        acknowledge_uncertain: bool = False,
    ) -> RuntimeOutcome:
        raise RuntimeError(
            "Job-bound interrupted work requires explicit Job recovery/re-claim and a new continuation run."
        )

    def run_bound(self, run_id: str) -> RuntimeOutcome:
        run = self.store.get_run(run_id)
        if run.state != AgentRunState.running.value:
            raise ValueError("only a running bound AgentRun can be driven")
        return self._drive(run_id)

    def _execute_tool(
        self,
        run_id: str,
        action: NextAction,
        tool: Any,
        validated_json: dict[str, Any],
    ) -> RuntimeOutcome | None:
        try:
            self.authority_guard.require_run_running(run_id)
        except JobAuthorityError as exc:
            return self._fail(run_id, "job_authority_lost", f"before_tool: {exc}")
        return super()._execute_tool(run_id, action, tool, validated_json)

    def _fail(self, run_id: str, category: str, detail: str) -> RuntimeOutcome:
        # The canonical Stage 1 loop intentionally catches provider exceptions
        # generically.  The guarded model raises JobAuthorityError to stop before
        # calling/applying the model; normalize that one known control failure.
        if category == "model_error" and detail.startswith("JobAuthorityError: "):
            category = "job_authority_lost"
            detail = detail.removeprefix("JobAuthorityError: ")
        return super()._fail(run_id, category, detail)
