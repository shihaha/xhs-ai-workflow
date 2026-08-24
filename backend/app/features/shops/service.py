"""Shop collection orchestration and tutorial-compatible N/N verification."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import stat
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from threading import Event, RLock
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from sqlalchemy import select

from backend.app.adapters.android_device import DEFAULT_SELECTOR_PROFILE_VERSION
from backend.app.adapters.contracts import (
    CollectionItem,
    CollectionRequest,
    CollectionResult,
    RejectedCollectionItem,
)
from backend.app.models.jobs import JobState
from backend.app.models.jobs import JobArtifactRecord, JobRecord
from backend.app.services.jobs import InvalidJobTransition, JobNotFound, JobService
from backend.app.services.shop_discovery import (
    ShopProductDiscovery,
    read_shop_product_discoveries,
)
from backend.app.features.shops.scope import (
    ShopScopeDecision,
    ShopScopeEvidence,
    classify_shop_scope,
)


_IMAGE_EXTENSIONS = {".webp", ".png", ".jpg", ".jpeg"}
ANDROID_SHOP_JOB_TYPE = "android_shop_collection"
ANDROID_SHOP_JOB_TYPES = ("shop_collection", ANDROID_SHOP_JOB_TYPE)
SHOP_TEST_OVERRIDE_REASON = "保留服装店完成一次有界E2E验证"
ACCOUNT_SCOPE_JOB_TYPE = "shop_account_scope_decision"
ACCOUNT_SCOPE_ARTIFACT_KIND = "shop_account_scope_decision"
ACCOUNT_SCOPE_ARTIFACT_PRODUCER = "shop_scope_service_v1"


class ShopVerificationMissing(BaseModel):
    reference: str = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=500)
    product_dir: str | None = Field(default=None, max_length=500)


class ShopVerificationResult(BaseModel):
    expected_count: int = Field(ge=0)
    discovered_count: int = Field(ge=0)
    succeeded_count: int = Field(ge=0)
    missing_count: int = Field(ge=0)
    missing_items: list[ShopVerificationMissing] = Field(default_factory=list)
    overflow_count: int = Field(default=0, ge=0)
    issues: list[str] = Field(default_factory=list)
    complete: bool = False


class ShopCollectionCreate(BaseModel):
    account_user_id: str = Field(
        min_length=1, max_length=500, pattern=r"^[A-Za-z0-9_-]+$"
    )
    account_name: str = Field(min_length=1, max_length=500)
    expected_count: StrictInt = Field(default=3, ge=0)
    available_count_observed: StrictInt | None = Field(default=None, ge=0)
    collection_mode: Literal[
        "preflight",
        "legacy_full_shop",
        "bounded_sample",
        "evidence_sample",
        "full_shop",
    ] = "preflight"
    product_sample_limit: StrictInt | None = Field(default=None, ge=1, le=3)
    test_override: bool = False
    test_override_reason: str | None = Field(default=None, min_length=1, max_length=200)
    scope_gate_job_id: str | None = Field(default=None, min_length=1, max_length=100)
    device_id: str | None = Field(default=None, min_length=1, max_length=500)
    verification_dir: str | None = Field(default=None, min_length=1, max_length=1000)
    selector_profile_version: str = Field(
        default=DEFAULT_SELECTOR_PROFILE_VERSION, min_length=1, max_length=100
    )

    @field_validator(
        "account_name", "device_id", "verification_dir", "selector_profile_version"
    )
    @classmethod
    def trim_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must contain non-whitespace text")
        return normalized

    @field_validator("test_override_reason", "scope_gate_job_id")
    @classmethod
    def trim_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must contain non-whitespace text")
        return normalized

    @model_validator(mode="after")
    def enforce_collection_mode(self) -> "ShopCollectionCreate":
        if self.collection_mode == "preflight":
            if self.available_count_observed is None:
                self.available_count_observed = self.expected_count
            self.expected_count = 3
        elif self.collection_mode == "bounded_sample":
            if not self.test_override:
                raise ValueError("bounded_sample requires test_override=true")
            if self.test_override_reason != SHOP_TEST_OVERRIDE_REASON:
                raise ValueError("bounded_sample requires the approved test override reason")
            if self.product_sample_limit != 3:
                raise ValueError("bounded_sample requires product_sample_limit=3")
            if self.available_count_observed is None:
                self.available_count_observed = self.expected_count
            self.expected_count = 3
        elif self.collection_mode == "evidence_sample":
            if self.test_override or self.test_override_reason is not None:
                raise ValueError("evidence_sample cannot use test_override")
            observed = (
                self.available_count_observed
                if self.available_count_observed is not None
                else self.expected_count
            )
            if observed < 1:
                raise ValueError("evidence_sample requires at least one visible product")
            if self.product_sample_limit != 3:
                raise ValueError("evidence_sample requires product_sample_limit=3")
            if not self.scope_gate_job_id:
                raise ValueError("evidence_sample requires a trusted in_scope gate job")
            self.available_count_observed = observed
            self.expected_count = 3
        elif self.test_override or self.test_override_reason is not None:
            raise ValueError("test_override is only valid for bounded_sample")
        if self.collection_mode == "full_shop" and not self.scope_gate_job_id:
            raise ValueError("full_shop requires a trusted in_scope gate job")
        return self


class ShopCollectionMissing(BaseModel):
    reference: str
    reason: str
    raw_evidence: dict[str, Any]


class ShopCollectionRead(BaseModel):
    job_id: str
    status: Literal["succeeded", "partial", "needs_human", "failed"]
    detail: str | None
    selector_profile_version: str
    expected_count: int
    discovered_count: int
    collected_count: int
    raw_observation_count: int
    duplicate_observation_count: int
    succeeded_count: int
    missing_count: int
    missing_items: list[ShopCollectionMissing]
    collection_missing_count: int
    collection_missing_items: list[ShopCollectionMissing]
    rejected_count: int
    rejected_items: list[RejectedCollectionItem]
    overflow_count: int
    evidence_artifacts: list[str]
    items: list[CollectionItem]
    verification: ShopVerificationResult | None = None
    complete: bool
    collection_mode: Literal[
        "preflight",
        "legacy_full_shop",
        "bounded_sample",
        "evidence_sample",
        "full_shop",
    ] = "legacy_full_shop"
    product_sample_limit: int | None = None
    available_count_observed: int | None = None
    test_override: bool = False
    test_override_reason: str | None = None
    sample_complete: bool = False
    shop_complete: bool = False
    natural_end_reached: bool = False
    scope_classification: Literal[
        "in_scope", "out_of_scope_physical", "needs_human"
    ] | None = None
    scope_reason: str | None = None
    sample_manifest_path: str | None = Field(default=None, min_length=1, max_length=1000)
    sample_manifest_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    sample_collection_path: str | None = Field(
        default=None, min_length=1, max_length=1000
    )
    sample_collection_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def preserve_legacy_full_shop_semantics(self) -> "ShopCollectionRead":
        if (
            "shop_complete" not in self.model_fields_set
            and self.collection_mode == "legacy_full_shop"
        ):
            self.shop_complete = self.complete
        bindings = (
            self.sample_manifest_path,
            self.sample_manifest_sha256,
            self.sample_collection_path,
            self.sample_collection_sha256,
        )
        if any(value is not None for value in bindings) and not all(
            value is not None for value in bindings
        ):
            raise ValueError("bounded sample evidence bindings must be all present or all absent")
        if (
            all(value is not None for value in bindings)
            and self.collection_mode not in {"bounded_sample", "evidence_sample"}
        ):
            raise ValueError("sample evidence bindings require a sample mode")
        return self


class ShopCollectionQueued(BaseModel):
    job_id: str
    status: Literal["queued"] = "queued"


class AccountScopeDecision(BaseModel):
    schema_version: Literal[1] = 1
    job_id: str = Field(min_length=1, max_length=100)
    account_user_id: str = Field(min_length=1, max_length=500)
    classification: Literal[
        "unknown", "in_scope", "out_of_scope_physical", "needs_human"
    ]
    decision_source: Literal["rule", "model", "human", "chatgpt"]
    reason: str = Field(min_length=1, max_length=500)
    decided_at: str = Field(min_length=1, max_length=100)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("evidence_refs")
    @classmethod
    def distinct_evidence_refs(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value) or len(value) != len(set(value)):
            raise ValueError("account scope evidence refs must be distinct non-empty paths")
        return value


class InvalidVerificationPath(ValueError):
    """Raised when supplied verification evidence is outside runtime storage."""


class ShopCollectionServiceClosed(RuntimeError):
    """Raised when shutdown has closed admission for physical device work."""


@dataclass(frozen=True)
class _PendingShopResult:
    temp_path: str
    final_path: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class _BoundedSampleEvidenceBinding:
    verification_dir: Path
    manifest_path: str
    manifest_sha256: str
    collection_path: str
    collection_sha256: str


class ShopCollectionService:
    """Queue a durable job, run one bounded device pass and finalize factual state."""

    def __init__(
        self,
        *,
        job_service: JobService,
        device_adapter: Any,
        scope_model_adapter: Any | None = None,
        submitter: Callable[..., Any] | None = None,
        max_workers: int = 4,
    ) -> None:
        self.job_service = job_service
        self.device_adapter = device_adapter
        self.scope_model_adapter = scope_model_adapter
        for job_type in ANDROID_SHOP_JOB_TYPES:
            self.job_service.recover_interrupted_workers(job_type=job_type)
        self._executor = (
            ThreadPoolExecutor(
                max_workers=max(1, max_workers),
                thread_name_prefix="shop-collection",
            )
            if submitter is None
            else None
        )
        self._submitter = submitter or self._executor.submit
        self._lifecycle_lock = RLock()
        self._accepting_work = True
        self._cancel_events: dict[str, Event] = {}
        self._futures: dict[str, Future[Any]] = {}

    def enqueue(
        self,
        payload: ShopCollectionCreate,
        *,
        agent_origin: tuple[str, str] | None = None,
    ) -> ShopCollectionQueued:
        self._resolve_verification_dir(payload.verification_dir)
        existing_scope = self.get_account_scope_decision(payload.account_user_id)
        if (
            existing_scope is not None
            and existing_scope.classification == "out_of_scope_physical"
        ):
            raise ValueError(
                "account scope is already out_of_scope_physical; in_scope is required"
            )
        if payload.collection_mode in {"full_shop", "evidence_sample"}:
            self._require_trusted_in_scope_gate(payload)
        with self._lifecycle_lock:
            if not self._accepting_work:
                raise ShopCollectionServiceClosed(
                    "Shop collection service is closed."
                )
            input_data = payload.model_dump(mode="json")
            if agent_origin is not None:
                origin_run_id, origin_tool_call_id = agent_origin
                if not origin_run_id or len(origin_run_id) > 100:
                    raise ValueError("agent origin run_id must be bounded non-empty text")
                if not origin_tool_call_id or len(origin_tool_call_id) > 100:
                    raise ValueError("agent origin tool_call_id must be bounded non-empty text")
                input_data["_agent_origin"] = {
                    "kind": "agent_tool",
                    "run_id": origin_run_id,
                    "tool_call_id": origin_tool_call_id,
                    "tool_name": "shop.preflight",
                }
            job = self.job_service.create(
                job_type=ANDROID_SHOP_JOB_TYPE,
                input_data=input_data,
                progress_total=payload.expected_count,
                current_stage="device_pending",
            )
            cancel_event = Event()
            self._cancel_events[job.id] = cancel_event
            try:
                submitted = self._submitter(self.execute, job.id, payload)
            except Exception as error:
                self._cancel_events.pop(job.id, None)
                self.job_service.append_log(
                    job.id,
                    level="error",
                    message=(
                        "Shop collection scheduling raised: "
                        f"{type(error).__name__}: {error}"
                    ),
                )
                self.job_service.claim(job.id)
                self.job_service.transition(
                    job.id,
                    JobState.failed,
                    current_stage="shop_failed",
                    error_category="shop_collection_schedule_failed",
                )
                raise
            if isinstance(submitted, Future):
                self._futures[job.id] = submitted
                submitted.add_done_callback(
                    lambda _future, job_id=job.id: self._mark_job_finished(job_id)
                )
        return ShopCollectionQueued(job_id=job.id)

    def list_discoveries(self, job_id: str) -> list[ShopProductDiscovery]:
        """Read strict per-product discovery facts after failure or restart."""

        return read_shop_product_discoveries(self.job_service, job_id)

    def get_account_scope_decision(
        self, account_user_id: str
    ) -> AccountScopeDecision | None:
        """Return the newest strictly bound account-level scope decision."""

        with self.job_service.database.session() as session:
            rows = session.execute(
                select(JobArtifactRecord, JobRecord)
                .join(JobRecord, JobRecord.id == JobArtifactRecord.job_id)
                .where(JobArtifactRecord.kind == ACCOUNT_SCOPE_ARTIFACT_KIND)
                .order_by(JobArtifactRecord.id.desc())
            ).all()
        for artifact, job in rows:
            metadata = dict(artifact.metadata_json)
            payload = metadata.get("result")
            if (
                artifact.producer != ACCOUNT_SCOPE_ARTIFACT_PRODUCER
                or not isinstance(payload, dict)
                or payload.get("account_user_id") != account_user_id
                or payload.get("job_id") != job.id
                or job.state not in {JobState.succeeded, JobState.needs_human}
            ):
                continue
            path = _resolved_regular_file(
                self.job_service.runtime_dir / artifact.path,
                roots=(self.job_service.runtime_dir,),
            )
            if path is None or path.is_symlink():
                continue
            try:
                loaded = _load_json(path, "account-scope.json")
            except ValueError:
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if loaded != payload or metadata.get("sha256") != digest:
                continue
            try:
                return AccountScopeDecision.model_validate(payload)
            except ValueError:
                continue
        return None

    def record_account_scope_decision(
        self,
        *,
        account_user_id: str,
        classification: Literal[
            "unknown", "in_scope", "out_of_scope_physical", "needs_human"
        ],
        decision_source: Literal["rule", "model", "human", "chatgpt"],
        reason: str,
        evidence_refs: list[str],
        source_handoff_id: str | None = None,
    ) -> AccountScopeDecision:
        """Persist one auditable scope decision without editing history."""

        self._require_account_evidence_refs(account_user_id, evidence_refs)
        if source_handoff_id is not None:
            normalized_handoff = source_handoff_id.strip()
            if not normalized_handoff or len(normalized_handoff) > 100:
                raise ValueError("source_handoff_id must be bounded non-empty text")
            existing = self._account_scope_decision_for_handoff(
                account_user_id=account_user_id,
                source_handoff_id=normalized_handoff,
            )
            if existing is not None:
                return existing
            source_handoff_id = normalized_handoff
        job = self.job_service.create(
            job_type=ACCOUNT_SCOPE_JOB_TYPE,
            input_data={
                "account_user_id": account_user_id,
                **(
                    {"source_handoff_id": source_handoff_id}
                    if source_handoff_id is not None
                    else {}
                ),
            },
            current_stage="scope_decision_pending",
        )
        self.job_service.claim(job.id)
        decision = self._persist_account_scope_decision(
            job.id,
            account_user_id=account_user_id,
            classification=classification,
            decision_source=decision_source,
            reason=reason,
            evidence_refs=evidence_refs,
        )
        self.job_service.transition(
            job.id,
            JobState.succeeded,
            current_stage=f"account_scope_{classification}",
        )
        return decision

    def _account_scope_decision_for_handoff(
        self,
        *,
        account_user_id: str,
        source_handoff_id: str,
    ) -> AccountScopeDecision | None:
        """Recover one already-persisted ChatGPT decision after retry/restart."""

        with self.job_service.database.session() as session:
            jobs = list(
                session.scalars(
                    select(JobRecord)
                    .where(JobRecord.type == ACCOUNT_SCOPE_JOB_TYPE)
                    .order_by(JobRecord.created_at, JobRecord.id)
                )
            )
            for job in jobs:
                if (
                    job.input_data.get("account_user_id") != account_user_id
                    or job.input_data.get("source_handoff_id") != source_handoff_id
                    or JobState(job.state) is not JobState.succeeded
                ):
                    continue
                artifact = session.scalar(
                    select(JobArtifactRecord)
                    .where(
                        JobArtifactRecord.job_id == job.id,
                        JobArtifactRecord.kind == ACCOUNT_SCOPE_ARTIFACT_KIND,
                    )
                    .order_by(JobArtifactRecord.id.desc())
                    .limit(1)
                )
                if artifact is None:
                    continue
                payload = dict(artifact.metadata_json or {}).get("result")
                if not isinstance(payload, dict):
                    continue
                try:
                    decision = AccountScopeDecision.model_validate(payload)
                except ValueError:
                    continue
                if decision.account_user_id == account_user_id:
                    return decision
        return None

    def _require_account_evidence_refs(
        self, account_user_id: str, evidence_refs: list[str]
    ) -> None:
        if not evidence_refs or len(evidence_refs) != len(set(evidence_refs)):
            raise ValueError("account scope decision requires distinct evidence refs")
        with self.job_service.database.session() as session:
            rows = session.execute(
                select(JobArtifactRecord, JobRecord)
                .join(JobRecord, JobRecord.id == JobArtifactRecord.job_id)
                .where(JobArtifactRecord.path.in_(evidence_refs))
            ).all()
        matched: set[str] = set()
        for artifact, job in rows:
            if job.input_data.get("account_user_id") == account_user_id:
                matched.add(artifact.path)
        if matched != set(evidence_refs):
            raise ValueError("account scope evidence refs do not belong to the account")

    def _persist_account_scope_decision(
        self,
        job_id: str,
        *,
        account_user_id: str,
        classification: Literal[
            "unknown", "in_scope", "out_of_scope_physical", "needs_human"
        ],
        decision_source: Literal["rule", "model", "human", "chatgpt"],
        reason: str,
        evidence_refs: list[str],
    ) -> AccountScopeDecision:
        decision = AccountScopeDecision(
            job_id=job_id,
            account_user_id=account_user_id,
            classification=classification,
            decision_source=decision_source,
            reason=reason,
            decided_at=datetime.now(UTC).isoformat(),
            evidence_refs=evidence_refs,
        )
        relative = Path("evidence") / "shops" / job_id / "account-scope.json"
        absolute = self.job_service.runtime_dir / relative
        payload = decision.model_dump(mode="json")
        absolute.parent.mkdir(parents=True, exist_ok=True)
        _write_json_evidence_once(absolute, payload)
        digest = hashlib.sha256(absolute.read_bytes()).hexdigest()
        self.job_service.attach_artifact_once(
            job_id,
            kind=ACCOUNT_SCOPE_ARTIFACT_KIND,
            producer=ACCOUNT_SCOPE_ARTIFACT_PRODUCER,
            path=relative.as_posix(),
            metadata={"schema_version": 1, "sha256": digest, "result": payload},
        )
        return decision

    def _classify_preflight_items(
        self, job_id: str, items: list[CollectionItem]
    ) -> ShopScopeDecision:
        discoveries = self.list_discoveries(job_id)
        refs = [item.artifact_path for item in discoveries]
        descriptions = [
            " ".join(
                str(item.data.get(key) or "")
                for key in ("price", "sold", "rank", "discount")
            ).strip()
            for item in items[:3]
        ]
        evidence = ShopScopeEvidence(
            shop_profile="android_preflight_observation",
            representative_product_titles=[
                str(item.raw_evidence.get("title") or item.data.get("title") or "未识别商品")
                for item in items[:3]
            ],
            visible_descriptions=[item for item in descriptions if item],
            evidence_refs=refs,
        )
        return classify_shop_scope(evidence, model_adapter=self.scope_model_adapter)

    def _persist_scope_gate(
        self,
        job_id: str,
        decision: ShopScopeDecision,
        *,
        account_user_id: str,
        items: list[CollectionItem],
    ) -> None:
        relative = Path("evidence") / "shops" / job_id / "scope-gate.json"
        artifact_path = self.job_service.runtime_dir / relative
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "job_id": job_id,
            "account_user_id": account_user_id,
            "classification": decision.classification,
            "reason": decision.reason,
            "representative_product_count": decision.representative_product_count,
            "deep_collection_allowed": decision.deep_collection_allowed,
            "decision_source": decision.decision_source,
            "decided_at": datetime.now(UTC).isoformat(),
            "evidence_refs": decision.evidence_refs,
            "evidence": decision.evidence.model_dump(mode="json"),
            "representative_products": [item.model_dump(mode="json") for item in items],
        }
        _write_json_evidence_once(artifact_path, payload)
        payload_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        self.job_service.attach_artifact(
            job_id,
            kind="shop_scope_gate_result",
            path=relative.as_posix(),
            metadata={"result": payload, "sha256": payload_sha256},
        )
        self._persist_account_scope_decision(
            job_id,
            account_user_id=account_user_id,
            classification=decision.classification,
            decision_source=decision.decision_source,
            reason=decision.reason,
            evidence_refs=decision.evidence_refs,
        )

    def _require_trusted_in_scope_gate(self, payload: ShopCollectionCreate) -> None:
        assert payload.scope_gate_job_id is not None
        try:
            gate = self.job_service.get(payload.scope_gate_job_id)
        except JobNotFound as error:
            raise ValueError("full_shop requires a trusted in_scope gate job") from error
        artifact = next(
            (
                item
                for item in gate.artifacts
                if item.kind == "shop_scope_gate_result"
            ),
            None,
        )
        result = artifact.metadata.get("result") if artifact is not None else None
        artifact_path = (
            _resolved_regular_file(
                self.job_service.runtime_dir / artifact.path,
                roots=(self.job_service.runtime_dir,),
            )
            if artifact is not None
            else None
        )
        artifact_payload: dict[str, Any] | None = None
        if artifact_path is not None and not artifact_path.is_symlink():
            try:
                loaded = _load_json(artifact_path, "scope-gate.json")
            except ValueError:
                loaded = None
            if isinstance(loaded, dict):
                artifact_payload = loaded
        trusted_artifact = (
            artifact_path is not None
            and artifact_payload == result
            and artifact is not None
            and artifact.metadata.get("sha256")
            == hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        )
        if (
            gate.type != ANDROID_SHOP_JOB_TYPE
            or gate.state is not JobState.succeeded
            or gate.input.get("account_user_id") != payload.account_user_id
            or gate.input.get("collection_mode") != "preflight"
            or not trusted_artifact
        ):
            if gate.input.get("account_user_id") != payload.account_user_id:
                raise ValueError("scope gate account does not match collection account")
            raise ValueError("full_shop requires a trusted in_scope gate job")
        if not isinstance(result, dict) or result.get("classification") != "in_scope":
            raise ValueError("full_shop requires a trusted in_scope gate job")

    def execute(
        self, job_id: str, payload: ShopCollectionCreate
    ) -> ShopCollectionRead | None:
        try:
            return self._execute_claimed(job_id, payload)
        finally:
            self._mark_job_finished(job_id)

    def _execute_claimed(
        self, job_id: str, payload: ShopCollectionCreate
    ) -> ShopCollectionRead | None:
        try:
            self.job_service.claim(job_id)
        except InvalidJobTransition:
            return None
        try:
            result = self.device_adapter.collect_shop(
                CollectionRequest(
                    capability="shop_products",
                    parameters={
                        "account_user_id": payload.account_user_id,
                        "account_name": payload.account_name,
                        "device_id": payload.device_id,
                        "collection_mode": payload.collection_mode,
                        "selector_profile_version": payload.selector_profile_version,
                        "job_id": job_id,
                        "is_cancelled": self._cancellation_callback(job_id),
                        "preflight_scope_evaluator": (
                            lambda items: self._preflight_scope_result(job_id, items)
                            if payload.collection_mode == "preflight"
                            else None
                        ),
                    },
                    expected_count=payload.expected_count,
                )
            )
        except Exception as error:
            self.job_service.append_log(
                job_id,
                level="error",
                message=f"Shop collection raised: {type(error).__name__}: {error}",
            )
            if self.job_service.get(job_id).state is JobState.running:
                self.job_service.transition(
                    job_id,
                    JobState.failed,
                    current_stage="shop_failed",
                    error_category="shop_collection_failed",
                )
            return None

        try:
            return self._complete_execution(job_id, payload, result)
        except Exception as error:
            self.job_service.append_log(
                job_id,
                level="error",
                message=(
                    "Shop collection background execution raised: "
                    f"{type(error).__name__}: {error}"
                ),
            )
            if self.job_service.get(job_id).state is JobState.running:
                self.job_service.transition(
                    job_id,
                    JobState.failed,
                    current_stage="shop_failed",
                    error_category="shop_collection_failed",
                )
            return None

    def _preflight_scope_result(
        self, job_id: str, items: list[CollectionItem]
    ) -> dict[str, Any] | None:
        decision = self._classify_preflight_items(job_id, items)
        if decision.classification == "needs_human":
            return None
        return decision.model_dump(mode="json")

    def _complete_execution(
        self,
        job_id: str,
        payload: ShopCollectionCreate,
        result: CollectionResult,
    ) -> ShopCollectionRead:
        short_natural_end_sample = bool(
            payload.collection_mode == "evidence_sample"
            and result.status == "succeeded"
            and result.complete
            and result.expected_count in {1, 2}
            and result.succeeded_count == result.expected_count
            and len(result.items) == result.expected_count
            and result.raw_evidence.get("natural_end_reached") is True
        )
        effective_expected_count = (
            result.expected_count if short_natural_end_sample else payload.expected_count
        )
        effective_available_count = (
            effective_expected_count
            if short_natural_end_sample
            else payload.available_count_observed
        )
        read = _shop_collection_read(
            job_id=job_id,
            selector_profile_version=payload.selector_profile_version,
            expected_count=effective_expected_count,
            result=result,
            collection_mode=payload.collection_mode,
            product_sample_limit=payload.product_sample_limit,
            available_count_observed=effective_available_count,
            test_override=payload.test_override,
            test_override_reason=payload.test_override_reason,
        )
        if short_natural_end_sample:
            read = read.model_copy(update={"natural_end_reached": True})
        scope_decision: ShopScopeDecision | None = None
        if payload.collection_mode in {"preflight", "bounded_sample"} and result.items:
            early_decision = result.raw_evidence.get("scope_early_stop")
            if payload.collection_mode == "preflight" and isinstance(
                early_decision, dict
            ):
                scope_decision = ShopScopeDecision.model_validate(early_decision)
            else:
                scope_decision = self._classify_preflight_items(
                    job_id, result.items[:3]
                )
            self._persist_scope_gate(
                job_id,
                scope_decision,
                account_user_id=payload.account_user_id,
                items=result.items[:3],
            )
            read = read.model_copy(
                update={
                    "scope_classification": scope_decision.classification,
                    "scope_reason": scope_decision.reason,
                }
            )
        if self.job_service.get(job_id).state is JobState.cancelled:
            read = _cancelled_shop_read(read)
        elif result.status == "succeeded" and payload.collection_mode == "preflight":
            if scope_decision is None or scope_decision.classification == "needs_human":
                read = read.model_copy(
                    update={
                        "status": "needs_human",
                        "detail": "shop_scope_needs_human",
                        "complete": False,
                    }
                )
            else:
                read = read.model_copy(update={"complete": False})
        elif result.status == "succeeded" and payload.collection_mode in {
            "bounded_sample",
            "evidence_sample",
        }:
            evidence_error = (
                "evidence_sample_invalid"
                if payload.collection_mode == "evidence_sample"
                else "bounded_sample_evidence_invalid"
            )
            try:
                binding = self._materialize_bounded_sample_evidence(
                    job_id, result.items, expected_count=effective_expected_count
                )
                read = read.model_copy(
                    update={
                        "sample_manifest_path": binding.manifest_path,
                        "sample_manifest_sha256": binding.manifest_sha256,
                        "sample_collection_path": binding.collection_path,
                        "sample_collection_sha256": binding.collection_sha256,
                    }
                )
                verification = verify_shop_collection(
                    binding.verification_dir,
                    expected_count=effective_expected_count,
                    expected_source_urls=[str(item.source_url) for item in result.items],
                )
            except (InvalidVerificationPath, ValueError) as error:
                self.job_service.append_log(
                    job_id,
                    level="warning",
                    message=(
                        "Bounded sample evidence failed closed: "
                        f"{type(error).__name__}: {error}"
                    ),
                )
                read = read.model_copy(
                    update={
                        "status": "needs_human",
                        "detail": evidence_error,
                        "complete": False,
                    }
                )
            else:
                verified_missing = [
                    ShopCollectionMissing(
                        reference=item.reference,
                        reason=item.reason,
                        raw_evidence={"product_dir": item.product_dir},
                    )
                    for item in verification.missing_items
                ]
                read = read.model_copy(
                    update={
                        "verification": verification,
                        "succeeded_count": verification.succeeded_count,
                        "missing_count": len(verified_missing),
                        "missing_items": verified_missing,
                        "complete": verification.complete,
                    }
                )
                if not verification.complete:
                    read = read.model_copy(
                        update={
                            "status": "needs_human",
                            "detail": evidence_error,
                            "complete": False,
                        }
                    )
        elif result.status == "succeeded" and payload.verification_dir is None:
            read = _verification_required(read)
        elif result.status == "succeeded":
            try:
                verification_dir = self._resolve_verification_dir(
                    payload.verification_dir
                )
                assert verification_dir is not None
                verification = verify_shop_collection(
                    verification_dir,
                    expected_count=payload.expected_count,
                    expected_source_urls=[str(item.source_url) for item in result.items],
                    source_identity_resolver=_resolve_xhs_goods_identity,
                )
            except (InvalidVerificationPath, ValueError) as error:
                self.job_service.append_log(
                    job_id,
                    level="warning",
                    message=(
                        "Product evidence verification failed: "
                        f"{type(error).__name__}: {error}"
                    ),
                )
                read = read.model_copy(
                    update={
                        "status": "needs_human",
                        "detail": "product_evidence_verification_failed",
                        "complete": False,
                    }
                )
            else:
                source_mismatch = (
                    "collected_source_url_mismatch" in verification.issues
                )
                if source_mismatch:
                    verified_missing = [
                        ShopCollectionMissing(
                            reference=str(item.source_url),
                            reason="collected_source_url_mismatch",
                            raw_evidence={
                                "collected_item_id": item.id,
                                "verification_issues": verification.issues,
                            },
                        )
                        for item in result.items
                    ]
                    verified_succeeded_count = 0
                else:
                    verified_missing = [
                        ShopCollectionMissing(
                            reference=item.reference,
                            reason=item.reason,
                            raw_evidence={"product_dir": item.product_dir},
                        )
                        for item in verification.missing_items
                    ]
                    verified_succeeded_count = verification.succeeded_count
                read = read.model_copy(
                    update={
                        "verification": verification,
                        "succeeded_count": verified_succeeded_count,
                        "missing_count": len(verified_missing),
                        "missing_items": verified_missing,
                        "complete": verification.complete,
                    }
                )
                if not verification.complete:
                    read = read.model_copy(
                        update={
                            "status": "needs_human",
                            "detail": "product_evidence_verification_failed",
                            "complete": False,
                        }
                    )
        if payload.collection_mode in {"bounded_sample", "evidence_sample"}:
            read = read.model_copy(
                update={
                    "sample_complete": bool(
                        read.status == "succeeded"
                        and read.succeeded_count == effective_expected_count
                        and read.missing_count == 0
                    ),
                    "shop_complete": False,
                    "complete": False,
                }
            )
        elif payload.collection_mode != "preflight":
            read = read.model_copy(
                update={"shop_complete": read.complete, "sample_complete": False}
            )
        if self.job_service.get(job_id).state is JobState.cancelled:
            read = _cancelled_shop_read(read)
        pending = self._persist_result(job_id, read)
        finalized = self._finalize(job_id, read, pending)
        if finalized is None:
            durable = self.job_service.get(job_id)
            if durable.state is JobState.cancelled:
                return _cancelled_shop_read(read)
            return read.model_copy(
                update={
                    "status": "needs_human",
                    "detail": durable.error_category or "job_state_changed",
                    "complete": False,
                }
            )
        return read

    def close(self) -> None:
        with self._lifecycle_lock:
            if not self._accepting_work:
                return
            self._accepting_work = False
            active = list(self._cancel_events.items())
            futures = list(self._futures.values())
            for _, cancel_event in active:
                cancel_event.set()
        for future in futures:
            future.cancel()
        for job_id, _ in active:
            try:
                current = self.job_service.get(job_id)
                if current.state in {JobState.queued, JobState.running}:
                    self.job_service.transition(
                        job_id,
                        JobState.cancelled,
                        current_stage="worker_shutdown_cancelled",
                        error_category="worker_shutdown_cancelled",
                    )
            except InvalidJobTransition:
                pass
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)

    def _cancellation_callback(self, job_id: str) -> Callable[[], bool]:
        with self._lifecycle_lock:
            cancel_event = self._cancel_events.get(job_id)
        return cancel_event.is_set if cancel_event is not None else lambda: True

    def _mark_job_finished(self, job_id: str) -> None:
        with self._lifecycle_lock:
            self._futures.pop(job_id, None)
            self._cancel_events.pop(job_id, None)

    def _resolve_verification_dir(self, value: str | None) -> Path | None:
        if value is None:
            return None
        relative = Path(value)
        if relative.is_absolute():
            raise InvalidVerificationPath(
                "Verification directory must be relative to runtime storage."
            )
        try:
            resolved = (self.job_service.runtime_dir / relative).resolve(strict=True)
            resolved.relative_to(self.job_service.runtime_dir)
        except (OSError, ValueError) as error:
            raise InvalidVerificationPath(
                "Verification directory escapes or is missing from runtime storage."
            ) from error
        if not resolved.is_dir():
            raise InvalidVerificationPath("Verification directory is not a directory.")
        return resolved

    def _materialize_bounded_sample_evidence(
        self,
        job_id: str,
        items: list[CollectionItem],
        *,
        expected_count: int,
    ) -> _BoundedSampleEvidenceBinding:
        """Bind the accepted sample to its real, same-job detail screenshots."""
        if len(items) != expected_count:
            raise ValueError("bounded sample product count does not match its exact target")
        job = self.job_service.get(job_id)
        screenshot_artifacts = {
            artifact.path: artifact
            for artifact in job.artifacts
            if artifact.kind == "android_screenshot"
        }
        sample_dir = (
            self.job_service.runtime_dir
            / "evidence"
            / "shops"
            / job_id
            / "sample-products"
        ).resolve()
        sample_dir.relative_to(self.job_service.runtime_dir)
        sample_dir.mkdir(parents=True, exist_ok=True)
        products: list[dict[str, Any]] = []
        sha_manifest: list[dict[str, str]] = []
        for index, item in enumerate(items, start=1):
            detail_screen = item.raw_evidence.get("detail_screen")
            if not isinstance(detail_screen, dict):
                raise ValueError("detail_screen evidence is missing")
            transition = detail_screen.get("transition")
            declared_sha = detail_screen.get("screenshot_sha256")
            paths = detail_screen.get("artifacts")
            if (
                not isinstance(transition, str)
                or not transition
                or not isinstance(declared_sha, str)
                or len(declared_sha) != 64
                or not isinstance(paths, list)
            ):
                raise ValueError("detail_screen evidence is malformed")
            matches = [
                path
                for path in paths
                if isinstance(path, str) and path in screenshot_artifacts
            ]
            if len(matches) != 1:
                raise ValueError("detail screenshot is not bound to this job")
            artifact = screenshot_artifacts[matches[0]]
            if (
                artifact.metadata.get("transition") != transition
                or artifact.metadata.get("sha256") != declared_sha
            ):
                raise ValueError("detail screenshot metadata does not match")
            source = _resolved_regular_file(
                self.job_service.runtime_dir / artifact.path,
                roots=(self.job_service.runtime_dir,),
            )
            if source is None or source.is_symlink():
                raise ValueError("detail screenshot is not a contained regular file")
            actual_sha = hashlib.sha256(source.read_bytes()).hexdigest()
            if actual_sha != declared_sha:
                raise ValueError("detail screenshot hash does not match")
            product_dir = sample_dir / f"{index:02d}_{item.id[:24]}"
            image_dir = product_dir / "images"
            image_dir.mkdir(parents=True, exist_ok=True)
            destination = image_dir / "detail.png"
            if destination.exists() or destination.is_symlink():
                existing = _resolved_regular_file(
                    destination, roots=(self.job_service.runtime_dir, sample_dir)
                )
                if existing is None or existing.is_symlink():
                    raise ValueError("existing bounded screenshot path is invalid")
                if hashlib.sha256(existing.read_bytes()).hexdigest() != actual_sha:
                    raise ValueError("existing bounded screenshot must not be overwritten")
            else:
                shutil.copyfile(source, destination)
            copied_sha = hashlib.sha256(destination.read_bytes()).hexdigest()
            if copied_sha != actual_sha:
                raise ValueError("copied detail screenshot hash does not match")
            source_url = str(item.source_url)
            detail = {
                "link": source_url,
                "title": item.raw_evidence.get("title"),
                "image_manifest": [
                    {"file": "images/detail.png", "sha256": copied_sha}
                ],
            }
            _write_json_evidence_once(product_dir / "detail.json", detail)
            products.append(
                {"source_url": source_url, "product_dir": product_dir.name}
            )
            sha_manifest.append(
                {
                    "source_url": source_url,
                    "image": f"{product_dir.name}/images/detail.png",
                    "sha256": copied_sha,
                }
            )
        _write_json_evidence_once(
            sample_dir / "collection.json",
            {"unique_product_link_count": expected_count, "products": products},
        )
        _write_json_evidence_once(
            sample_dir / "manifest.json", {"products": sha_manifest}
        )
        manifest_path = sample_dir / "manifest.json"
        collection_path = sample_dir / "collection.json"
        return _BoundedSampleEvidenceBinding(
            verification_dir=sample_dir,
            manifest_path=manifest_path.relative_to(
                self.job_service.runtime_dir
            ).as_posix(),
            manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            collection_path=collection_path.relative_to(
                self.job_service.runtime_dir
            ).as_posix(),
            collection_sha256=hashlib.sha256(collection_path.read_bytes()).hexdigest(),
        )

    def _persist_result(
        self, job_id: str, result: ShopCollectionRead
    ) -> _PendingShopResult:
        relative = Path("evidence") / "shops" / job_id / "result.json"
        temp_relative = relative.with_name(f".result-{uuid4().hex}.tmp")
        absolute = (self.job_service.runtime_dir / relative).resolve()
        temp_absolute = (self.job_service.runtime_dir / temp_relative).resolve()
        try:
            absolute.relative_to(self.job_service.runtime_dir)
            temp_absolute.relative_to(self.job_service.runtime_dir)
        except ValueError as error:
            raise RuntimeError("Shop result path escapes runtime storage.") from error
        temp_absolute.parent.mkdir(parents=True, exist_ok=True)
        payload = result.model_dump(mode="json")
        temp_absolute.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return _PendingShopResult(
            temp_path=temp_relative.as_posix(),
            final_path=relative.as_posix(),
            payload=payload,
        )

    def _finalize(
        self,
        job_id: str,
        result: ShopCollectionRead,
        pending: _PendingShopResult,
    ) -> Any | None:
        if result.status == "succeeded":
            state = JobState.succeeded
            current_stage = (
                (
                    "shop_evidence_sample_complete"
                    if result.collection_mode == "evidence_sample"
                    else "shop_sample_complete"
                )
                if result.collection_mode in {"bounded_sample", "evidence_sample"}
                else (
                    f"scope_gate_{result.scope_classification}"
                    if result.collection_mode == "preflight"
                    else "shop_complete"
                )
            )
            error_category = None
        elif result.status == "failed":
            state = JobState.failed
            current_stage = "shop_failed"
            error_category = result.detail or "shop_collection_failed"
        else:
            state = JobState.needs_human
            current_stage = "shop_needs_human"
            error_category = result.detail or result.status
        return self.job_service.finalize_running_with_artifact(
            job_id,
            state=state,
            progress_current=(
                result.collected_count
                if result.collection_mode == "preflight"
                else result.succeeded_count
            ),
            progress_total=result.expected_count,
            current_stage=current_stage,
            error_category=error_category,
            kind="shop_collection_result",
            temp_path=pending.temp_path,
            path=pending.final_path,
            metadata={"result": pending.payload},
        )


def verify_shop_collection(
    account_dir: Path | str,
    *,
    expected_count: int,
    expected_source_urls: list[str] | None = None,
    source_identity_resolver: Callable[[str], str] | None = None,
) -> ShopVerificationResult:
    """Verify exact product evidence without converting partial artifacts into N/N."""
    if isinstance(expected_count, bool) or expected_count < 0:
        raise ValueError("expected_count must be a non-negative integer.")
    try:
        account_path = Path(account_dir).resolve(strict=True)
    except OSError as error:
        raise ValueError(f"account directory cannot be resolved: {account_dir}") from error
    if not _is_regular_directory(account_path, roots=(account_path,)):
        raise ValueError(f"account directory is not a directory: {account_path}")
    collection_path = _resolved_regular_file(
        account_path / "collection.json", roots=(account_path,)
    )
    if collection_path is None:
        raise ValueError("collection.json is not a contained regular file.")
    collection = _load_json(collection_path, "collection.json")
    entries = collection.get("products")
    if not isinstance(entries, list):
        raise ValueError("collection.products must be a list.")

    issues: list[str] = []
    declared_count = collection.get("unique_product_link_count")
    if (
        isinstance(declared_count, bool)
        or not isinstance(declared_count, int)
        or declared_count != len(entries)
    ):
        issues.append("declared_discovered_count_mismatch")
    if declared_count != expected_count:
        issues.append("declared_expected_count_mismatch")

    missing: list[ShopVerificationMissing] = []
    seen_urls: set[str] = set()
    seen_dirs: set[str] = set()
    discovered_urls: list[str] = []
    succeeded = 0
    for index, entry in enumerate(entries):
        reference = f"collection_product:{index + 1}"
        if not isinstance(entry, dict):
            missing.append(
                ShopVerificationMissing(
                    reference=reference, reason="invalid_product_entry"
                )
            )
            continue
        source_url = _text(entry.get("source_url"))
        product_dir = _text(entry.get("product_dir"))
        if source_url is None or not _is_https_url(source_url):
            missing.append(
                ShopVerificationMissing(
                    reference=reference,
                    reason="invalid_source_url",
                    product_dir=product_dir,
                )
            )
            continue
        discovered_urls.append(source_url)
        if source_url in seen_urls:
            missing.append(
                ShopVerificationMissing(
                    reference=source_url,
                    reason="duplicate_source_url",
                    product_dir=product_dir,
                )
            )
            continue
        seen_urls.add(source_url)
        if product_dir is None or not _safe_product_dir(product_dir):
            missing.append(
                ShopVerificationMissing(
                    reference=source_url,
                    reason="invalid_product_dir",
                    product_dir=product_dir,
                )
            )
            continue
        if product_dir in seen_dirs:
            missing.append(
                ShopVerificationMissing(
                    reference=source_url,
                    reason="duplicate_product_dir",
                    product_dir=product_dir,
                )
            )
            continue
        seen_dirs.add(product_dir)
        product_path = _resolved_directory(
            account_path / product_dir, roots=(account_path,)
        )
        if product_path is None:
            missing.append(
                ShopVerificationMissing(
                    reference=source_url,
                    reason="invalid_product_dir",
                    product_dir=product_dir,
                )
            )
            continue
        reason = _verify_product(product_path, source_url, account_path=account_path)
        if reason is not None:
            missing.append(
                ShopVerificationMissing(
                    reference=source_url,
                    reason=reason,
                    product_dir=product_dir,
                )
            )
            continue
        succeeded += 1

    discovered = len(entries)
    if expected_source_urls is not None:
        discovered_identities = discovered_urls
        expected_identities = expected_source_urls
        if source_identity_resolver is not None:
            try:
                discovered_identities = [
                    source_identity_resolver(url) for url in discovered_urls
                ]
                expected_identities = [
                    source_identity_resolver(url) for url in expected_source_urls
                ]
            except (ValueError, httpx.HTTPError):
                discovered_identities = []
                expected_identities = expected_source_urls
        if (
            len(discovered_identities) != len(expected_identities)
            or len(set(discovered_identities)) != len(discovered_identities)
            or set(discovered_identities) != set(expected_identities)
        ):
            issues.append("collected_source_url_mismatch")
    for index in range(discovered, expected_count):
        missing.append(
            ShopVerificationMissing(
                reference=f"expected_product:{index + 1}",
                reason="expected_product_not_discovered",
            )
        )
    overflow = max(discovered - expected_count, 0)
    complete = (
        not issues
        and overflow == 0
        and discovered == expected_count
        and succeeded == expected_count
        and not missing
    )
    return ShopVerificationResult(
        expected_count=expected_count,
        discovered_count=discovered,
        succeeded_count=succeeded,
        missing_count=len(missing),
        missing_items=missing,
        overflow_count=overflow,
        issues=issues,
        complete=complete,
    )


def _resolve_xhs_goods_identity(source_url: str) -> str:
    """Resolve a public share URL to its stable XHS goods id without credentials."""
    allowed_hosts = {"xhslink.com", "www.xiaohongshu.com"}
    current = source_url
    with httpx.Client(
        follow_redirects=False,
        timeout=10,
        headers={"User-Agent": "Mozilla/5.0"},
    ) as client:
        for _ in range(6):
            parsed = urlsplit(current)
            if (
                parsed.scheme != "https"
                or (parsed.hostname or "").lower() not in allowed_hosts
            ):
                raise ValueError("unsupported_xhs_product_url")
            with client.stream("GET", current) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("xhs_product_redirect_missing")
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                match = re.fullmatch(r"/goods-detail/([0-9a-f]{24})", parsed.path)
                if match is None:
                    raise ValueError("xhs_product_identity_missing")
                return f"xhs-goods:{match.group(1)}"
    raise ValueError("xhs_product_redirect_limit")


def _shop_collection_read(
    *,
    job_id: str,
    selector_profile_version: str,
    expected_count: int,
    result: CollectionResult,
    collection_mode: Literal[
        "preflight",
        "legacy_full_shop",
        "bounded_sample",
        "evidence_sample",
        "full_shop",
    ],
    product_sample_limit: int | None,
    available_count_observed: int | None,
    test_override: bool,
    test_override_reason: str | None,
) -> ShopCollectionRead:
    collection_missing_items = [
        ShopCollectionMissing(
            reference=item.reference,
            reason=item.reason,
            raw_evidence=item.raw_evidence,
        )
        for item in result.missing_items
    ]
    verification_reason = (
        "product_evidence_verification_pending"
        if result.status == "succeeded"
        else result.detail or "product_evidence_unavailable"
    )
    missing_items = [
        ShopCollectionMissing(
            reference=f"expected_product:{index + 1}",
            reason=verification_reason,
            raw_evidence={
                "job_id": job_id,
                "collection_status": result.status,
            },
        )
        for index in range(expected_count)
    ]
    return ShopCollectionRead(
        job_id=job_id,
        status=result.status,
        detail=result.detail,
        selector_profile_version=selector_profile_version,
        expected_count=expected_count,
        discovered_count=len(result.items),
        collected_count=result.succeeded_count,
        raw_observation_count=int(result.raw_observation_count or 0),
        duplicate_observation_count=result.duplicate_observation_count,
        succeeded_count=0,
        missing_count=len(missing_items),
        missing_items=missing_items,
        collection_missing_count=len(collection_missing_items),
        collection_missing_items=collection_missing_items,
        rejected_count=len(result.rejected_items),
        rejected_items=list(result.rejected_items),
        overflow_count=result.overflow_count,
        evidence_artifacts=result.evidence_artifacts,
        items=result.items,
        complete=False,
        collection_mode=collection_mode,
        product_sample_limit=product_sample_limit,
        available_count_observed=available_count_observed,
        test_override=test_override,
        test_override_reason=test_override_reason,
    )


def _verification_required(result: ShopCollectionRead) -> ShopCollectionRead:
    return result.model_copy(
        update={
            "status": "needs_human",
            "detail": "product_evidence_verification_pending",
            "complete": False,
        }
    )


def _cancelled_shop_read(result: ShopCollectionRead) -> ShopCollectionRead:
    return result.model_copy(
        update={"status": "failed", "detail": "cancelled", "complete": False}
    )


def _verify_product(
    product_dir: Path, source_url: str, *, account_path: Path
) -> str | None:
    unresolved_detail = product_dir / "detail.json"
    if not unresolved_detail.exists() and not unresolved_detail.is_symlink():
        return "detail_json_missing"
    detail_path = _resolved_regular_file(
        unresolved_detail, roots=(account_path, product_dir)
    )
    if detail_path is None:
        return "detail_json_invalid_path"
    try:
        detail = _load_json(detail_path, "detail.json")
    except ValueError:
        return "detail_json_invalid"
    if _text(detail.get("link")) != source_url:
        return "detail_url_mismatch"
    unresolved_images_dir = product_dir / "images"
    if not unresolved_images_dir.exists() and not unresolved_images_dir.is_symlink():
        return "images_directory_missing"
    images_dir = _resolved_directory(
        unresolved_images_dir, roots=(account_path, product_dir)
    )
    if images_dir is None:
        return "images_directory_invalid_path"
    images: list[Path] = []
    for candidate in sorted(images_dir.iterdir()):
        if candidate.suffix.lower() not in _IMAGE_EXTENSIONS:
            continue
        image = _resolved_regular_file(
            candidate, roots=(account_path, product_dir, images_dir)
        )
        if image is None:
            return "image_file_invalid_path"
        images.append(image)
    if not images:
        return "local_images_missing"
    manifest = detail.get("image_manifest")
    if not isinstance(manifest, list):
        return "image_manifest_missing"
    records: dict[str, tuple[str, Path]] = {}
    for item in manifest:
        if not isinstance(item, dict):
            return "image_manifest_invalid"
        relative = _text(item.get("file"))
        digest = _text(item.get("sha256"))
        if relative is None or digest is None or relative in records:
            return "image_manifest_invalid"
        manifest_path = PurePosixPath(relative)
        if (
            "\\" in relative
            or manifest_path.is_absolute()
            or relative != manifest_path.as_posix()
            or len(manifest_path.parts) != 2
            or manifest_path.parts[0] != "images"
            or any(part in {".", ".."} for part in manifest_path.parts)
        ):
            return "image_manifest_invalid_path"
        resolved_manifest_path = _resolved_regular_file(
            product_dir.joinpath(*manifest_path.parts),
            roots=(account_path, product_dir, images_dir),
        )
        if resolved_manifest_path is None:
            return "image_manifest_invalid_path"
        records[relative] = (digest, resolved_manifest_path)
    expected_files = {f"images/{image.name}" for image in images}
    if set(records) != expected_files:
        return "image_manifest_incomplete"
    for image in images:
        relative = f"images/{image.name}"
        digest, manifest_image = records[relative]
        if manifest_image != image:
            return "image_manifest_invalid_path"
        if hashlib.sha256(image.read_bytes()).hexdigest() != digest:
            return "image_hash_mismatch"
    return None


def _resolved_regular_file(path: Path, *, roots: tuple[Path, ...]) -> Path | None:
    try:
        resolved = path.resolve(strict=True)
        mode = resolved.stat().st_mode
        for root in roots:
            resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    return resolved if stat.S_ISREG(mode) else None


def _resolved_directory(path: Path, *, roots: tuple[Path, ...]) -> Path | None:
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    return resolved if _is_regular_directory(resolved, roots=roots) else None


def _is_regular_directory(path: Path, *, roots: tuple[Path, ...]) -> bool:
    try:
        mode = path.stat().st_mode
        for root in roots:
            path.relative_to(root)
    except (OSError, ValueError):
        return False
    return stat.S_ISDIR(mode)


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} cannot be read: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return value


def _write_json_evidence_once(path: Path, payload: dict[str, Any]) -> None:
    """Create derived evidence once; retries may reuse identical bytes only."""
    if path.exists() or path.is_symlink():
        existing = _resolved_regular_file(path, roots=(path.parent,))
        if existing is None or existing.is_symlink():
            raise ValueError("existing evidence path is invalid")
        if _load_json(existing, path.name) != payload:
            raise ValueError("existing evidence must not be overwritten")
        return
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _safe_product_dir(value: str) -> bool:
    candidate = Path(value)
    return (
        value not in {".", ".."}
        and candidate.name == value
        and "/" not in value
        and "\\" not in value
    )


def _is_https_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
        return (
            parts.scheme.lower() == "https"
            and parts.hostname is not None
            and parts.username is None
            and parts.password is None
            and parts.port is None
            and not parts.fragment
        )
    except (TypeError, ValueError, UnicodeError):
        return False


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None
