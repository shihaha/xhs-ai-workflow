"""Consume accepted ChatGPT shop-scope results without replaying physical work."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event, RLock, Thread
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import exists, func, select

from backend.app.agent_runtime.external_result_finalizer import (
    DeterministicExternalResultFinalizer,
    ExternalResultFinalizationError,
)
from backend.app.agent_runtime.job_binding import AgentJobBindingRecord
from backend.app.agent_runtime.manual_chatgpt_handoff import (
    MANUAL_CHATGPT_RESULT_READY,
    ManualChatGPTHandoffRecord,
)
from backend.app.agent_runtime.persistence import AgentRunRecord, AgentStepRecord
from backend.app.agent_runtime.physical_followup import SHOP_SCOPE_HANDOFF_REVISION
from backend.app.db import Database
from backend.app.features.shops.service import ShopCollectionService
from backend.app.models.jobs import JobArtifactRecord, JobRecord, JobState


class ChatGPTShopScopeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    classification: Literal["in_scope", "out_of_scope_physical", "needs_human"]
    reason: str = Field(min_length=1, max_length=500)
    evidence_refs: list[str] = Field(min_length=1, max_length=20)

    @field_validator("evidence_refs")
    @classmethod
    def validate_refs(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("evidence_refs must be distinct non-empty strings")
        for item in value:
            prefix, separator, raw_id = item.partition(":")
            if prefix != "artifact" or separator != ":" or not raw_id.isdigit():
                raise ValueError("shop scope evidence refs must use artifact:<id>")
        return value


@dataclass(frozen=True, slots=True)
class ChatGPTScopeApplyResult:
    handoff_id: str
    physical_job_id: str
    classification: str
    status: str
    scope_decision_job_id: str | None = None
    continuation_run_id: str | None = None


class AgentChatGPTScopeResultReconciler:
    """Validate and apply only the Stage-6 shop-scope handoff contract."""

    def __init__(
        self,
        database: Database,
        *,
        shop_service: ShopCollectionService,
    ) -> None:
        self.database = database
        self.shop_service = shop_service
        self.finalizer = DeterministicExternalResultFinalizer(database)

    def reconcile_once(self) -> list[ChatGPTScopeApplyResult]:
        binding = AgentJobBindingRecord.__table__.alias("chatgpt_scope_source_binding")
        newer = AgentJobBindingRecord.__table__.alias("chatgpt_scope_newer_binding")
        same_time = AgentJobBindingRecord.__table__.alias("chatgpt_scope_same_time_binding")
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
                        ManualChatGPTHandoffRecord.stage_revision == SHOP_SCOPE_HANDOFF_REVISION,
                        AgentRunRecord.state == "needs_human",
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

        results: list[ChatGPTScopeApplyResult] = []
        for handoff_id in handoff_ids:
            try:
                result = self._apply(handoff_id)
            except (ValueError, ExternalResultFinalizationError):
                # The accepted result remains durable and visible.  Validation or
                # authority errors never trigger a phone replay or raw DB repair.
                continue
            if result is not None:
                results.append(result)
        return results

    def _apply(self, handoff_id: str) -> ChatGPTScopeApplyResult | None:
        with self.database.sessions() as session:
            handoff = session.get(ManualChatGPTHandoffRecord, handoff_id)
            if (
                handoff is None
                or handoff.status != "accepted"
                or handoff.stage_revision != SHOP_SCOPE_HANDOFF_REVISION
                or not isinstance(handoff.result_json, dict)
                or not isinstance(handoff.task_json, dict)
            ):
                return None
            source = session.get(AgentRunRecord, handoff.source_run_id)
            source_binding = session.get(AgentJobBindingRecord, handoff.source_run_id)
            if source is None or source_binding is None:
                return None
            parent_job = session.get(JobRecord, source_binding.job_id)
            if (
                parent_job is None
                or parent_job.type != "agent_orchestration"
                or JobState(parent_job.state) is not JobState.needs_human
                or parent_job.current_stage != MANUAL_CHATGPT_RESULT_READY
                or parent_job.error_category != MANUAL_CHATGPT_RESULT_READY
                or parent_job.lease_expires_at is not None
                or not self._is_unique_latest_binding(
                    session,
                    job_id=parent_job.id,
                    run_id=handoff.source_run_id,
                )
            ):
                return None
            if (
                source.state != "needs_human"
                or source.error_category != MANUAL_CHATGPT_RESULT_READY
            ):
                return None

            task = dict(handoff.task_json)
            if task.get("kind") != "shop_scope_review":
                return None
            if task.get("requires_scope_judgment") is not True:
                # Clear deterministic classifications must never be overridden by
                # ChatGPT. They are finalized by the physical-result reconciler.
                return None
            if task.get("deterministic_scope_classification") != "needs_human":
                return None
            physical_job_id = task.get("physical_job_id")
            if not isinstance(physical_job_id, str) or not physical_job_id:
                return None
            physical_job = session.get(JobRecord, physical_job_id)
            if physical_job is None or physical_job.type != "android_shop_collection":
                return None
            account_user_id = physical_job.input_data.get("account_user_id")
            origin = physical_job.input_data.get("_agent_origin")
            if not isinstance(account_user_id, str) or not account_user_id:
                return None
            if not self._origin_matches(
                origin,
                source_run_id=handoff.source_run_id,
                physical_job_id=physical_job_id,
                session=session,
            ):
                return None
            result = ChatGPTShopScopeResult.model_validate(handoff.result_json)
            allowed_context = set(handoff.context_refs_json or [])
            if not set(result.evidence_refs).issubset(allowed_context):
                raise ValueError("ChatGPT cited evidence outside the handoff context")
            evidence_paths = self._resolve_artifact_paths(
                session,
                physical_job_id=physical_job_id,
                account_user_id=account_user_id,
                refs=result.evidence_refs,
            )
            external_ref = f"external:chatgpt:{handoff.id}"

        decision = self.shop_service.record_account_scope_decision(
            account_user_id=account_user_id,
            classification=result.classification,
            decision_source="chatgpt",
            reason=result.reason,
            evidence_refs=evidence_paths,
            source_handoff_id=handoff_id,
        )

        if result.classification == "needs_human":
            # ChatGPT itself could not establish a safe delivery scope. Preserve
            # the accepted result + domain decision and leave the parent Job at a
            # human boundary rather than manufacturing success.
            return ChatGPTScopeApplyResult(
                handoff_id=handoff_id,
                physical_job_id=physical_job_id,
                classification=result.classification,
                status="chatgpt_still_ambiguous",
                scope_decision_job_id=decision.job_id,
            )

        final_output = {
            "kind": "shop_scope_review_applied",
            "handoff_id": handoff_id,
            "physical_job_id": physical_job_id,
            "account_user_id": account_user_id,
            "classification": result.classification,
            "reason": result.reason,
            "scope_decision_job_id": decision.job_id,
            "decision_source": "chatgpt",
            "physical_replay": False,
        }
        continuation_run_id = self.finalizer.finalize(
            source_run_id=handoff.source_run_id,
            continuation_key=f"shop-scope-{handoff_id}",
            final_output=final_output,
            evidence_refs=[external_ref, *result.evidence_refs],
        )
        return ChatGPTScopeApplyResult(
            handoff_id=handoff_id,
            physical_job_id=physical_job_id,
            classification=result.classification,
            status="applied",
            scope_decision_job_id=decision.job_id,
            continuation_run_id=continuation_run_id,
        )

    @staticmethod
    def _resolve_artifact_paths(
        session: Any,
        *,
        physical_job_id: str,
        account_user_id: str,
        refs: list[str],
    ) -> list[str]:
        ids = [int(item.split(":", 1)[1]) for item in refs]
        artifacts = list(
            session.scalars(
                select(JobArtifactRecord)
                .where(JobArtifactRecord.id.in_(ids))
                .order_by(JobArtifactRecord.id)
            )
        )
        by_id = {artifact.id: artifact for artifact in artifacts}
        if set(by_id) != set(ids):
            raise ValueError("ChatGPT cited a missing artifact")
        physical_job = session.get(JobRecord, physical_job_id)
        if (
            physical_job is None
            or physical_job.input_data.get("account_user_id") != account_user_id
        ):
            raise ValueError("Physical Job account identity changed")
        paths: list[str] = []
        for artifact_id in ids:
            artifact = by_id[artifact_id]
            if artifact.job_id != physical_job_id:
                raise ValueError("ChatGPT cited an artifact from another Job")
            paths.append(artifact.path)
        return paths

    @staticmethod
    def _is_unique_latest_binding(session: Any, *, job_id: str, run_id: str) -> bool:
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

    @staticmethod
    def _origin_matches(
        origin: Any,
        *,
        source_run_id: str,
        physical_job_id: str,
        session: Any,
    ) -> bool:
        if not (
            isinstance(origin, dict)
            and origin.get("kind") == "agent_tool"
            and origin.get("run_id") == source_run_id
            and origin.get("tool_name") == "shop.preflight"
            and isinstance(origin.get("tool_call_id"), str)
        ):
            return False
        step = session.scalar(
            select(AgentStepRecord)
            .where(
                AgentStepRecord.run_id == source_run_id,
                AgentStepRecord.kind == "tool",
                AgentStepRecord.tool_name == "shop.preflight",
                AgentStepRecord.tool_call_id == origin["tool_call_id"],
            )
            .order_by(AgentStepRecord.step_index.desc())
            .limit(1)
        )
        if step is None or not isinstance(step.output_json, dict):
            return False
        output = step.output_json.get("output")
        return isinstance(output, dict) and output.get("job_id") == physical_job_id


class AgentChatGPTScopeResultWorker:
    def __init__(
        self,
        reconciler: AgentChatGPTScopeResultReconciler,
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
                name="agent-chatgpt-scope-result",
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
