"""Durable manual-ChatGPT handoff bridge for the local AgentDock workflow.

The bridge intentionally stops at *accepted external result*.  It does not
re-claim the Job or create a continuation run; that is a separate authority
boundary which requires a real orchestration driver.  See architecture note 18.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Callable
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import DateTime, ForeignKey, JSON, String, Text, exists, func, select, update
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.agent_runtime.job_binding import (
    AGENT_ORCHESTRATION_JOB_TYPE,
    AgentJobBindingRecord,
)
from backend.app.agent_runtime.persistence import (
    AgentRuntimeBase,
    AgentRunRecord,
    AgentRunStore,
    AgentStepRecord,
    HumanActionRecord,
)
from backend.app.agent_runtime.types import AgentRunState
from backend.app.db import Database
from backend.app.models.jobs import JobLogRecord, JobRecord, JobState


MANUAL_CHATGPT_TOOL_NAME = "manual_chatgpt"
MANUAL_CHATGPT_REQUIRED = "manual_chatgpt_required"
MANUAL_CHATGPT_RESULT_READY = "manual_chatgpt_result_ready"
_DEFAULT_MAX_PACKAGE_BYTES = 2 * 1024 * 1024
_DEFAULT_MAX_RESULT_BYTES = 2 * 1024 * 1024


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ManualChatGPTHandoffError(RuntimeError):
    """A handoff request or AgentDock return cannot be accepted safely."""


class HandoffResultContract(BaseModel):
    """Small deterministic contract for the first external-result bridge slice."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(min_length=1, max_length=64)
    required_fields: tuple[str, ...] = ()
    allow_extra_fields: bool = True

    @field_validator("required_fields")
    @classmethod
    def _validate_required_fields(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("required_fields must be unique")
        for item in value:
            if not item or len(item) > 128:
                raise ValueError("required_fields entries must be 1..128 characters")
        return value

    def validate_result(self, result: dict[str, Any]) -> None:
        missing = [name for name in self.required_fields if name not in result]
        if missing:
            raise ManualChatGPTHandoffError(
                "ChatGPT result is missing required fields: " + ", ".join(missing)
            )
        if not self.allow_extra_fields:
            extra = sorted(set(result) - set(self.required_fields))
            if extra:
                raise ManualChatGPTHandoffError(
                    "ChatGPT result contains unexpected fields: " + ", ".join(extra)
                )


class ManualChatGPTHandoffRecord(AgentRuntimeBase):
    __tablename__ = "manual_chatgpt_handoffs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id"), nullable=False, index=True
    )
    human_action_id: Mapped[str] = mapped_column(
        ForeignKey("human_actions.id"), nullable=False, unique=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    stage_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    task_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    context_refs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    result_contract_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    package_path: Mapped[str] = mapped_column(Text, nullable=False)
    result_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


@dataclass(frozen=True, slots=True)
class ManualChatGPTHandoff:
    handoff_id: str
    job_id: str
    source_run_id: str
    human_action_id: str
    input_hash: str
    schema_version: str
    package_path: str
    return_path: str


@dataclass(frozen=True, slots=True)
class AcceptedManualChatGPTResult:
    handoff_id: str
    job_id: str
    source_run_id: str
    human_action_id: str
    evidence_ref: str
    result: dict[str, Any]


def _canonical_json(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ManualChatGPTHandoffError("Handoff payload must be strict JSON data.") from exc
    return encoded.encode("utf-8")


def _input_hash(
    *,
    task: dict[str, Any],
    context_refs: list[str],
    stage_revision: str,
    result_contract: HandoffResultContract,
) -> str:
    payload = {
        "task": task,
        "context_refs": context_refs,
        "stage_revision": stage_revision,
        "result_contract": result_contract.model_dump(mode="json"),
    }
    return "sha256:" + hashlib.sha256(_canonical_json(payload)).hexdigest()


def _validated_uuid(value: str, *, label: str) -> str:
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ManualChatGPTHandoffError(f"{label} must be a UUID.") from exc
    canonical = str(parsed)
    if value.lower() != canonical:
        raise ManualChatGPTHandoffError(f"{label} must use canonical UUID text.")
    return canonical


def _latest_binding_predicates(job_id: str, run_id: str):
    table = AgentJobBindingRecord.__table__
    max_binding = table.alias("manual_chatgpt_max_binding")
    count_binding = table.alias("manual_chatgpt_count_binding")
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


class ManualChatGPTHandoffService:
    """Create local ChatGPT handoffs and accept AgentDock return envelopes."""

    def __init__(
        self,
        database: Database,
        *,
        runtime_dir: Path,
        handoff_id_factory: Callable[[], str] | None = None,
        human_action_id_factory: Callable[[], str] | None = None,
        max_package_bytes: int = _DEFAULT_MAX_PACKAGE_BYTES,
        max_result_bytes: int = _DEFAULT_MAX_RESULT_BYTES,
    ) -> None:
        if max_package_bytes < 1 or max_result_bytes < 1:
            raise ValueError("handoff file size limits must be positive")
        self.database = database
        self.runtime_dir = runtime_dir.resolve()
        self._handoff_id_factory = handoff_id_factory or (lambda: str(uuid4()))
        self._human_action_id_factory = human_action_id_factory or (lambda: str(uuid4()))
        self.max_package_bytes = max_package_bytes
        self.max_result_bytes = max_result_bytes
        self.store = AgentRunStore(database)
        AgentJobBindingRecord.__table__.create(bind=database.engine, checkfirst=True)
        ManualChatGPTHandoffRecord.__table__.create(bind=database.engine, checkfirst=True)

    def request_handoff(
        self,
        source_run_id: str,
        *,
        task: dict[str, Any],
        context_refs: list[str],
        stage_revision: str,
        result_contract: HandoffResultContract,
    ) -> ManualChatGPTHandoff:
        """Atomically tighten one running Job/AgentRun into manual ChatGPT wait."""

        if not stage_revision or len(stage_revision) > 128:
            raise ValueError("stage_revision must be 1..128 characters")
        if len(context_refs) > 256:
            raise ValueError("context_refs is too large")
        for ref in context_refs:
            if not isinstance(ref, str) or not ref or len(ref) > 2_000:
                raise ValueError("context_refs entries must be bounded non-empty strings")
        # Strict-JSON validation happens before any state write.
        _canonical_json(task)
        contract_json = result_contract.model_dump(mode="json")
        _canonical_json(contract_json)

        handoff_id = _validated_uuid(self._handoff_id_factory(), label="handoff_id")
        human_action_id = _validated_uuid(
            self._human_action_id_factory(), label="human_action_id"
        )
        input_hash = _input_hash(
            task=task,
            context_refs=context_refs,
            stage_revision=stage_revision,
            result_contract=result_contract,
        )
        tool_call_id = f"manual-chatgpt:{handoff_id}"
        package_rel = Path("chatgpt-handoffs") / "outbox" / f"{handoff_id}.json"
        return_rel = Path("external-results") / f"{handoff_id}.json"
        now = _utcnow()

        with self.database.sessions.begin() as session:
            binding = session.get(AgentJobBindingRecord, source_run_id)
            source = session.get(AgentRunRecord, source_run_id)
            if binding is None or source is None:
                raise ManualChatGPTHandoffError(
                    "Source AgentRun and durable Job binding must both exist."
                )
            job = session.get(JobRecord, binding.job_id)
            if job is None:
                raise ManualChatGPTHandoffError("Bound Job does not exist.")
            if job.type != AGENT_ORCHESTRATION_JOB_TYPE:
                raise ManualChatGPTHandoffError(
                    "Manual ChatGPT handoff only applies to agent_orchestration Jobs."
                )
            if JobState(job.state) is not JobState.running:
                raise ManualChatGPTHandoffError("Bound Job must be running before handoff.")
            if job.lease_expires_at is None or job.lease_expires_at <= now:
                raise ManualChatGPTHandoffError(
                    "Bound Job has no valid running lease; handoff fails closed."
                )
            if source.state != AgentRunState.running.value:
                raise ManualChatGPTHandoffError("Source AgentRun must be running before handoff.")
            pending = session.scalar(
                select(HumanActionRecord.id).where(
                    HumanActionRecord.run_id == source_run_id,
                    HumanActionRecord.status == "pending",
                )
            )
            if pending is not None:
                raise ManualChatGPTHandoffError(
                    "Source AgentRun already has a pending HumanAction."
                )

            latest_count, source_is_latest = _latest_binding_predicates(
                job.id, source_run_id
            )
            tightened = session.execute(
                update(JobRecord)
                .where(
                    JobRecord.id == job.id,
                    JobRecord.type == AGENT_ORCHESTRATION_JOB_TYPE,
                    JobRecord.state == JobState.running.value,
                    JobRecord.retry_count == job.retry_count,
                    JobRecord.lease_expires_at == job.lease_expires_at,
                    JobRecord.lease_expires_at > now,
                    latest_count == 1,
                    source_is_latest,
                )
                .values(
                    state=JobState.needs_human.value,
                    current_stage=MANUAL_CHATGPT_REQUIRED,
                    error_category=MANUAL_CHATGPT_REQUIRED,
                    lease_expires_at=None,
                    updated_at=now,
                )
            )
            if tightened.rowcount != 1:
                raise ManualChatGPTHandoffError(
                    "Job authority changed while requesting handoff; nothing was committed."
                )

            next_index = source.step_count + 1
            source.step_count = next_index
            source.state = AgentRunState.needs_human.value
            source.error_category = MANUAL_CHATGPT_REQUIRED
            source.error_detail = f"Waiting for AgentDock ChatGPT handoff {handoff_id}."
            source.updated_at = now
            source.completed_at = None

            session.add(
                AgentStepRecord(
                    run_id=source_run_id,
                    step_index=next_index,
                    kind="external_handoff",
                    tool_name=MANUAL_CHATGPT_TOOL_NAME,
                    tool_call_id=tool_call_id,
                    input_json={
                        "handoff_id": handoff_id,
                        "input_hash": input_hash,
                        "schema_version": result_contract.schema_version,
                        "stage_revision": stage_revision,
                    },
                    output_json=None,
                    evidence_refs_json=[],
                    status="needs_human",
                    error_category=MANUAL_CHATGPT_REQUIRED,
                    error_detail="Waiting for ChatGPT result via AgentDock.",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                HumanActionRecord(
                    id=human_action_id,
                    run_id=source_run_id,
                    tool_call_id=tool_call_id,
                    tool_name=MANUAL_CHATGPT_TOOL_NAME,
                    status="pending",
                    request_json={
                        "kind": "manual_chatgpt",
                        "handoff_id": handoff_id,
                        "input_hash": input_hash,
                        "schema_version": result_contract.schema_version,
                        "stage_revision": stage_revision,
                        "package_path": package_rel.as_posix(),
                        "return_path": return_rel.as_posix(),
                    },
                    resolution_json=None,
                    created_at=now,
                    resolved_at=None,
                )
            )
            # The handoff row has a real FK to human_actions.  SQLAlchemy has no
            # ORM relationship between these isolated spike records, so make the
            # parent ordering explicit while preserving the single transaction.
            session.flush()
            session.add(
                ManualChatGPTHandoffRecord(
                    id=handoff_id,
                    source_run_id=source_run_id,
                    human_action_id=human_action_id,
                    status="pending",
                    stage_revision=stage_revision,
                    schema_version=result_contract.schema_version,
                    input_hash=input_hash,
                    task_json=task,
                    context_refs_json=list(context_refs),
                    result_contract_json=contract_json,
                    package_path=package_rel.as_posix(),
                    result_path=None,
                    result_json=None,
                    created_at=now,
                    accepted_at=None,
                )
            )
            session.add(
                JobLogRecord(
                    job_id=job.id,
                    level="info",
                    message=(
                        f"Agent run {source_run_id} requested manual ChatGPT handoff "
                        f"{handoff_id}; Job moved to needs_human and released its lease."
                    ),
                    created_at=now,
                )
            )
            session.flush()
            job_id = job.id

        # DB wait state is authoritative.  If disk materialization fails, the
        # package can be regenerated idempotently without reopening the Job.
        self.materialize_package(handoff_id)
        return ManualChatGPTHandoff(
            handoff_id=handoff_id,
            job_id=job_id,
            source_run_id=source_run_id,
            human_action_id=human_action_id,
            input_hash=input_hash,
            schema_version=result_contract.schema_version,
            package_path=package_rel.as_posix(),
            return_path=return_rel.as_posix(),
        )

    def materialize_package(self, handoff_id: str) -> Path:
        handoff_id = _validated_uuid(handoff_id, label="handoff_id")
        with self.database.sessions() as session:
            record = session.get(ManualChatGPTHandoffRecord, handoff_id)
            if record is None:
                raise ManualChatGPTHandoffError(f"Unknown handoff {handoff_id}.")
            package = self._package_from_record(record)
            relative = Path(record.package_path)

        encoded = json.dumps(
            package,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > self.max_package_bytes:
            raise ManualChatGPTHandoffError("Handoff package exceeds the configured size limit.")
        target = self._safe_runtime_path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_bytes(encoded)
        temporary.replace(target)
        return target

    def accept_return_file(self, handoff_id: str) -> AcceptedManualChatGPTResult:
        """Validate one fixed-path AgentDock envelope and persist it atomically."""

        handoff_id = _validated_uuid(handoff_id, label="handoff_id")
        result_rel = Path("external-results") / f"{handoff_id}.json"
        result_path = self._safe_runtime_path(result_rel)
        if not result_path.exists() or not result_path.is_file() or result_path.is_symlink():
            raise ManualChatGPTHandoffError("AgentDock return file is missing or unsafe.")
        if result_path.stat().st_size > self.max_result_bytes:
            raise ManualChatGPTHandoffError("AgentDock return file exceeds the configured size limit.")
        try:
            envelope = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ManualChatGPTHandoffError("AgentDock return file is not valid UTF-8 JSON.") from exc
        if not isinstance(envelope, dict):
            raise ManualChatGPTHandoffError("AgentDock return envelope must be a JSON object.")
        expected_envelope_fields = {"handoff_id", "schema_version", "input_hash", "result"}
        if set(envelope) != expected_envelope_fields:
            raise ManualChatGPTHandoffError(
                "AgentDock return envelope has unexpected or missing top-level fields."
            )
        if envelope.get("handoff_id") != handoff_id:
            raise ManualChatGPTHandoffError("AgentDock return handoff_id does not match the file identity.")
        if not isinstance(envelope.get("result"), dict):
            raise ManualChatGPTHandoffError("AgentDock return result must be a JSON object.")
        _canonical_json(envelope)

        now = _utcnow()
        with self.database.sessions.begin() as session:
            record = session.get(ManualChatGPTHandoffRecord, handoff_id)
            if record is None:
                raise ManualChatGPTHandoffError(f"Unknown handoff {handoff_id}.")
            if record.status != "pending":
                raise ManualChatGPTHandoffError(
                    "Handoff result was already accepted or is no longer pending; replay rejected."
                )
            if envelope.get("schema_version") != record.schema_version:
                raise ManualChatGPTHandoffError("AgentDock return schema_version is stale or mismatched.")
            if envelope.get("input_hash") != record.input_hash:
                raise ManualChatGPTHandoffError("AgentDock return input_hash is stale or mismatched.")

            contract = HandoffResultContract.model_validate(record.result_contract_json)
            expected_hash = _input_hash(
                task=dict(record.task_json),
                context_refs=list(record.context_refs_json),
                stage_revision=record.stage_revision,
                result_contract=contract,
            )
            if expected_hash != record.input_hash:
                raise ManualChatGPTHandoffError(
                    "Durable handoff payload no longer matches its stored input_hash."
                )
            result = dict(envelope["result"])
            contract.validate_result(result)

            source = session.get(AgentRunRecord, record.source_run_id)
            human = session.get(HumanActionRecord, record.human_action_id)
            binding = session.get(AgentJobBindingRecord, record.source_run_id)
            if source is None or human is None or binding is None:
                raise ManualChatGPTHandoffError(
                    "Handoff lost its source AgentRun, HumanAction, or Job binding."
                )
            job = session.get(JobRecord, binding.job_id)
            if job is None:
                raise ManualChatGPTHandoffError("Handoff bound Job no longer exists.")
            if (
                human.run_id != record.source_run_id
                or human.tool_name != MANUAL_CHATGPT_TOOL_NAME
                or human.tool_call_id != f"manual-chatgpt:{handoff_id}"
                or human.status != "pending"
            ):
                raise ManualChatGPTHandoffError(
                    "Handoff HumanAction identity/status changed; result rejected."
                )
            if (
                source.state != AgentRunState.needs_human.value
                or source.error_category != MANUAL_CHATGPT_REQUIRED
            ):
                raise ManualChatGPTHandoffError(
                    "Source AgentRun is no longer waiting for this manual ChatGPT result."
                )
            if (
                job.type != AGENT_ORCHESTRATION_JOB_TYPE
                or JobState(job.state) is not JobState.needs_human
                or job.lease_expires_at is not None
            ):
                raise ManualChatGPTHandoffError(
                    "Bound Job is no longer an authoritative lease-free manual wait."
                )

            latest_count, source_is_latest = _latest_binding_predicates(
                job.id, record.source_run_id
            )
            # This no-state-change CAS is the authoritative write-boundary gate.
            # SQLite takes the writer lock here; if a newer continuation won the
            # race first, the predicates fail and all result writes roll back.
            guarded_job = session.execute(
                update(JobRecord)
                .where(
                    JobRecord.id == job.id,
                    JobRecord.type == AGENT_ORCHESTRATION_JOB_TYPE,
                    JobRecord.state == JobState.needs_human.value,
                    JobRecord.lease_expires_at.is_(None),
                    latest_count == 1,
                    source_is_latest,
                )
                .values(
                    current_stage=MANUAL_CHATGPT_RESULT_READY,
                    error_category=MANUAL_CHATGPT_RESULT_READY,
                    updated_at=now,
                )
            )
            if guarded_job.rowcount != 1:
                raise ManualChatGPTHandoffError(
                    "Job/binding authority changed while accepting result; nothing was committed."
                )

            evidence_ref = f"external:chatgpt:{handoff_id}"
            next_index = source.step_count + 1
            source.step_count = next_index
            source.error_category = MANUAL_CHATGPT_RESULT_READY
            source.error_detail = (
                f"ChatGPT handoff {handoff_id} accepted; awaiting safe external-result continuation."
            )
            source.updated_at = now
            session.add(
                AgentStepRecord(
                    run_id=source.id,
                    step_index=next_index,
                    kind="external_result",
                    tool_name=MANUAL_CHATGPT_TOOL_NAME,
                    tool_call_id=f"manual-chatgpt:{handoff_id}",
                    input_json={
                        "handoff_id": handoff_id,
                        "input_hash": record.input_hash,
                        "schema_version": record.schema_version,
                    },
                    output_json=result,
                    evidence_refs_json=[evidence_ref],
                    status="succeeded",
                    error_category=None,
                    error_detail=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            wait_step = session.scalar(
                select(AgentStepRecord)
                .where(
                    AgentStepRecord.run_id == source.id,
                    AgentStepRecord.kind == "external_handoff",
                    AgentStepRecord.tool_call_id == f"manual-chatgpt:{handoff_id}",
                )
                .order_by(AgentStepRecord.step_index.desc())
            )
            if wait_step is None or wait_step.status != "needs_human":
                raise ManualChatGPTHandoffError(
                    "Durable handoff wait step is missing or already resolved."
                )
            wait_step.status = "resolved"
            wait_step.output_json = {"accepted_evidence_ref": evidence_ref}
            wait_step.error_category = None
            wait_step.error_detail = None
            wait_step.updated_at = now

            human.status = "completed"
            human.resolution_json = {
                "kind": "manual_chatgpt",
                "accepted": True,
                "handoff_id": handoff_id,
                "evidence_ref": evidence_ref,
            }
            human.resolved_at = now
            record.status = "accepted"
            record.result_path = result_rel.as_posix()
            record.result_json = result
            record.accepted_at = now
            session.add(
                JobLogRecord(
                    job_id=job.id,
                    level="info",
                    message=(
                        f"AgentDock returned ChatGPT handoff {handoff_id}; result was validated "
                        "and persisted before any Job continuation."
                    ),
                    created_at=now,
                )
            )
            session.flush()
            job_id = job.id
            accepted_source_run_id = record.source_run_id
            accepted_human_action_id = record.human_action_id

        return AcceptedManualChatGPTResult(
            handoff_id=handoff_id,
            job_id=job_id,
            source_run_id=accepted_source_run_id,
            human_action_id=accepted_human_action_id,
            evidence_ref=evidence_ref,
            result=result,
        )

    def _package_from_record(self, record: ManualChatGPTHandoffRecord) -> dict[str, Any]:
        return {
            "handoff_id": record.id,
            "mode": "manual_chatgpt",
            "source_run_id": record.source_run_id,
            "stage_revision": record.stage_revision,
            "schema_version": record.schema_version,
            "input_hash": record.input_hash,
            "task": record.task_json,
            "context_refs": record.context_refs_json,
            "result_contract": record.result_contract_json,
            "agentdock_return": {
                "relative_path": f"external-results/{record.id}.json",
                "envelope_fields": [
                    "handoff_id",
                    "schema_version",
                    "input_hash",
                    "result",
                ],
            },
        }

    def _safe_runtime_path(self, relative: Path) -> Path:
        if relative.is_absolute() or ".." in relative.parts:
            raise ManualChatGPTHandoffError("Handoff path must stay inside runtime_dir.")
        target = (self.runtime_dir / relative).resolve()
        try:
            target.relative_to(self.runtime_dir)
        except ValueError as exc:
            raise ManualChatGPTHandoffError("Handoff path escapes runtime_dir.") from exc
        return target
