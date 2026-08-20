"""Ground model output in persisted evidence before any successful write."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import selectinload

from backend.app.adapters.contracts import (
    CollectionItem,
    CollectionResult,
    ModelAdapterError,
    StructuredModelRequest,
)
from backend.app.db import Database, canonical_raw_evidence_digest
from backend.app.features.analysis.models import AnalysisRecord, OpportunityRecord
from backend.app.features.analysis.schemas import (
    AnalysisCreate,
    AnalysisEvidenceRead,
    AnalysisOutput,
    AnalysisRead,
    OpportunityReviewCreate,
    OpportunityRead,
)
from backend.app.features.radar.models import RankItemRecord
from backend.app.features.shops.service import ANDROID_SHOP_JOB_TYPES, ShopCollectionRead
from backend.app.features.xhs.constants import (
    ACCOUNT_COLLECTION_ARTIFACT_KIND,
    ACCOUNT_COLLECTION_ARTIFACT_PRODUCER,
    ACCOUNT_COLLECTION_JOB_TYPE,
)
from backend.app.features.xhs.models import (
    XhsAccountNoteRecord,
    XhsAccountProfileRecord,
    XhsAccountProfileSnapshotRecord,
    XhsAccountSnapshotNoteRecord,
    XhsArtifactPromotionJournalRecord,
)
from backend.app.features.xhs.ownership import OwnerIdentityError, canonical_owner_id
from backend.app.features.xhs.schemas import (
    NOTE_PUBLIC_COUNTER_FIELDS,
    PROFILE_PUBLIC_COUNTER_FIELDS,
    public_counter_json_matches,
    public_counter_values,
)
from backend.app.features.xhs.staging_cleanup import sqlite_file_device_identity
from backend.app.models.jobs import JobArtifactRecord
from backend.app.models.jobs import JobState


PROMPT_VERSION = "tutorial-demand-radar-grounded-v1"
MAX_TRUSTED_RESULT_BYTES = 5 * 1024 * 1024
MAX_TRUSTED_ACCOUNT_RESULT_BYTES = 20 * 1024 * 1024
MAX_SQLITE_ID = 9_223_372_036_854_775_807
XHS_RAW_TRUST_ANCHOR_KINDS = frozenset(
    {ACCOUNT_COLLECTION_ARTIFACT_KIND, "xhs_note_search_raw"}
)


class EvidenceNotFound(ValueError):
    pass


class EvidenceAccountMismatch(ValueError):
    pass


class AnalysisNotFound(LookupError):
    pass


class OpportunityNotFound(LookupError):
    pass


class OpportunityStateError(ValueError):
    pass


class AnalysisCommitRolledBack(RuntimeError):
    pass


class AnalysisTransactionUnknown(RuntimeError):
    pass


class _AnalysisCommitOutcome(Enum):
    committed = "committed"
    rolled_back = "rolled_back"
    unknown = "unknown"


@dataclass(frozen=True)
class _ContainedFileSnapshot:
    payload: bytes
    identity: tuple[int, int, int, int]


@dataclass(frozen=True)
class _ArtifactBinding:
    evidence_id: str
    artifact_id: int
    artifact_job_id: str
    relative_path: str
    sha256: str
    size_bytes: int
    file_identity: tuple[int, int, int, int]

    def snapshot_value(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "artifact_id": self.artifact_id,
            "artifact_job_id": self.artifact_job_id,
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "file_identity": list(self.file_identity),
        }


@dataclass(frozen=True)
class _EvidenceResolution:
    facts: list[dict[str, Any]]
    trust: list[dict[str, Any]]
    artifact_bindings: tuple[_ArtifactBinding, ...]
    trust_fingerprint: str
    account_scope: tuple[str, ...]
    allowed_ids: tuple[str, ...]


@dataclass(frozen=True)
class _ExpectedAnalysisGraph:
    analysis_id: str
    opportunity_ids: tuple[str, ...]
    value: str


class AnalysisService:
    def __init__(
        self, database: Database, model_adapter: Any, *, runtime_dir: Path
    ) -> None:
        self.database = database
        self.model_adapter = model_adapter
        self.runtime_dir = runtime_dir.resolve()

    def create(self, payload: AnalysisCreate) -> AnalysisRead:
        before_model = self._resolve_evidence(payload)
        evidence = before_model.facts
        digest = _digest(payload, evidence)
        shop_evidence_present = any(
            fact["kind"] == "shop_collection_result" for fact in evidence
        )
        requires_complete_shop = payload.analysis_type in {
            "product_cluster",
            "account_opportunity",
        } or shop_evidence_present
        eligible = _eligible_for_opportunity(
            evidence, required_accounts=payload.account_scope
        )
        if requires_complete_shop and not eligible:
            return self._persist_failure(
                payload,
                digest=digest,
                status="needs_human",
                error_category="deep_verification_incomplete",
                error_detail=(
                    "Unique shop links, verified product details and local image manifests "
                    "must all be complete before opportunity judgment."
                ),
            )

        request = StructuredModelRequest(
            system_prompt=_system_prompt(),
            user_prompt=json.dumps(
                {
                    "analysis_type": payload.analysis_type,
                    "account_user_id": payload.account_user_id,
                    "account_user_ids": payload.account_user_ids,
                    "allowed_evidence": evidence,
                    "required_schema": AnalysisOutput.model_json_schema(),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            prompt_version=PROMPT_VERSION,
            evidence_ids=payload.evidence_ids,
        )
        try:
            model_result = self.model_adapter.generate_structured(request, AnalysisOutput)
        except ModelAdapterError as error:
            return self._persist_failure(
                payload,
                digest=digest,
                status="failed",
                error_category=_safe_model_category(error.category),
                error_detail="Model adapter reported a safe failure.",
                attempts=_safe_model_attempts(error.attempts),
            )
        except TimeoutError:
            return self._persist_failure(
                payload,
                digest=digest,
                status="failed",
                error_category="model_timeout",
                error_detail="Model adapter timed out.",
            )

        opportunity_projections: list[dict[str, Any]] = []
        try:
            output = AnalysisOutput.model_validate(model_result.output)
            _validate_grounding(output, allowed=set(payload.evidence_ids))
            if payload.analysis_type == "account_report" and output.opportunities:
                raise ValueError("Single-account reports cannot create opportunities.")
            if output.opportunities and not eligible:
                raise ValueError("Opportunity output requires complete deep verification.")
            opportunity_projections = [
                _validated_opportunity_projection(
                    card,
                    evidence=evidence,
                    required_accounts=payload.account_scope,
                )
                for card in output.opportunities
            ]
        except (ValidationError, ValueError) as error:
            return self._persist_failure(
                payload,
                digest=digest,
                status="failed",
                error_category="evidence_grounding_failed",
                error_detail="Model output failed strict schema or evidence grounding.",
            )

        now = _utc_now()
        raw_evidence = model_result.raw_evidence
        record = AnalysisRecord(
            id=str(uuid4()),
            analysis_type=payload.analysis_type,
            account_user_id=payload.account_user_id,
            account_user_ids_json=sorted(payload.account_user_ids),
            status="succeeded",
            prompt_version=PROMPT_VERSION,
            provider=self.model_adapter.provider,
            model=self.model_adapter.model,
            input_digest=digest,
            evidence_ids_json=list(payload.evidence_ids),
            output_json=output.model_dump(mode="json"),
            usage_json=dict(model_result.usage),
            duration_ms=model_result.duration_ms,
            attempts_json=_safe_model_attempts(
                raw_evidence.get("attempts") if isinstance(raw_evidence, dict) else None
            ),
            created_at=now,
        )
        for card, projection in zip(
            output.opportunities, opportunity_projections, strict=True
        ):
            record.opportunities.append(
                OpportunityRecord(
                    id=str(uuid4()),
                    title=card.title,
                    status=projection["status"],
                    summary=card.summary,
                    evidence_ids_json=list(card.evidence_ids),
                    review_status="pending_review",
                    evidence_level=projection["evidence_level"],
                    supporting_accounts_json=projection["supporting_accounts"],
                    supporting_products_json=projection["supporting_products"],
                    supporting_notes_json=projection["supporting_notes"],
                    next_action=card.next_action,
                    created_at=now,
                )
            )
        with self.database.session() as session:
            # Hold SQLite's single-writer reservation across the final resolve
            # and insert so no mutable DB evidence can pass between the check
            # and the success commit.
            session.execute(text("BEGIN IMMEDIATE"))
            try:
                after_model = self._resolve_evidence_in_session(session, payload)
            except (EvidenceNotFound, EvidenceAccountMismatch):
                return self._persist_failure_in_session(
                    session,
                    payload,
                    digest=digest,
                    status="needs_human",
                    error_category="evidence_changed_after_model",
                    error_detail=(
                        "Evidence trust changed while the model request was in flight."
                    ),
                )
            if (
                after_model.trust_fingerprint
                != before_model.trust_fingerprint
                or after_model.account_scope != before_model.account_scope
                or after_model.allowed_ids != before_model.allowed_ids
            ):
                return self._persist_failure_in_session(
                    session,
                    payload,
                    digest=digest,
                    status="needs_human",
                    error_category="evidence_changed_after_model",
                    error_detail=(
                        "Evidence trust changed while the model request was in flight."
                    ),
                )
            record.evidence_snapshot_json = _evidence_snapshot_value(
                after_model,
                input_digest=digest,
            )
            expected_graph = _expected_analysis_graph(record)
            session.add(record)
            # Flush the immutable claim snapshot into the reserved DB
            # transaction before the final name->identity CAS.  If that CAS
            # fails the whole success graph is rolled back; after it succeeds,
            # the claim no longer depends on mutable path bytes.
            session.flush()
            if not _artifact_bindings_still_current(
                self.runtime_dir,
                after_model.artifact_bindings,
            ):
                session.rollback()
                session.execute(text("BEGIN IMMEDIATE"))
                return self._persist_failure_in_session(
                    session,
                    payload,
                    digest=digest,
                    status="needs_human",
                    error_category="evidence_changed_after_model",
                    error_detail=(
                        "Evidence trust changed while the model request was in flight."
                    ),
                )
            try:
                session.commit()
                return _analysis_read(_load_analysis(session, record.id))
            except SQLAlchemyError as error:
                session.close()
                outcome, committed = self._classify_success_commit(expected_graph)
                if outcome is _AnalysisCommitOutcome.committed:
                    assert committed is not None
                    return committed
                if outcome is _AnalysisCommitOutcome.rolled_back:
                    raise AnalysisCommitRolledBack(
                        "analysis_success_commit_rolled_back"
                    ) from error
                raise AnalysisTransactionUnknown(
                    "analysis_success_transaction_unknown"
                ) from error

    def get(self, analysis_id: str) -> AnalysisRead:
        with self.database.session() as session:
            return _analysis_read(_load_analysis(session, analysis_id))

    def list(self) -> list[AnalysisRead]:
        with self.database.session() as session:
            records = session.scalars(
                select(AnalysisRecord)
                .options(selectinload(AnalysisRecord.opportunities))
                .order_by(AnalysisRecord.created_at.desc(), AnalysisRecord.id)
            ).all()
            return [_analysis_read(record) for record in records]

    def list_opportunities(self) -> list[OpportunityRead]:
        with self.database.session() as session:
            records = session.scalars(
                select(OpportunityRecord)
                .join(AnalysisRecord)
                .where(AnalysisRecord.status == "succeeded")
                .order_by(OpportunityRecord.created_at.desc(), OpportunityRecord.id)
            ).all()
            return [_opportunity_read(record) for record in records]

    def review_opportunity(
        self, opportunity_id: str, payload: OpportunityReviewCreate
    ) -> OpportunityRead:
        now = _utc_now()
        review_status = "approved" if payload.decision == "approve" else "rejected"
        rejection_reason = payload.reason if payload.decision == "reject" else None
        with self.database.session() as session:
            result = session.execute(
                update(OpportunityRecord)
                .where(
                    OpportunityRecord.id == opportunity_id,
                    OpportunityRecord.review_status == "pending_review",
                )
                .values(
                    review_status=review_status,
                    reviewed_at=now,
                    rejection_reason=rejection_reason,
                )
            )
            if result.rowcount != 1:
                existing = session.get(OpportunityRecord, opportunity_id)
                if existing is None:
                    raise OpportunityNotFound(
                        f"Opportunity {opportunity_id} does not exist."
                    )
                raise OpportunityStateError(
                    "Opportunity review is final and cannot be changed."
                )
            session.commit()
            record = session.get(OpportunityRecord, opportunity_id)
            if record is None:
                raise OpportunityNotFound(
                    f"Opportunity {opportunity_id} does not exist."
                )
            return _opportunity_read(record)

    def list_evidence(
        self, *, account_user_id: str | None = None
    ) -> list[AnalysisEvidenceRead]:
        rows: list[AnalysisEvidenceRead] = []
        with self.database.session() as session:
            artifacts = session.scalars(
                select(JobArtifactRecord)
                .options(selectinload(JobArtifactRecord.job))
                .order_by(JobArtifactRecord.id)
            ).all()
            for artifact in artifacts:
                if artifact.kind in XHS_RAW_TRUST_ANCHOR_KINDS:
                    # The raw snapshot is a trust anchor, not a selectable model
                    # fact. Its public notes are exposed through account-note IDs.
                    continue
                job_input = dict(artifact.job.input_data)
                artifact_account = job_input.get("account_user_id")
                if account_user_id is not None and artifact_account != account_user_id:
                    continue
                fact = {
                    "evidence_id": f"artifact:{artifact.id}",
                    "kind": artifact.kind,
                    "account_user_id": (
                        artifact_account if isinstance(artifact_account, str) else None
                    ),
                    "job_state": JobState(artifact.job.state).value,
                    "trusted_shop_result": self._trusted_shop_result(artifact),
                }
                rows.append(
                    AnalysisEvidenceRead(
                        evidence_id=fact["evidence_id"],
                        kind=artifact.kind,
                        account_user_id=(
                            artifact_account if isinstance(artifact_account, str) else None
                        ),
                        eligible_for_opportunity=_eligible_for_opportunity(
                            [fact],
                            required_accounts=(
                                frozenset((artifact_account,))
                                if isinstance(artifact_account, str) and artifact_account
                                else frozenset()
                            ),
                        ),
                    )
                )
            rank_items = session.scalars(
                select(RankItemRecord).order_by(RankItemRecord.id)
            ).all()
            for item in rank_items:
                if account_user_id is not None and item.user_id != account_user_id:
                    continue
                rows.append(
                    AnalysisEvidenceRead(
                        evidence_id=f"rank-item:{item.id}",
                        kind="rank_item",
                        account_user_id=item.user_id,
                        eligible_for_opportunity=False,
                    )
                )
            latest_snapshot_ids = (
                select(func.max(XhsAccountProfileSnapshotRecord.id))
                .group_by(XhsAccountProfileSnapshotRecord.user_id)
            )
            account_note_query = (
                select(XhsAccountNoteRecord)
                .join(
                    XhsAccountSnapshotNoteRecord,
                    XhsAccountSnapshotNoteRecord.note_record_id
                    == XhsAccountNoteRecord.id,
                )
                .where(
                    XhsAccountSnapshotNoteRecord.snapshot_id.in_(
                        latest_snapshot_ids
                    )
                )
            )
            if account_user_id is not None:
                account_note_query = account_note_query.where(
                    XhsAccountNoteRecord.user_id == account_user_id
                )
            account_notes = session.scalars(
                account_note_query.order_by(XhsAccountNoteRecord.id)
            ).all()
            for note in account_notes:
                if not _canonical_sqlite_identity(note.id):
                    continue
                trusted = self._trusted_account_note(session, note)
                rows.append(
                    AnalysisEvidenceRead(
                        evidence_id=f"account-note:{note.id}",
                        kind="account_note",
                        account_user_id=note.user_id,
                        eligible_for_opportunity=trusted is not None,
                    )
                )
        return rows

    def _classify_success_commit(
        self,
        expected: _ExpectedAnalysisGraph,
    ) -> tuple[_AnalysisCommitOutcome, AnalysisRead | None]:
        for _attempt in range(2):
            try:
                return self._probe_success_commit(expected)
            except SQLAlchemyError:
                continue
        return _AnalysisCommitOutcome.unknown, None

    def _probe_success_commit(
        self,
        expected: _ExpectedAnalysisGraph,
    ) -> tuple[_AnalysisCommitOutcome, AnalysisRead | None]:
        with self.database.session() as session:
            record = session.scalar(
                select(AnalysisRecord)
                .options(selectinload(AnalysisRecord.opportunities))
                .where(AnalysisRecord.id == expected.analysis_id)
            )
            related_opportunities = session.scalars(
                select(OpportunityRecord).where(or_(
                    OpportunityRecord.analysis_id == expected.analysis_id,
                    OpportunityRecord.id.in_(expected.opportunity_ids),
                ))
            ).all()
            if record is None:
                return (
                    (_AnalysisCommitOutcome.rolled_back, None)
                    if not related_opportunities
                    else (_AnalysisCommitOutcome.unknown, None)
                )
            if _analysis_graph_value(record) != expected.value:
                return _AnalysisCommitOutcome.unknown, None
            return _AnalysisCommitOutcome.committed, _analysis_read(record)

    def _persist_failure(
        self,
        payload: AnalysisCreate,
        *,
        digest: str,
        status: str,
        error_category: str,
        error_detail: str,
        attempts: list[dict[str, Any]] | None = None,
    ) -> AnalysisRead:
        record = AnalysisRecord(
            analysis_type=payload.analysis_type,
            account_user_id=payload.account_user_id,
            account_user_ids_json=list(payload.account_user_ids),
            status=status,
            prompt_version=PROMPT_VERSION,
            provider=self.model_adapter.provider,
            model=self.model_adapter.model,
            input_digest=digest,
            evidence_ids_json=list(payload.evidence_ids),
            output_json=None,
            usage_json={},
            duration_ms=None,
            attempts_json=attempts or [],
            error_category=error_category,
            error_detail=error_detail,
            created_at=_utc_now(),
        )
        with self.database.session() as session:
            session.add(record)
            session.commit()
            return _analysis_read(_load_analysis(session, record.id))

    def _persist_failure_in_session(
        self,
        session: Any,
        payload: AnalysisCreate,
        *,
        digest: str,
        status: str,
        error_category: str,
        error_detail: str,
    ) -> AnalysisRead:
        record = AnalysisRecord(
            analysis_type=payload.analysis_type,
            account_user_id=payload.account_user_id,
            account_user_ids_json=list(payload.account_user_ids),
            status=status,
            prompt_version=PROMPT_VERSION,
            provider=self.model_adapter.provider,
            model=self.model_adapter.model,
            input_digest=digest,
            evidence_ids_json=list(payload.evidence_ids),
            output_json=None,
            usage_json={},
            duration_ms=None,
            attempts_json=[],
            error_category=error_category,
            error_detail=error_detail,
            created_at=_utc_now(),
        )
        session.add(record)
        session.commit()
        return _analysis_read(_load_analysis(session, record.id))

    def _resolve_evidence(self, payload: AnalysisCreate) -> _EvidenceResolution:
        with self.database.session() as session:
            return self._resolve_evidence_in_session(session, payload)

    def _resolve_evidence_in_session(
        self,
        session: Any,
        payload: AnalysisCreate,
    ) -> _EvidenceResolution:
        facts: list[dict[str, Any]] = []
        trust: list[dict[str, Any]] = []
        artifact_bindings: list[_ArtifactBinding] = []
        account_scope = payload.account_scope
        for evidence_id in payload.evidence_ids:
            prefix, separator, raw_id = evidence_id.partition(":")
            identity = _parse_sqlite_identity(raw_id) if separator else None
            if identity is None:
                raise EvidenceNotFound(f"Unknown evidence id: {evidence_id}")
            if prefix == "artifact":
                artifact = session.scalar(
                    select(JobArtifactRecord)
                    .options(selectinload(JobArtifactRecord.job))
                    .where(JobArtifactRecord.id == identity)
                )
                if artifact is None:
                    raise EvidenceNotFound(f"Unknown evidence id: {evidence_id}")
                job_input = dict(artifact.job.input_data)
                artifact_account = job_input.get("account_user_id")
                if (
                    not isinstance(artifact_account, str)
                    or not artifact_account.strip()
                    or artifact_account not in account_scope
                ):
                    raise EvidenceAccountMismatch(
                        f"Evidence {evidence_id} has no matching account ownership."
                    )
                (
                    trusted_shop_result,
                    artifact_binding,
                ) = self._trusted_shop_result_and_binding(
                    artifact,
                    evidence_id=evidence_id,
                )
                facts.append(
                    {
                        "evidence_id": evidence_id,
                        "kind": artifact.kind,
                        "job_id": artifact.job_id,
                        "job_state": JobState(artifact.job.state).value,
                        "account_user_id": artifact_account,
                        "job_input": job_input,
                        "trusted_shop_result": trusted_shop_result,
                    }
                )
                if artifact_binding is not None:
                    artifact_bindings.append(artifact_binding)
                trust.append({
                    "evidence_id": evidence_id,
                    "artifact_id": artifact.id,
                    "artifact_job_id": artifact.job_id,
                    "artifact_kind": artifact.kind,
                    "artifact_producer": artifact.producer,
                    "artifact_path": artifact.path,
                    "artifact_metadata": artifact.metadata_json,
                    "job_type": artifact.job.type,
                    "job_state": artifact.job.state,
                    "job_input": artifact.job.input_data,
                    "trusted_shop_result": facts[-1]["trusted_shop_result"],
                })
            elif prefix == "rank-item":
                item = session.get(RankItemRecord, identity)
                if item is None:
                    raise EvidenceNotFound(f"Unknown evidence id: {evidence_id}")
                if (
                    not isinstance(item.user_id, str)
                    or not item.user_id.strip()
                    or item.user_id not in account_scope
                ):
                    raise EvidenceAccountMismatch(
                        f"Evidence {evidence_id} has no matching account ownership."
                    )
                facts.append(
                    {
                        "evidence_id": evidence_id,
                        "kind": "rank_item",
                        "account_user_id": item.user_id,
                        "user_id": item.user_id,
                        "source_url": item.source_url,
                        "facts": {
                            "title": item.title,
                            "author_name": item.author_name,
                            "gmv_range": item.gmv_range,
                            "pay_rate_range": item.pay_rate_range,
                            "read_range": item.read_range,
                        },
                        "raw_evidence": dict(item.raw_evidence),
                    }
                )
                trust.append({"evidence_id": evidence_id, "row": facts[-1]})
            elif prefix == "account-note":
                note = session.get(XhsAccountNoteRecord, identity)
                if note is None:
                    raise EvidenceNotFound(f"Unknown evidence id: {evidence_id}")
                if note.user_id not in account_scope:
                    raise EvidenceAccountMismatch(
                        f"Evidence {evidence_id} has no matching account ownership."
                    )
                trusted_note = self._trusted_account_note(session, note)
                if trusted_note is None:
                    raise EvidenceNotFound(
                        f"Evidence {evidence_id} is no longer trusted."
                    )
                public_fact, trust_fact, artifact_binding = trusted_note
                facts.append(public_fact)
                trust.append(trust_fact)
                artifact_bindings.append(artifact_binding)
            else:
                raise EvidenceNotFound(f"Unknown evidence id: {evidence_id}")
        fingerprint_value = json.dumps(
            {
                "account_scope": sorted(account_scope),
                "allowed_ids": list(payload.evidence_ids),
                "facts": facts,
                "trust": trust,
                "artifact_bindings": [
                    binding.snapshot_value() for binding in artifact_bindings
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return _EvidenceResolution(
            facts=facts,
            trust=trust,
            artifact_bindings=tuple(artifact_bindings),
            trust_fingerprint=sha256(fingerprint_value.encode("utf-8")).hexdigest(),
            account_scope=tuple(sorted(account_scope)),
            allowed_ids=tuple(payload.evidence_ids),
        )

    def _trusted_account_note(
        self, session: Any, note: XhsAccountNoteRecord
    ) -> tuple[dict[str, Any], dict[str, Any], _ArtifactBinding] | None:
        if not _canonical_sqlite_identity(note.id):
            return None
        member = session.get(XhsAccountSnapshotNoteRecord, note.id)
        snapshot = (
            session.get(XhsAccountProfileSnapshotRecord, member.snapshot_id)
            if member is not None
            else None
        )
        anchor = session.get(XhsAccountProfileRecord, note.user_id)
        artifact = session.get(JobArtifactRecord, note.collection_artifact_id)
        journals = session.scalars(
            select(XhsArtifactPromotionJournalRecord).where(
                XhsArtifactPromotionJournalRecord.job_id
                == note.collection_job_id,
                XhsArtifactPromotionJournalRecord.artifact_id
                == note.collection_artifact_id,
                XhsArtifactPromotionJournalRecord.state == "completed",
                XhsArtifactPromotionJournalRecord.resolution == "committed",
            )
        ).all()
        if (
            member is None
            or snapshot is None
            or anchor is None
            or artifact is None
            or len(journals) != 1
        ):
            return None
        journal = journals[0]
        job = artifact.job
        if (
            job is None
            or job.id != note.collection_job_id
            or job.id != snapshot.collection_job_id
            or job.type != ACCOUNT_COLLECTION_JOB_TYPE
            or _safe_job_state(job.state) is not JobState.succeeded
            or artifact.job_id != job.id
            or artifact.id != snapshot.collection_artifact_id
            or artifact.kind != ACCOUNT_COLLECTION_ARTIFACT_KIND
            or artifact.producer != ACCOUNT_COLLECTION_ARTIFACT_PRODUCER
            or note.user_id != snapshot.user_id
            or journal.target_state != JobState.succeeded.value
            or journal.artifact_kind != artifact.kind
            or journal.producer != artifact.producer
            or journal.final_path != artifact.path
            or journal.owner_token is not None
            or journal.recovery_lease_expires_at is not None
            or journal.file_dev is None
            or journal.file_ino is None
            or journal.file_mtime_ns is None
        ):
            return None
        expected_path = Path("evidence") / "xhs" / f"{job.id}.json"
        if artifact.path != expected_path.as_posix():
            return None
        try:
            file_snapshot = _read_contained_regular_file(
                self.runtime_dir,
                expected_path,
                limit=MAX_TRUSTED_ACCOUNT_RESULT_BYTES,
            )
            if file_snapshot is None:
                return None
            raw_payload = file_snapshot.payload
            metadata = artifact.metadata_json
            if not isinstance(metadata, dict):
                return None
            if not _strict_value(metadata.get("size_bytes"), len(raw_payload)):
                return None
            file_digest = sha256(raw_payload).hexdigest()
            if metadata.get("sha256") != file_digest:
                return None
            physical_identity = file_snapshot.identity
            if physical_identity != (
                journal.file_dev,
                journal.file_ino,
                journal.size_bytes,
                journal.file_mtime_ns,
            ):
                return None
            if (
                journal.sha256 != file_digest
                or journal.size_bytes != len(raw_payload)
            ):
                return None
            payload_document = json.loads(raw_payload.decode("utf-8", errors="strict"))
            if (
                not isinstance(payload_document, dict)
                or set(payload_document)
                != {"schema_version", "job_id", "job_type", "collected_at", "result"}
                or payload_document.get("schema_version") != 1
                or payload_document.get("job_id") != job.id
                or payload_document.get("job_type") != ACCOUNT_COLLECTION_JOB_TYPE
            ):
                return None
            collected_at = datetime.fromisoformat(payload_document["collected_at"])
            result = CollectionResult.model_validate(payload_document["result"])
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecursionError,
            TypeError,
            ValueError,
            AttributeError,
            ValidationError,
        ):
            return None
        job_user_id = job.input_data.get("user_id")
        expected_note_count = job.input_data.get("expected_note_count")
        if (
            not isinstance(job_user_id, str)
            or not job_user_id
            or job_user_id != note.user_id
            or job_user_id != snapshot.user_id
            or isinstance(expected_note_count, bool)
            or not isinstance(expected_note_count, int)
            or expected_note_count < 0
            or job.progress_total != expected_note_count
            or job.progress_current != expected_note_count
            or job.current_stage != "xhs_collection_complete"
            or job.error_category is not None
            or collected_at != artifact.created_at
            or collected_at != note.collected_at
            or collected_at != snapshot.collected_at
        ):
            return None
        expected_item_count = expected_note_count + 1
        if not _exact_account_result(result, expected_item_count=expected_item_count):
            return None
        metadata_expected = {
            "artifact_id": artifact.id,
            "capability": "fetch_account",
            "complete": True,
            "expected_count": expected_item_count,
            "expected_item_count": expected_item_count,
            "expected_note_count": expected_note_count,
            "job_id": job.id,
            "missing_count": 0,
            "observed_count": expected_item_count,
            "overflow_count": 0,
            "rejected_count": 0,
            "sha256": file_digest,
            "size_bytes": len(raw_payload),
            "source": "xhs-cli",
            "succeeded_count": expected_item_count,
            "succeeded_item_count": expected_item_count,
            "succeeded_note_count": expected_note_count,
            "user_id": job_user_id,
        }
        if any(
            key not in metadata
            or not _strict_value(metadata[key], expected_value)
            for key, expected_value in metadata_expected.items()
        ):
            return None
        try:
            profile_item, note_items = _account_result_items(result, job_user_id)
        except (ValueError, OwnerIdentityError):
            return None
        persisted_notes = session.scalars(
            select(XhsAccountNoteRecord)
            .join(
                XhsAccountSnapshotNoteRecord,
                XhsAccountSnapshotNoteRecord.note_record_id
                == XhsAccountNoteRecord.id,
            )
            .where(XhsAccountSnapshotNoteRecord.snapshot_id == snapshot.id)
            .order_by(XhsAccountSnapshotNoteRecord.position)
        ).all()
        items_by_note_id = {
            str(item.data["note_id"]): item for item in note_items
        }
        if (
            len(persisted_notes) != expected_note_count
            or len(items_by_note_id) != expected_note_count
            or {row.note_id for row in persisted_notes} != set(items_by_note_id)
            or [row.note_id for row in persisted_notes]
            != [str(item.data["note_id"]) for item in note_items]
            or not _profile_matches_item(snapshot, profile_item, artifact.id, job.id)
        ):
            return None
        for persisted_note in persisted_notes:
            if not _note_matches_item(
                persisted_note,
                items_by_note_id[persisted_note.note_id],
                artifact.id,
                job.id,
            ):
                return None
        if (
            anchor.collection_job_id == snapshot.collection_job_id
            and anchor.collection_artifact_id == snapshot.collection_artifact_id
            and not _profile_matches_item(
                anchor,
                profile_item,
                artifact.id,
                job.id,
            )
        ):
            return None
        public_fact = {
            "evidence_id": f"account-note:{note.id}",
            "kind": "account_note",
            "account_user_id": note.user_id,
            "facts": {
                "profile": {
                    "user_id": snapshot.user_id,
                    "source_url": snapshot.source_url,
                    "nickname": snapshot.nickname,
                    "bio": snapshot.bio,
                    "public_stats": dict(snapshot.public_stats_json),
                },
                "note": {
                    "note_id": note.note_id,
                    "source_url": note.source_url,
                    "title": note.title,
                    "summary": note.summary,
                    "published_at": note.published_at,
                    "public_interactions": dict(note.public_interactions_json),
                },
            },
        }
        trust_fact = {
            "evidence_id": public_fact["evidence_id"],
            "note_record_id": note.id,
            "snapshot_id": snapshot.id,
            "snapshot_position": member.position,
            "snapshot_note_record_ids": [row.id for row in persisted_notes],
            "collection_job_id": job.id,
            "collection_artifact_id": artifact.id,
            "artifact_path": artifact.path,
            "artifact_metadata": metadata,
            "journal_id": journal.id,
            "journal_sha256": journal.sha256,
            "journal_size_bytes": journal.size_bytes,
            "journal_file_identity": list(physical_identity),
            "profile_raw_digest": snapshot.raw_digest,
            "note_raw_digests": [row.raw_digest for row in persisted_notes],
        }
        return (
            public_fact,
            trust_fact,
            _ArtifactBinding(
                evidence_id=public_fact["evidence_id"],
                artifact_id=artifact.id,
                artifact_job_id=artifact.job_id,
                relative_path=artifact.path,
                sha256=file_digest,
                size_bytes=len(raw_payload),
                file_identity=physical_identity,
            ),
        )

    def _trusted_shop_result(
        self, artifact: JobArtifactRecord
    ) -> dict[str, Any] | None:
        result, _binding = self._trusted_shop_result_and_binding(
            artifact,
            evidence_id=f"artifact:{artifact.id}",
        )
        return result

    def _trusted_shop_result_and_binding(
        self,
        artifact: JobArtifactRecord,
        *,
        evidence_id: str,
    ) -> tuple[dict[str, Any] | None, _ArtifactBinding | None]:
        job = artifact.job
        if (
            job.type not in ANDROID_SHOP_JOB_TYPES
            or artifact.kind != "shop_collection_result"
            or artifact.producer != "android_shop_worker_v1"
            or JobState(job.state) is not JobState.succeeded
        ):
            return None, None
        expected_path = Path("evidence") / "shops" / job.id / "result.json"
        if artifact.path != expected_path.as_posix():
            return None, None
        try:
            file_snapshot = _read_contained_regular_file(
                self.runtime_dir,
                expected_path,
                limit=MAX_TRUSTED_RESULT_BYTES,
            )
            if file_snapshot is None:
                return None, None
            raw_result = file_snapshot.payload
            file_result = json.loads(raw_result.decode("utf-8", errors="strict"))
            metadata_result = artifact.metadata_json.get("result")
            if not isinstance(file_result, dict) or file_result != metadata_result:
                return None, None
            parsed = ShopCollectionRead.model_validate(file_result)
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecursionError,
            TypeError,
            AttributeError,
            ValidationError,
        ):
            return None, None
        job_account = job.input_data.get("account_user_id")
        expected_count = job.input_data.get("expected_count")
        if (
            parsed.job_id != job.id
            or not isinstance(job_account, str)
            or not job_account.strip()
            or isinstance(expected_count, bool)
            or not isinstance(expected_count, int)
            or parsed.expected_count != expected_count
            or job.progress_total != expected_count
            or job.progress_current != expected_count
            or job.current_stage != "shop_complete"
            or job.error_category is not None
        ):
            return None, None
        digest = sha256(raw_result).hexdigest()
        return (
            parsed.model_dump(mode="json"),
            _ArtifactBinding(
                evidence_id=evidence_id,
                artifact_id=artifact.id,
                artifact_job_id=artifact.job_id,
                relative_path=artifact.path,
                sha256=digest,
                size_bytes=len(raw_result),
                file_identity=file_snapshot.identity,
            ),
        )


def _strict_value(actual: object, expected: object) -> bool:
    return type(actual) is type(expected) and actual == expected


def _safe_job_state(value: object) -> JobState | None:
    try:
        return JobState(value)
    except (ValueError, TypeError):
        return None


def _canonical_sqlite_identity(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and 1 <= value <= MAX_SQLITE_ID
    )


def _parse_sqlite_identity(value: str) -> int | None:
    if re.fullmatch(r"[1-9][0-9]*", value, re.ASCII) is None:
        return None
    if len(value) > 19 or (len(value) == 19 and value > str(MAX_SQLITE_ID)):
        return None
    identity = int(value)
    return identity if _canonical_sqlite_identity(identity) else None


def _exact_account_result(
    result: CollectionResult, *, expected_item_count: int
) -> bool:
    return (
        result.status == "succeeded"
        and result.complete
        and result.expected_count_known
        and result.expected_count == expected_item_count
        and result.succeeded_count == expected_item_count
        and result.observed_count == expected_item_count
        and result.raw_observation_count == expected_item_count
        and result.duplicate_observation_count == 0
        and len(result.items) == expected_item_count
        and not result.rejected_items
        and not result.missing_items
        and result.overflow_count == 0
    )


def _account_result_items(
    result: CollectionResult, account_user_id: str
) -> tuple[CollectionItem, list[CollectionItem]]:
    profiles = [item for item in result.items if item.kind == "profile"]
    notes = [item for item in result.items if item.kind == "note"]
    if len(profiles) != 1 or len(profiles) + len(notes) != len(result.items):
        raise ValueError("account result shape is not exact")
    profile = profiles[0]
    profile_owner = canonical_owner_id(
        profile.data, profile.raw_evidence, include_record_id=True
    )
    if (
        profile_owner != account_user_id
        or profile.id != f"profile:{account_user_id}"
    ):
        raise ValueError("profile identity is not exact")
    note_ids: list[str] = []
    for item in notes:
        note_id = item.data.get("note_id")
        note_owner = canonical_owner_id(item.data, item.raw_evidence)
        if (
            not isinstance(note_id, str)
            or not note_id
            or note_owner != account_user_id
            or item.id != f"note:{note_id}"
        ):
            raise ValueError("note identity is not exact")
        note_ids.append(note_id)
    if len(note_ids) != len(set(note_ids)):
        raise ValueError("note identities are not unique")
    return profile, notes


def _profile_matches_item(
    record: XhsAccountProfileRecord,
    item: CollectionItem,
    artifact_id: int,
    job_id: str,
) -> bool:
    return (
        record.collection_job_id == job_id
        and record.collection_artifact_id == artifact_id
        and record.source_url == str(item.source_url)
        and record.nickname == _public_text(item.data, "nickname")
        and record.bio == _public_text(item.data, "bio", "description", "desc")
        and public_counter_json_matches(
            record.public_stats_json,
            public_counter_values(
                item.data,
                allowed_fields=PROFILE_PUBLIC_COUNTER_FIELDS,
            ),
            allowed_fields=PROFILE_PUBLIC_COUNTER_FIELDS,
        )
        and _raw_evidence_matches(record.raw_evidence, record.raw_digest, item)
    )


def _note_matches_item(
    record: XhsAccountNoteRecord,
    item: CollectionItem,
    artifact_id: int,
    job_id: str,
) -> bool:
    return (
        record.collection_job_id == job_id
        and record.collection_artifact_id == artifact_id
        and record.note_id == item.data.get("note_id")
        and record.source_url == str(item.source_url)
        and record.title == _public_text(item.data, "title")
        and record.summary
        == _public_text(item.data, "summary", "description", "desc")
        and record.published_at
        == _public_text(
            item.data, "published_at", "publish_time", "publishTime"
        )
        and public_counter_json_matches(
            record.public_interactions_json,
            public_counter_values(
                item.data,
                allowed_fields=NOTE_PUBLIC_COUNTER_FIELDS,
            ),
            allowed_fields=NOTE_PUBLIC_COUNTER_FIELDS,
        )
        and _raw_evidence_matches(record.raw_evidence, record.raw_digest, item)
    )


def _raw_evidence_matches(
    persisted: dict[str, Any], persisted_digest: str, item: CollectionItem
) -> bool:
    item_digest = canonical_raw_evidence_digest(item.raw_evidence)
    persisted_recomputed = canonical_raw_evidence_digest(persisted)
    return (
        item_digest is not None
        and persisted_recomputed is not None
        and persisted == item.raw_evidence
        and persisted_digest == item_digest
        and persisted_digest == persisted_recomputed
    )


def _public_text(data: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = data.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def _eligible_for_opportunity(
    evidence: list[dict[str, Any]], *, required_accounts: frozenset[str]
) -> bool:
    shop_facts = [fact for fact in evidence if fact["kind"] == "shop_collection_result"]
    if not shop_facts or not required_accounts:
        return False
    covered_accounts: set[str] = set()
    for fact in shop_facts:
        account_user_id = fact.get("account_user_id")
        result = fact.get("trusted_shop_result")
        if not isinstance(account_user_id, str) or account_user_id not in required_accounts:
            return False
        if not isinstance(result, dict):
            return False
        covered_accounts.add(account_user_id)
        if result.get("status") != "succeeded" or result.get("complete") is not True:
            return False
        verification = result.get("verification")
        if not isinstance(verification, dict) or verification.get("complete") is not True:
            return False
        counts = (
            result.get("expected_count"),
            result.get("discovered_count"),
            result.get("succeeded_count"),
            verification.get("expected_count"),
            verification.get("discovered_count"),
            verification.get("succeeded_count"),
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in counts):
            return False
        all_counters = (
            result.get("expected_count"),
            result.get("discovered_count"),
            result.get("collected_count"),
            result.get("raw_observation_count"),
            result.get("duplicate_observation_count"),
            result.get("succeeded_count"),
            result.get("missing_count"),
            result.get("collection_missing_count"),
            result.get("rejected_count"),
            result.get("overflow_count"),
            verification.get("expected_count"),
            verification.get("discovered_count"),
            verification.get("succeeded_count"),
            verification.get("missing_count"),
            verification.get("overflow_count"),
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in all_counters
        ):
            return False
        if (
            len(set(counts)) != 1
            or counts[0] <= 0
            or result.get("collected_count") != counts[0]
            or result.get("missing_count") != 0
            or result.get("missing_items") != []
            or result.get("collection_missing_count") != 0
            or result.get("collection_missing_items") != []
            or result.get("overflow_count") != 0
            or verification.get("missing_count") != 0
            or verification.get("missing_items") != []
            or verification.get("overflow_count", 0) != 0
            or verification.get("issues", []) != []
        ):
            return False
        rejected_items = result.get("rejected_items")
        rejected_count = result.get("rejected_count")
        if (
            not isinstance(rejected_items, list)
            or rejected_count != len(rejected_items)
        ):
            return False
        rejected_references = [
            item.get("reference") if isinstance(item, dict) else None
            for item in rejected_items
        ]
        if (
            any(not isinstance(reference, str) or not reference.strip() for reference in rejected_references)
            or len(set(rejected_references)) != len(rejected_references)
        ):
            return False
        duplicate_rows = sum(
            isinstance(item, dict) and item.get("reason") == "duplicate_source_url"
            for item in rejected_items
        )
        if (
            duplicate_rows != result.get("duplicate_observation_count")
            or duplicate_rows != rejected_count
            or result.get("raw_observation_count")
            != result.get("collected_count") + rejected_count
        ):
            return False
        items = result.get("items")
        if not isinstance(items, list) or len(items) != counts[0]:
            return False
        item_ids = [item.get("id") for item in items if isinstance(item, dict)]
        if len(item_ids) != counts[0] or len(set(item_ids)) != counts[0]:
            return False
        urls = [item.get("source_url") for item in items if isinstance(item, dict)]
        if len(urls) != counts[0] or len(set(urls)) != counts[0]:
            return False
    return covered_accounts == set(required_accounts)


def _validated_opportunity_projection(
    card: Any,
    *,
    evidence: list[dict[str, Any]],
    required_accounts: frozenset[str],
) -> dict[str, Any]:
    if len(required_accounts) < 2:
        raise ValueError("Cross-account opportunities require at least two accounts.")
    facts_by_id = {
        fact.get("evidence_id"): fact
        for fact in evidence
        if isinstance(fact.get("evidence_id"), str)
    }
    supporting_accounts = [item.model_dump(mode="json") for item in card.supporting_accounts]
    if {item["account_user_id"] for item in supporting_accounts} != set(required_accounts):
        raise ValueError("Opportunity support must cover the complete account scope.")
    cited = set(card.evidence_ids)
    supporting_products: list[dict[str, Any]] = []
    supporting_notes: list[dict[str, Any]] = []
    for support in supporting_accounts:
        account_user_id = support["account_user_id"]
        support_ids = set(support["shop_evidence_ids"]) | set(
            support["note_evidence_ids"]
        )
        if not support_ids.issubset(cited):
            raise ValueError("Opportunity support must be included in card citations.")
        for evidence_id in support["shop_evidence_ids"]:
            fact = facts_by_id.get(evidence_id)
            if (
                not isinstance(fact, dict)
                or fact.get("account_user_id") != account_user_id
                or fact.get("kind") != "shop_collection_result"
                or not isinstance(fact.get("trusted_shop_result"), dict)
            ):
                raise ValueError("Shop evidence does not belong to its supporting account.")
            result = fact["trusted_shop_result"]
            artifacts = result.get("evidence_artifacts")
            artifacts = artifacts if isinstance(artifacts, list) else []
            items = result.get("items")
            if not isinstance(items, list) or not items:
                raise ValueError("Supporting shop evidence has no verified products.")
            for index, item in enumerate(items, start=1):
                if not isinstance(item, dict):
                    raise ValueError("Supporting shop product is malformed.")
                data = item.get("data") if isinstance(item.get("data"), dict) else {}
                raw = (
                    item.get("raw_evidence")
                    if isinstance(item.get("raw_evidence"), dict)
                    else {}
                )
                title = data.get("title") or raw.get("title")
                image_count = sum(
                    isinstance(path, str)
                    and f"product_{index}_" in path
                    and path.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
                    for path in artifacts
                )
                if image_count < 1:
                    raise ValueError(
                        "Supporting shop product has no trusted image manifest entry."
                    )
                supporting_products.append(
                    {
                        "account_user_id": account_user_id,
                        "evidence_id": evidence_id,
                        "product_id": str(item.get("id") or ""),
                        "title": title if isinstance(title, str) else None,
                        "source_url": str(item.get("source_url") or ""),
                        "image_evidence_count": image_count,
                    }
                )
        for evidence_id in support["note_evidence_ids"]:
            fact = facts_by_id.get(evidence_id)
            note = fact.get("facts", {}).get("note") if isinstance(fact, dict) else None
            if (
                not isinstance(fact, dict)
                or fact.get("account_user_id") != account_user_id
                or fact.get("kind") != "account_note"
                or not isinstance(note, dict)
            ):
                raise ValueError("Note evidence does not belong to its supporting account.")
            supporting_notes.append(
                {
                    "account_user_id": account_user_id,
                    "evidence_id": evidence_id,
                    "note_id": str(note.get("note_id") or ""),
                    "title": note.get("title") if isinstance(note.get("title"), str) else None,
                    "source_url": str(note.get("source_url") or ""),
                }
            )
    level = (
        "validated_candidate"
        if len(required_accounts) >= 3
        else "warming_candidate"
    )
    return {
        "status": "已验证" if level == "validated_candidate" else "升温",
        "evidence_level": level,
        "supporting_accounts": supporting_accounts,
        "supporting_products": supporting_products,
        "supporting_notes": supporting_notes,
    }


def _read_contained_regular_file(
    root: Path, relative_path: Path, *, limit: int
) -> _ContainedFileSnapshot | None:
    try:
        resolved_root = root.resolve(strict=True)
        candidate = root.joinpath(*relative_path.parts)
        current = root
        for part in relative_path.parts:
            current = current / part
            if current.is_symlink() or (
                hasattr(current, "is_junction") and current.is_junction()
            ):
                return None
        pre_resolved = candidate.resolve(strict=True)
        pre_resolved.relative_to(resolved_root)
        pre_stat = candidate.stat()
        if not stat.S_ISREG(pre_stat.st_mode) or pre_stat.st_size > limit:
            return None
        with candidate.open("rb") as stream:
            opened_stat = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(opened_stat.st_mode)
                or _file_identity(opened_stat) != _file_identity(pre_stat)
                or opened_stat.st_size > limit
            ):
                return None
            payload = stream.read(limit + 1)
            if len(payload) > limit:
                return None
            final_handle_stat = os.fstat(stream.fileno())
            if _file_identity(final_handle_stat) != _file_identity(opened_stat):
                return None
        post_resolved = candidate.resolve(strict=True)
        post_resolved.relative_to(resolved_root)
        post_stat = candidate.stat()
        if (
            post_resolved != pre_resolved
            or _file_identity(post_stat) != _file_identity(opened_stat)
        ):
            return None
    except (OSError, ValueError, TypeError):
        return None
    return _ContainedFileSnapshot(
        payload=payload,
        identity=_file_identity(opened_stat),
    )


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (
        sqlite_file_device_identity(value.st_dev),
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
    )


def _contained_regular_file_identity(
    root: Path,
    relative_path: Path,
    *,
    limit: int,
) -> tuple[int, int, int, int] | None:
    """CAS the trusted name to an identity without rereading evidence bytes."""

    try:
        resolved_root = root.resolve(strict=True)
        candidate = root.joinpath(*relative_path.parts)
        current = root
        for part in relative_path.parts:
            current = current / part
            if current.is_symlink() or (
                hasattr(current, "is_junction") and current.is_junction()
            ):
                return None
        first_resolved = candidate.resolve(strict=True)
        first_resolved.relative_to(resolved_root)
        first_stat = candidate.stat()
        if not stat.S_ISREG(first_stat.st_mode) or first_stat.st_size > limit:
            return None
        second_resolved = candidate.resolve(strict=True)
        second_resolved.relative_to(resolved_root)
        second_stat = candidate.stat()
        if (
            second_resolved != first_resolved
            or _file_identity(second_stat) != _file_identity(first_stat)
        ):
            return None
        return _file_identity(first_stat)
    except (OSError, ValueError, TypeError):
        return None


def _artifact_bindings_still_current(
    runtime_dir: Path,
    bindings: tuple[_ArtifactBinding, ...],
) -> bool:
    return all(
        _contained_regular_file_identity(
            runtime_dir,
            Path(binding.relative_path),
            limit=MAX_TRUSTED_ACCOUNT_RESULT_BYTES,
        )
        == binding.file_identity
        for binding in bindings
    )


def _evidence_snapshot_value(
    resolution: _EvidenceResolution,
    *,
    input_digest: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "trust_fingerprint": resolution.trust_fingerprint,
        "account_scope": list(resolution.account_scope),
        "allowed_ids": list(resolution.allowed_ids),
        "facts": resolution.facts,
        "trust": resolution.trust,
        "artifact_bindings": [
            binding.snapshot_value() for binding in resolution.artifact_bindings
        ],
        "input_digest": input_digest,
    }


_SAFE_MODEL_CATEGORIES = {
    "model_unconfigured",
    "model_authentication_failed",
    "model_retry_exhausted",
    "model_request_failed",
    "model_output_invalid",
    "model_transport_failed",
    "model_timeout",
}
_SAFE_ATTEMPT_CATEGORIES = re.compile(
    r"^(?:timeout|network|authentication|response_received|http_(?:408|429|5[0-9]{2}))$",
    re.ASCII,
)


def _safe_model_category(value: object) -> str:
    return value if isinstance(value, str) and value in _SAFE_MODEL_CATEGORIES else "model_request_failed"


def _safe_model_attempts(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    safe: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        attempt = item.get("attempt")
        category = item.get("category")
        if (
            isinstance(attempt, bool)
            or not isinstance(attempt, int)
            or not 1 <= attempt <= 1000
            or not isinstance(category, str)
            or len(category) > 32
            or _SAFE_ATTEMPT_CATEGORIES.fullmatch(category) is None
        ):
            continue
        safe.append({"attempt": attempt, "category": category})
        if len(safe) == 20:
            break
    return safe


def _validate_grounding(output: AnalysisOutput, *, allowed: set[str]) -> None:
    citation_groups = [item.evidence_ids for item in output.claims]
    citation_groups += [item.evidence_ids for item in output.product_clusters]
    citation_groups += [item.evidence_ids for item in output.opportunities]
    for citations in citation_groups:
        if not citations or not set(citations).issubset(allowed):
            raise ValueError("Every conclusion must cite only request evidence.")


def _digest(payload: AnalysisCreate, evidence: list[dict[str, Any]]) -> str:
    value = json.dumps(
        {"request": payload.model_dump(mode="json"), "evidence": evidence},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(value.encode("utf-8")).hexdigest()


def _system_prompt() -> str:
    return (
        "Return exactly one JSON object matching the supplied schema. Every claim, product "
        "cluster and opportunity must cite one or more IDs from allowed_evidence. Use only "
        "persisted facts. Opportunity status must be one of 观察中/升温/已验证/降温/放弃. "
        "Provide evidence and a next_action; do not make the operator's final business decision."
    )


def _load_analysis(session: Any, analysis_id: str) -> AnalysisRecord:
    record = session.scalar(
        select(AnalysisRecord)
        .options(selectinload(AnalysisRecord.opportunities))
        .where(AnalysisRecord.id == analysis_id)
    )
    if record is None:
        raise AnalysisNotFound(f"Analysis {analysis_id} does not exist.")
    return record


def _expected_analysis_graph(record: AnalysisRecord) -> _ExpectedAnalysisGraph:
    return _ExpectedAnalysisGraph(
        analysis_id=record.id,
        opportunity_ids=tuple(sorted(card.id for card in record.opportunities)),
        value=_analysis_graph_value(record),
    )


def _analysis_graph_value(record: AnalysisRecord) -> str:
    value = {
        "analysis": {
            "id": record.id,
            "analysis_type": record.analysis_type,
            "account_user_id": record.account_user_id,
            "account_user_ids_json": record.account_user_ids_json,
            "status": record.status,
            "prompt_version": record.prompt_version,
            "provider": record.provider,
            "model": record.model,
            "input_digest": record.input_digest,
            "evidence_ids_json": record.evidence_ids_json,
            "evidence_snapshot_json": record.evidence_snapshot_json,
            "output_json": record.output_json,
            "usage_json": record.usage_json,
            "duration_ms": record.duration_ms,
            "attempts_json": record.attempts_json,
            "error_category": record.error_category,
            "error_detail": record.error_detail,
            "created_at": record.created_at.isoformat(timespec="microseconds"),
        },
        "opportunities": [
            {
                "id": card.id,
                "analysis_id": card.analysis_id or record.id,
                "title": card.title,
                "status": card.status,
                "summary": card.summary,
                "evidence_ids_json": card.evidence_ids_json,
                "review_status": card.review_status,
                "evidence_level": card.evidence_level,
                "supporting_accounts_json": card.supporting_accounts_json,
                "supporting_products_json": card.supporting_products_json,
                "supporting_notes_json": card.supporting_notes_json,
                "reviewed_at": (
                    card.reviewed_at.isoformat(timespec="microseconds")
                    if card.reviewed_at is not None
                    else None
                ),
                "rejection_reason": card.rejection_reason,
                "next_action": card.next_action,
                "created_at": card.created_at.isoformat(timespec="microseconds"),
            }
            for card in sorted(record.opportunities, key=lambda item: item.id)
        ],
    }
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _analysis_read(record: AnalysisRecord) -> AnalysisRead:
    return AnalysisRead(
        id=record.id,
        analysis_type=record.analysis_type,
        account_user_id=record.account_user_id,
        account_user_ids=list(record.account_user_ids_json),
        status=record.status,
        prompt_version=record.prompt_version,
        provider=record.provider,
        model=record.model,
        input_digest=record.input_digest,
        evidence_ids=list(record.evidence_ids_json),
        output=record.output_json if record.status == "succeeded" else None,
        usage=dict(record.usage_json),
        duration_ms=record.duration_ms,
        attempts=list(record.attempts_json),
        error_category=record.error_category,
        error_detail=record.error_detail,
        created_at=record.created_at,
    )


def _opportunity_read(record: OpportunityRecord) -> OpportunityRead:
    return OpportunityRead(
        id=record.id,
        analysis_id=record.analysis_id,
        title=record.title,
        status=record.status,
        summary=record.summary,
        evidence_ids=list(record.evidence_ids_json),
        review_status=record.review_status,
        evidence_level=record.evidence_level,
        supporting_account_count=len(record.supporting_accounts_json),
        supporting_accounts=list(record.supporting_accounts_json),
        supporting_products=list(record.supporting_products_json),
        supporting_notes=list(record.supporting_notes_json),
        reviewed_at=record.reviewed_at,
        rejection_reason=record.rejection_reason,
        next_action=record.next_action,
        created_at=record.created_at,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
