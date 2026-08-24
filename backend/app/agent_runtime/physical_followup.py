"""Durable reconciliation from physical child Jobs into ChatGPT handoffs.

The physical worker remains owned by its domain service.  This module only reads
its already-persisted Job/Artifact result and, when the originating AgentRun is
still the unique current wait, converts that wait into a bounded ChatGPT handoff.
It never replays Android/XHS/browser work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Event, RLock, Thread
from typing import Any

from sqlalchemy import func, select

from backend.app.agent_runtime.external_result_finalizer import (
    DeterministicExternalResultFinalizer,
    ExternalResultFinalizationError,
)
from backend.app.agent_runtime.job_binding import AgentJobBindingRecord
from backend.app.agent_runtime.job_bound_runtime import ActiveRunJobAuthorityGuard
from backend.app.agent_runtime.manual_chatgpt_handoff import (
    HandoffResultContract,
    ManualChatGPTHandoffError,
    ManualChatGPTHandoffService,
)
from backend.app.agent_runtime.persistence import AgentRunRecord, AgentRunStore, AgentStepRecord
from backend.app.db import Database
from backend.app.models.jobs import JobArtifactRecord, JobLogRecord, JobRecord, JobState
from backend.app.services.jobs import JobService


PHYSICAL_JOB_PENDING = "physical_job_pending"
PHYSICAL_RESULT_READY = "physical_result_ready"
SHOP_SCOPE_HANDOFF_REVISION = "shop-scope-chatgpt-v1"
_SHOP_PREFLIGHT_TOOL = "shop.preflight"
_SCOPE_ARTIFACT_KIND = "shop_scope_gate_result"
_ALLOWED_SCOPE_RESULTS = {"in_scope", "out_of_scope_physical", "needs_human"}


@dataclass(frozen=True, slots=True)
class PhysicalFollowupResult:
    source_run_id: str
    child_job_id: str
    status: str
    handoff_id: str | None = None


class AgentPhysicalFollowupReconciler:
    """Convert one proven physical-result wait into an authoritative handoff."""

    def __init__(self, database: Database, *, runtime_dir) -> None:
        self.database = database
        self.jobs = JobService(database, runtime_dir=runtime_dir)
        self.guard = ActiveRunJobAuthorityGuard(database)
        self.store = AgentRunStore(database)
        self.handoffs = ManualChatGPTHandoffService(
            database,
            runtime_dir=runtime_dir,
        )
        self.finalizer = DeterministicExternalResultFinalizer(database)

    def reconcile_once(self) -> list[PhysicalFollowupResult]:
        with self.database.sessions() as session:
            run_ids = list(
                session.scalars(
                    select(AgentRunRecord.id)
                    .where(
                        AgentRunRecord.state == "needs_human",
                        AgentRunRecord.error_category.in_(
                            [PHYSICAL_JOB_PENDING, PHYSICAL_RESULT_READY]
                        ),
                    )
                    .order_by(AgentRunRecord.created_at, AgentRunRecord.id)
                )
            )

        results: list[PhysicalFollowupResult] = []
        for run_id in run_ids:
            result = self._reconcile_run(run_id)
            if result is not None:
                results.append(result)
        return results

    def _reconcile_run(self, run_id: str) -> PhysicalFollowupResult | None:
        run = self.store.get_run(run_id)
        if run.error_category == PHYSICAL_RESULT_READY:
            return self._finalize_deterministic_ready(run_id)
        try:
            parent_job_id = self.guard.require_current_binding(run_id)
        except Exception:
            return None

        with self.database.sessions() as session:
            parent = session.get(JobRecord, parent_job_id)
            run = session.get(AgentRunRecord, run_id)
            if (
                parent is None
                or run is None
                or JobState(parent.state) is not JobState.needs_human
                or parent.lease_expires_at is not None
                or parent.error_category != PHYSICAL_JOB_PENDING
                or run.state != "needs_human"
                or run.error_category != PHYSICAL_JOB_PENDING
            ):
                return None
            steps = list(
                session.scalars(
                    select(AgentStepRecord)
                    .where(
                        AgentStepRecord.run_id == run_id,
                        AgentStepRecord.kind == "tool",
                        AgentStepRecord.tool_name == _SHOP_PREFLIGHT_TOOL,
                        AgentStepRecord.status == "needs_human",
                        AgentStepRecord.error_category == PHYSICAL_JOB_PENDING,
                    )
                    .order_by(AgentStepRecord.step_index.desc())
                )
            )
            if not steps:
                return None
            step = steps[0]
            child_job_id = self._child_job_id(step.output_json)
            if child_job_id is None:
                return None
            child = session.get(JobRecord, child_job_id)
            if child is None:
                return None
            origin = child.input_data.get("_agent_origin")
            if not self._origin_matches(origin, run_id=run_id, tool_call_id=step.tool_call_id):
                return None
            child_state = JobState(child.state)
            if child_state in {JobState.queued, JobState.running}:
                return PhysicalFollowupResult(
                    source_run_id=run_id,
                    child_job_id=child_job_id,
                    status="waiting",
                )
            artifacts = list(
                session.scalars(
                    select(JobArtifactRecord)
                    .where(JobArtifactRecord.job_id == child_job_id)
                    .order_by(JobArtifactRecord.id)
                )
            )

        scope = self._scope_result(artifacts)
        if scope is None:
            # Selector/device/collection failures are physical recovery problems,
            # not reasoning tasks.  Keep the existing needs_human boundary.
            return PhysicalFollowupResult(
                source_run_id=run_id,
                child_job_id=child_job_id,
                status="physical_result_not_reasonable",
            )

        classification = scope.get("classification")
        if classification not in _ALLOWED_SCOPE_RESULTS:
            return PhysicalFollowupResult(
                source_run_id=run_id,
                child_job_id=child_job_id,
                status="invalid_scope_result",
            )

        context_refs = [f"artifact:{artifact.id}" for artifact in artifacts]
        if classification != "needs_human":
            if not self._mark_deterministic_result_ready(
                run_id=run_id,
                parent_job_id=parent_job_id,
                child_job_id=child_job_id,
                classification=classification,
                reason=str(scope.get("reason") or "deterministic_scope_result"),
                evidence_refs=context_refs,
            ):
                return None
            return self._finalize_deterministic_ready(run_id)

        task = self._handoff_task(
            child_job_id=child_job_id,
            child_state=child_state,
            scope=scope,
        )
        contract = HandoffResultContract(
            schema_version="shop-scope-chatgpt-v1",
            required_fields=("classification", "reason", "evidence_refs"),
            allow_extra_fields=False,
        )
        try:
            handoff = self.handoffs.request_followup_handoff(
                run_id,
                expected_wait_category=PHYSICAL_JOB_PENDING,
                task=task,
                context_refs=context_refs,
                stage_revision=SHOP_SCOPE_HANDOFF_REVISION,
                result_contract=contract,
            )
        except ManualChatGPTHandoffError:
            # Another process/restart may have won the exact authority race.
            return None
        return PhysicalFollowupResult(
            source_run_id=run_id,
            child_job_id=child_job_id,
            status="chatgpt_handoff_created",
            handoff_id=handoff.handoff_id,
        )

    def _finalize_deterministic_ready(
        self, run_id: str
    ) -> PhysicalFollowupResult | None:
        try:
            parent_job_id = self.guard.require_current_binding(run_id)
        except Exception:
            return None
        with self.database.sessions() as session:
            run = session.get(AgentRunRecord, run_id)
            parent = session.get(JobRecord, parent_job_id)
            if (
                run is None
                or parent is None
                or run.state != "needs_human"
                or run.error_category != PHYSICAL_RESULT_READY
                or JobState(parent.state) is not JobState.needs_human
                or parent.error_category != PHYSICAL_RESULT_READY
                or parent.lease_expires_at is not None
            ):
                return None
            checkpoint = session.scalar(
                select(AgentStepRecord)
                .where(
                    AgentStepRecord.run_id == run_id,
                    AgentStepRecord.kind == "checkpoint",
                    AgentStepRecord.tool_name == _SHOP_PREFLIGHT_TOOL,
                    AgentStepRecord.status == "succeeded",
                )
                .order_by(AgentStepRecord.step_index.desc())
                .limit(1)
            )
            if checkpoint is None or not isinstance(checkpoint.output_json, dict):
                return None
            child_job_id = checkpoint.output_json.get("physical_job_id")
            classification = checkpoint.output_json.get("classification")
            reason = checkpoint.output_json.get("reason")
            if (
                not isinstance(child_job_id, str)
                or classification not in {"in_scope", "out_of_scope_physical"}
                or not isinstance(reason, str)
                or not reason
            ):
                return None
            evidence_refs = list(checkpoint.evidence_refs_json or [])
        try:
            continuation_run_id = self.finalizer.finalize(
                source_run_id=run_id,
                continuation_key=f"physical-scope-{child_job_id}",
                final_output={
                    "kind": "shop_scope_deterministic_applied",
                    "physical_job_id": child_job_id,
                    "classification": classification,
                    "reason": reason,
                    "decision_source": "rule",
                    "physical_replay": False,
                },
                evidence_refs=evidence_refs,
            )
        except ExternalResultFinalizationError:
            return None
        return PhysicalFollowupResult(
            source_run_id=run_id,
            child_job_id=child_job_id,
            status="deterministic_scope_applied",
        )

    def _mark_deterministic_result_ready(
        self,
        *,
        run_id: str,
        parent_job_id: str,
        child_job_id: str,
        classification: str,
        reason: str,
        evidence_refs: list[str],
    ) -> bool:
        """Persist one local scope result without inventing a ChatGPT task.

        The parent remains fail-closed in ``needs_human`` until a following
        deterministic workflow slice consumes ``physical_result_ready``.  The
        important distinction is that this is no longer represented as a
        reasoning/human ambiguity and therefore is not eligible for handoff.
        """

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self.database.sessions.begin() as session:
            run = session.get(AgentRunRecord, run_id)
            job = session.get(JobRecord, parent_job_id)
            if (
                run is None
                or job is None
                or run.state != "needs_human"
                or run.error_category != PHYSICAL_JOB_PENDING
                or JobState(job.state) is not JobState.needs_human
                or job.error_category != PHYSICAL_JOB_PENDING
                or job.lease_expires_at is not None
            ):
                return False

            latest_at = session.scalar(
                select(func.max(AgentJobBindingRecord.created_at)).where(
                    AgentJobBindingRecord.job_id == parent_job_id
                )
            )
            latest_ids = (
                []
                if latest_at is None
                else list(
                    session.scalars(
                        select(AgentJobBindingRecord.run_id).where(
                            AgentJobBindingRecord.job_id == parent_job_id,
                            AgentJobBindingRecord.created_at == latest_at,
                        )
                    )
                )
            )
            if latest_ids != [run_id]:
                return False

            next_index = run.step_count + 1
            run.step_count = next_index
            run.error_category = PHYSICAL_RESULT_READY
            run.error_detail = (
                f"Physical child Job {child_job_id} persisted deterministic scope "
                f"{classification}: {reason}"
            )
            run.updated_at = now
            session.add(
                AgentStepRecord(
                    run_id=run_id,
                    step_index=next_index,
                    kind="checkpoint",
                    tool_name=_SHOP_PREFLIGHT_TOOL,
                    tool_call_id=None,
                    input_json={"physical_job_id": child_job_id},
                    output_json={
                        "classification": classification,
                        "reason": reason,
                        "physical_job_id": child_job_id,
                    },
                    evidence_refs_json=list(evidence_refs),
                    status="succeeded",
                    error_category=None,
                    error_detail=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            job.current_stage = PHYSICAL_RESULT_READY
            job.error_category = PHYSICAL_RESULT_READY
            job.updated_at = now
            session.add(
                JobLogRecord(
                    job_id=parent_job_id,
                    level="info",
                    message=(
                        f"Physical child Job {child_job_id} produced deterministic scope "
                        f"{classification}; ChatGPT handoff was not requested."
                    ),
                    created_at=now,
                )
            )
        return True

    @staticmethod
    def _child_job_id(output_json: dict[str, Any] | None) -> str | None:
        if not isinstance(output_json, dict):
            return None
        output = output_json.get("output")
        if not isinstance(output, dict):
            return None
        value = output.get("job_id")
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _origin_matches(
        origin: Any,
        *,
        run_id: str,
        tool_call_id: str | None,
    ) -> bool:
        return bool(
            isinstance(origin, dict)
            and origin.get("kind") == "agent_tool"
            and origin.get("run_id") == run_id
            and origin.get("tool_call_id") == tool_call_id
            and origin.get("tool_name") == _SHOP_PREFLIGHT_TOOL
        )

    @staticmethod
    def _scope_result(artifacts: list[JobArtifactRecord]) -> dict[str, Any] | None:
        for artifact in reversed(artifacts):
            if artifact.kind != _SCOPE_ARTIFACT_KIND:
                continue
            metadata = dict(artifact.metadata_json or {})
            payload = metadata.get("result")
            if isinstance(payload, dict):
                return payload
        return None

    @staticmethod
    def _handoff_task(
        *,
        child_job_id: str,
        child_state: JobState,
        scope: dict[str, Any],
    ) -> dict[str, Any]:
        classification = scope.get("classification")
        reason = scope.get("reason")
        ambiguous = classification == "needs_human"
        return {
            "kind": "shop_scope_review",
            "physical_job_id": child_job_id,
            "physical_job_state": child_state.value,
            "deterministic_scope_classification": classification,
            "deterministic_scope_reason": reason,
            "requires_scope_judgment": ambiguous,
            "allowed_classifications": [
                "in_scope",
                "out_of_scope_physical",
                "needs_human",
            ],
            "instruction": (
                "Inspect only the cited durable shop evidence, including screenshots when useful. "
                "If requires_scope_judgment=true, decide whether the products prove digital/virtual "
                "delivery, physical goods, or remain genuinely ambiguous. If it is false, preserve "
                "the deterministic classification and do not override it. Return only the result "
                "contract fields and cite only supplied evidence refs."
            ),
        }


class AgentPhysicalFollowupWorker:
    """Small local poller; reconciliation itself is idempotent and authority-checked."""

    def __init__(
        self,
        reconciler: AgentPhysicalFollowupReconciler,
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
                name="agent-physical-followup",
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
                # Never mutate Job authority from an error handler.  The next
                # pass can retry because request_followup_handoff is CAS-guarded.
                continue
