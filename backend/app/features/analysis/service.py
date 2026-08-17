"""Ground model output in persisted evidence before any successful write."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.adapters.bailian import BailianError, StructuredModelRequest
from backend.app.db import Database
from backend.app.features.analysis.models import AnalysisRecord, OpportunityRecord
from backend.app.features.analysis.schemas import (
    AnalysisCreate,
    AnalysisEvidenceRead,
    AnalysisOutput,
    AnalysisRead,
    OpportunityRead,
)
from backend.app.features.radar.models import RankItemRecord
from backend.app.models.jobs import JobArtifactRecord, JobRecord
from backend.app.models.jobs import JobState


PROMPT_VERSION = "tutorial-demand-radar-grounded-v1"


class EvidenceNotFound(ValueError):
    pass


class EvidenceAccountMismatch(ValueError):
    pass


class AnalysisNotFound(LookupError):
    pass


class AnalysisService:
    def __init__(self, database: Database, model_adapter: Any) -> None:
        self.database = database
        self.model_adapter = model_adapter

    def create(self, payload: AnalysisCreate) -> AnalysisRead:
        evidence = self._resolve_evidence(payload)
        digest = _digest(payload, evidence)
        if payload.analysis_type == "account_opportunity" and not _eligible_for_opportunity(
            evidence
        ):
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
            output = AnalysisOutput.model_validate(model_result.output)
            _validate_grounding(output, allowed=set(payload.evidence_ids))
            if output.opportunities and not _eligible_for_opportunity(evidence):
                raise ValueError("Opportunity output requires complete deep verification.")
        except BailianError as error:
            return self._persist_failure(
                payload,
                digest=digest,
                status="failed",
                error_category=_model_error_category(error),
                error_detail=str(error),
                attempts=error.attempts,
            )
        except (ValidationError, ValueError) as error:
            return self._persist_failure(
                payload,
                digest=digest,
                status="failed",
                error_category="evidence_grounding_failed",
                error_detail="Model output failed strict schema or evidence grounding.",
            )

        now = _utc_now()
        record = AnalysisRecord(
            analysis_type=payload.analysis_type,
            account_user_id=payload.account_user_id,
            status="succeeded",
            prompt_version=PROMPT_VERSION,
            provider="alibaba_bailian",
            model=model_result.model,
            input_digest=digest,
            evidence_ids_json=list(payload.evidence_ids),
            output_json=output.model_dump(mode="json"),
            usage_json=dict(model_result.usage),
            duration_ms=model_result.duration_ms,
            attempts_json=list(model_result.raw_evidence.get("attempts", [])),
            created_at=now,
        )
        for card in output.opportunities:
            record.opportunities.append(
                OpportunityRecord(
                    title=card.title,
                    status=card.status,
                    summary=card.summary,
                    evidence_ids_json=list(card.evidence_ids),
                    next_action=card.next_action,
                    created_at=now,
                )
            )
        with self.database.session() as session:
            session.add(record)
            session.commit()
            return _analysis_read(_load_analysis(session, record.id))

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
                select(OpportunityRecord).order_by(
                    OpportunityRecord.created_at.desc(), OpportunityRecord.id
                )
            ).all()
            return [_opportunity_read(record) for record in records]

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
                job_input = dict(artifact.job.input_data)
                artifact_account = job_input.get("account_user_id")
                if account_user_id is not None and artifact_account != account_user_id:
                    continue
                fact = {
                    "evidence_id": f"artifact:{artifact.id}",
                    "kind": artifact.kind,
                    "job_state": JobState(artifact.job.state).value,
                    "metadata": dict(artifact.metadata_json),
                }
                rows.append(
                    AnalysisEvidenceRead(
                        evidence_id=fact["evidence_id"],
                        kind=artifact.kind,
                        account_user_id=(
                            artifact_account if isinstance(artifact_account, str) else None
                        ),
                        eligible_for_opportunity=_eligible_for_opportunity([fact]),
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
        return rows

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
            status=status,
            prompt_version=PROMPT_VERSION,
            provider="alibaba_bailian",
            model=getattr(self.model_adapter, "model", "unavailable"),
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

    def _resolve_evidence(self, payload: AnalysisCreate) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        with self.database.session() as session:
            for evidence_id in payload.evidence_ids:
                prefix, separator, raw_id = evidence_id.partition(":")
                if not separator or not raw_id.isdigit():
                    raise EvidenceNotFound(f"Unknown evidence id: {evidence_id}")
                if prefix == "artifact":
                    artifact = session.scalar(
                        select(JobArtifactRecord)
                        .options(selectinload(JobArtifactRecord.job))
                        .where(JobArtifactRecord.id == int(raw_id))
                    )
                    if artifact is None:
                        raise EvidenceNotFound(f"Unknown evidence id: {evidence_id}")
                    job_input = dict(artifact.job.input_data)
                    artifact_account = job_input.get("account_user_id")
                    if (
                        payload.account_user_id
                        and artifact_account
                        and artifact_account != payload.account_user_id
                    ):
                        raise EvidenceAccountMismatch(
                            f"Evidence {evidence_id} belongs to another account."
                        )
                    facts.append(
                        {
                            "evidence_id": evidence_id,
                            "kind": artifact.kind,
                            "job_id": artifact.job_id,
                            "job_state": JobState(artifact.job.state).value,
                            "job_input": job_input,
                            "metadata": dict(artifact.metadata_json),
                        }
                    )
                elif prefix == "rank-item":
                    item = session.get(RankItemRecord, int(raw_id))
                    if item is None:
                        raise EvidenceNotFound(f"Unknown evidence id: {evidence_id}")
                    if (
                        payload.account_user_id
                        and item.user_id
                        and item.user_id != payload.account_user_id
                    ):
                        raise EvidenceAccountMismatch(
                            f"Evidence {evidence_id} belongs to another account."
                        )
                    facts.append(
                        {
                            "evidence_id": evidence_id,
                            "kind": "rank_item",
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
                else:
                    raise EvidenceNotFound(f"Unknown evidence id: {evidence_id}")
        return facts


def _eligible_for_opportunity(evidence: list[dict[str, Any]]) -> bool:
    shop_facts = [fact for fact in evidence if fact["kind"] == "shop_collection_result"]
    if not shop_facts:
        return False
    for fact in shop_facts:
        if fact.get("job_state") != JobState.succeeded.value:
            return False
        result = fact.get("metadata", {}).get("result")
        if (
            not isinstance(result, dict)
            or result.get("status") != "succeeded"
            or result.get("complete") is not True
        ):
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
        if (
            len(set(counts)) != 1
            or counts[0] <= 0
            or verification.get("missing_count") != 0
            or verification.get("overflow_count", 0) != 0
            or verification.get("issues", []) != []
        ):
            return False
        items = result.get("items")
        if not isinstance(items, list) or len(items) != counts[0]:
            return False
        urls = [item.get("source_url") for item in items if isinstance(item, dict)]
        if len(urls) != counts[0] or len(set(urls)) != counts[0]:
            return False
    return True


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


def _model_error_category(error: BailianError) -> str:
    name = type(error).__name__
    mapping = {
        "BailianAuthenticationError": "model_authentication_failed",
        "BailianRetryExhausted": "model_retry_exhausted",
        "ModelOutputInvalid": "model_output_invalid",
        "BailianNotConfigured": "model_unconfigured",
    }
    return mapping.get(name, "model_request_failed")


def _load_analysis(session: Any, analysis_id: str) -> AnalysisRecord:
    record = session.scalar(
        select(AnalysisRecord)
        .options(selectinload(AnalysisRecord.opportunities))
        .where(AnalysisRecord.id == analysis_id)
    )
    if record is None:
        raise AnalysisNotFound(f"Analysis {analysis_id} does not exist.")
    return record


def _analysis_read(record: AnalysisRecord) -> AnalysisRead:
    return AnalysisRead(
        id=record.id,
        analysis_type=record.analysis_type,
        account_user_id=record.account_user_id,
        status=record.status,
        prompt_version=record.prompt_version,
        provider=record.provider,
        model=record.model,
        input_digest=record.input_digest,
        evidence_ids=list(record.evidence_ids_json),
        output=record.output_json,
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
        next_action=record.next_action,
        created_at=record.created_at,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
