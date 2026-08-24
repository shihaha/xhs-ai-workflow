"""Apply accepted initial ChatGPT NextAction results without bypassing permissions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Event, RLock, Thread
from typing import Callable
from uuid import uuid4

from sqlalchemy import exists, func, select, update

from backend.app.agent_runtime.external_result_finalizer import (
    DeterministicExternalResultFinalizer,
    ExternalResultFinalizationError,
)
from backend.app.agent_runtime.job_binding import AgentJobBindingRecord
from backend.app.agent_runtime.job_bound_runtime import JobBoundAgentRuntime
from backend.app.agent_runtime.manual_chatgpt_handoff import (
    MANUAL_CHATGPT_RESULT_READY,
    ManualChatGPTHandoffRecord,
)
from backend.app.agent_runtime.orchestration_service import (
    INITIAL_CHATGPT_DECISION_REVISION,
)
from backend.app.agent_runtime.permissions import PermissionRequest
from backend.app.agent_runtime.persistence import (
    AgentCheckpointRecord,
    AgentRunRecord,
    AgentStepRecord,
    HumanActionRecord,
    PermissionDecisionRecord,
)
from backend.app.agent_runtime.types import AgentRunState, NextAction, PermissionDecision
from backend.app.db import Database
from backend.app.models.jobs import JobLogRecord, JobRecord, JobState


@dataclass(frozen=True, slots=True)
class InitialChatGPTDecisionApplyResult:
    handoff_id: str
    job_id: str
    source_run_id: str
    status: str
    human_action_id: str | None = None
    continuation_run_id: str | None = None


class InitialChatGPTDecisionError(RuntimeError):
    """An accepted initial ChatGPT action cannot be applied safely."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _latest_binding_predicates(job_id: str, run_id: str):
    table = AgentJobBindingRecord.__table__
    max_binding = table.alias("initial_chatgpt_max_binding")
    count_binding = table.alias("initial_chatgpt_count_binding")
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
    return latest_binding_count, source_is_latest


class AgentInitialChatGPTDecisionReconciler:
    """Turn one accepted initial ChatGPT result into finish or approval wait."""

    def __init__(
        self,
        database: Database,
        *,
        runtime_factory: Callable[[], JobBoundAgentRuntime],
    ) -> None:
        self.database = database
        self.runtime_factory = runtime_factory
        self.finalizer = DeterministicExternalResultFinalizer(database)

    def reconcile_once(self) -> list[InitialChatGPTDecisionApplyResult]:
        binding = AgentJobBindingRecord.__table__.alias("initial_chatgpt_source_binding")
        newer = AgentJobBindingRecord.__table__.alias("initial_chatgpt_newer_binding")
        same_time = AgentJobBindingRecord.__table__.alias("initial_chatgpt_same_time_binding")
        no_newer_binding = ~exists().where(
            newer.c.job_id == binding.c.job_id,
            newer.c.created_at > binding.c.created_at,
        )
        unique_at_latest = (
            select(func.count())
            .select_from(same_time)
            .where(
                same_time.c.job_id == binding.c.job_id,
                same_time.c.created_at == binding.c.created_at,
            )
            .scalar_subquery()
            == 1
        )
        with self.database.sessions() as session:
            handoff_ids = list(
                session.scalars(
                    select(ManualChatGPTHandoffRecord.id)
                    .join(
                        AgentRunRecord,
                        AgentRunRecord.id == ManualChatGPTHandoffRecord.source_run_id,
                    )
                    .join(
                        binding,
                        binding.c.run_id == ManualChatGPTHandoffRecord.source_run_id,
                    )
                    .join(JobRecord, JobRecord.id == binding.c.job_id)
                    .where(
                        ManualChatGPTHandoffRecord.status == "accepted",
                        ManualChatGPTHandoffRecord.stage_revision
                        == INITIAL_CHATGPT_DECISION_REVISION,
                        AgentRunRecord.state == AgentRunState.needs_human.value,
                        AgentRunRecord.error_category == MANUAL_CHATGPT_RESULT_READY,
                        JobRecord.type == "agent_orchestration",
                        JobRecord.state == JobState.needs_human.value,
                        JobRecord.current_stage == MANUAL_CHATGPT_RESULT_READY,
                        JobRecord.error_category == MANUAL_CHATGPT_RESULT_READY,
                        JobRecord.lease_expires_at.is_(None),
                        no_newer_binding,
                        unique_at_latest,
                    )
                    .order_by(
                        ManualChatGPTHandoffRecord.accepted_at,
                        ManualChatGPTHandoffRecord.id,
                    )
                )
            )

        results: list[InitialChatGPTDecisionApplyResult] = []
        for handoff_id in handoff_ids:
            try:
                applied = self._apply(handoff_id)
            except (ValueError, InitialChatGPTDecisionError, ExternalResultFinalizationError):
                # Keep the accepted result durable and fail closed. Nothing here
                # repairs authority or executes a physical side effect on error.
                continue
            if applied is not None:
                results.append(applied)
        return results

    def _apply(self, handoff_id: str) -> InitialChatGPTDecisionApplyResult | None:
        with self.database.sessions() as session:
            handoff = session.get(ManualChatGPTHandoffRecord, handoff_id)
            if (
                handoff is None
                or handoff.status != "accepted"
                or handoff.stage_revision != INITIAL_CHATGPT_DECISION_REVISION
                or not isinstance(handoff.result_json, dict)
                or not isinstance(handoff.task_json, dict)
            ):
                return None
            source = session.get(AgentRunRecord, handoff.source_run_id)
            binding = session.get(AgentJobBindingRecord, handoff.source_run_id)
            if source is None or binding is None:
                return None
            job = session.get(JobRecord, binding.job_id)
            if job is None:
                return None
            if (
                source.state != AgentRunState.needs_human.value
                or source.error_category != MANUAL_CHATGPT_RESULT_READY
                or JobState(job.state) is not JobState.needs_human
                or job.current_stage != MANUAL_CHATGPT_RESULT_READY
                or job.error_category != MANUAL_CHATGPT_RESULT_READY
                or job.lease_expires_at is not None
                or not self._is_unique_latest_binding(
                    session,
                    job_id=job.id,
                    run_id=source.id,
                )
            ):
                return None
            task = dict(handoff.task_json)
            if task.get("kind") != "initial_agent_next_action":
                return None
            supported = task.get("supported_tool_names")
            if not isinstance(supported, list) or any(not isinstance(item, str) for item in supported):
                raise InitialChatGPTDecisionError("Initial handoff has invalid supported tool list.")
            action = NextAction.model_validate(handoff.result_json)
            if len(action.tool_call_id) > 64:
                raise InitialChatGPTDecisionError("ChatGPT tool_call_id exceeds durable bounds.")
            job_id = job.id
            source_run_id = source.id
            external_ref = f"external:chatgpt:{handoff.id}"

        if action.action == "finish":
            assert action.final_output is not None
            continuation_run_id = self.finalizer.finalize(
                source_run_id=source_run_id,
                continuation_key=f"initial-chatgpt-{handoff_id}",
                final_output=action.final_output,
                evidence_refs=[external_ref],
            )
            return InitialChatGPTDecisionApplyResult(
                handoff_id=handoff_id,
                job_id=job_id,
                source_run_id=source_run_id,
                status="finished",
                continuation_run_id=continuation_run_id,
            )

        assert action.tool_name is not None
        if action.tool_name not in supported:
            raise InitialChatGPTDecisionError(
                f"ChatGPT proposed unsupported initial tool {action.tool_name!r}."
            )
        runtime = self.runtime_factory()
        tool = runtime.tools.resolve(action.tool_name)
        validated = runtime.tools.validate(tool, action.arguments)
        if tool.pre_approval_validate is not None:
            # This is side-effect-free by ToolSpec contract and binds approval to
            # the same exact target that the physical handler validates again.
            tool.pre_approval_validate(validated)
        validated_json = validated.model_dump(mode="json")
        permission = runtime.permissions.decide(
            PermissionRequest(
                run_id=source_run_id,
                tool_call_id=action.tool_call_id,
                tool=tool,
                arguments=validated_json,
            )
        )
        if permission.decision is not PermissionDecision.ask:
            raise InitialChatGPTDecisionError(
                "Initial ChatGPT tool proposal must pass through an explicit approval boundary."
            )
        return self._create_approval_wait(
            handoff_id=handoff_id,
            source_run_id=source_run_id,
            job_id=job_id,
            action=action,
            validated_arguments=validated_json,
            permission_reason=permission.reason,
        )

    def _create_approval_wait(
        self,
        *,
        handoff_id: str,
        source_run_id: str,
        job_id: str,
        action: NextAction,
        validated_arguments: dict,
        permission_reason: str,
    ) -> InitialChatGPTDecisionApplyResult:
        now = _utcnow()
        human_action_id = str(uuid4())
        latest_count, source_is_latest = _latest_binding_predicates(job_id, source_run_id)
        with self.database.sessions.begin() as session:
            source = session.get(AgentRunRecord, source_run_id)
            job = session.get(JobRecord, job_id)
            if source is None or job is None:
                raise InitialChatGPTDecisionError("Initial ChatGPT source authority disappeared.")
            if (
                source.state != AgentRunState.needs_human.value
                or source.error_category != MANUAL_CHATGPT_RESULT_READY
                or JobState(job.state) is not JobState.needs_human
                or job.current_stage != MANUAL_CHATGPT_RESULT_READY
                or job.error_category != MANUAL_CHATGPT_RESULT_READY
                or job.lease_expires_at is not None
            ):
                raise InitialChatGPTDecisionError("Initial ChatGPT result is no longer current.")
            pending = session.scalar(
                select(HumanActionRecord.id)
                .where(
                    HumanActionRecord.run_id == source_run_id,
                    HumanActionRecord.status == "pending",
                )
                .limit(1)
            )
            if pending is not None:
                raise InitialChatGPTDecisionError("Source run already has a pending HumanAction.")

            changed = session.execute(
                update(JobRecord)
                .where(
                    JobRecord.id == job_id,
                    JobRecord.state == JobState.needs_human.value,
                    JobRecord.current_stage == MANUAL_CHATGPT_RESULT_READY,
                    JobRecord.error_category == MANUAL_CHATGPT_RESULT_READY,
                    JobRecord.lease_expires_at.is_(None),
                    latest_count == 1,
                    source_is_latest,
                )
                .values(
                    current_stage="approval_required",
                    error_category="approval_required",
                    updated_at=now,
                )
            )
            if changed.rowcount != 1:
                raise InitialChatGPTDecisionError(
                    "Job authority changed while creating ChatGPT-proposed approval wait."
                )

            next_index = source.step_count + 1
            source.step_count = next_index
            source.error_category = "approval_required"
            source.error_detail = permission_reason
            source.updated_at = now
            session.add(
                PermissionDecisionRecord(
                    run_id=source_run_id,
                    tool_call_id=action.tool_call_id,
                    tool_name=action.tool_name,
                    decision=PermissionDecision.ask.value,
                    reason=permission_reason,
                    created_at=now,
                )
            )
            session.add(
                AgentStepRecord(
                    run_id=source_run_id,
                    step_index=next_index,
                    kind="permission",
                    tool_name=action.tool_name,
                    tool_call_id=action.tool_call_id,
                    input_json={"arguments": validated_arguments},
                    output_json={
                        "decision": PermissionDecision.ask.value,
                        "reason": permission_reason,
                        "proposed_by": "chatgpt_handoff",
                        "handoff_id": handoff_id,
                    },
                    evidence_refs_json=[],
                    status=PermissionDecision.ask.value,
                    error_category=None,
                    error_detail=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                HumanActionRecord(
                    id=human_action_id,
                    run_id=source_run_id,
                    tool_call_id=action.tool_call_id,
                    tool_name=action.tool_name,
                    status="pending",
                    request_json={
                        "action": action.model_copy(update={"arguments": validated_arguments}).model_dump(
                            mode="json"
                        ),
                        "reason": permission_reason,
                        "proposed_by": "chatgpt_handoff",
                        "source_handoff_id": handoff_id,
                    },
                    resolution_json=None,
                    created_at=now,
                    resolved_at=None,
                )
            )
            session.add(
                AgentCheckpointRecord(
                    run_id=source_run_id,
                    step_index=next_index,
                    state_json={
                        "state": AgentRunState.needs_human.value,
                        "human_action_id": human_action_id,
                        "tool_call_id": action.tool_call_id,
                        "source_handoff_id": handoff_id,
                    },
                    created_at=now,
                )
            )
            session.add(
                JobLogRecord(
                    job_id=job_id,
                    level="info",
                    message=(
                        f"Accepted initial ChatGPT handoff {handoff_id} proposed "
                        f"{action.tool_name}; explicit HumanAction {human_action_id} is required."
                    ),
                    created_at=now,
                )
            )

        return InitialChatGPTDecisionApplyResult(
            handoff_id=handoff_id,
            job_id=job_id,
            source_run_id=source_run_id,
            status="approval_required",
            human_action_id=human_action_id,
        )

    @staticmethod
    def _is_unique_latest_binding(session, *, job_id: str, run_id: str) -> bool:
        latest_at = session.scalar(
            select(func.max(AgentJobBindingRecord.created_at)).where(
                AgentJobBindingRecord.job_id == job_id
            )
        )
        if latest_at is None:
            return False
        latest_ids = list(
            session.scalars(
                select(AgentJobBindingRecord.run_id).where(
                    AgentJobBindingRecord.job_id == job_id,
                    AgentJobBindingRecord.created_at == latest_at,
                )
            )
        )
        return latest_ids == [run_id]


class AgentInitialChatGPTDecisionWorker:
    def __init__(
        self,
        reconciler: AgentInitialChatGPTDecisionReconciler,
        *,
        poll_seconds: float = 0.5,
    ) -> None:
        self.reconciler = reconciler
        self.poll_seconds = max(0.1, min(float(poll_seconds), 5.0))
        self._stop = Event()
        self._lock = RLock()
        self._thread: Thread | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._thread = Thread(
                target=self._run,
                name="agent-initial-chatgpt-decision",
                daemon=True,
            )
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(2.0, self.poll_seconds * 4))

    def _run(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.reconciler.reconcile_once()
            except Exception:
                continue
