"""Ground model output in persisted evidence before any successful write."""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.adapters.contracts import ModelAdapterError, StructuredModelRequest
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
from backend.app.features.shops.service import ANDROID_SHOP_JOB_TYPES, ShopCollectionRead
from backend.app.models.jobs import JobArtifactRecord
from backend.app.models.jobs import JobState


PROMPT_VERSION = "tutorial-demand-radar-grounded-v1"
MAX_TRUSTED_RESULT_BYTES = 5 * 1024 * 1024


class EvidenceNotFound(ValueError):
    pass


class EvidenceAccountMismatch(ValueError):
    pass


class AnalysisNotFound(LookupError):
    pass


class AnalysisService:
    def __init__(
        self, database: Database, model_adapter: Any, *, runtime_dir: Path
    ) -> None:
        self.database = database
        self.model_adapter = model_adapter
        self.runtime_dir = runtime_dir.resolve()

    def create(self, payload: AnalysisCreate) -> AnalysisRead:
        evidence = self._resolve_evidence(payload)
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

        try:
            output = AnalysisOutput.model_validate(model_result.output)
            _validate_grounding(output, allowed=set(payload.evidence_ids))
            if output.opportunities and not eligible:
                raise ValueError("Opportunity output requires complete deep verification.")
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
            analysis_type=payload.analysis_type,
            account_user_id=payload.account_user_id,
            account_user_ids_json=list(payload.account_user_ids),
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

    def _resolve_evidence(self, payload: AnalysisCreate) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        account_scope = payload.account_scope
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
                        not isinstance(artifact_account, str)
                        or not artifact_account.strip()
                        or artifact_account not in account_scope
                    ):
                        raise EvidenceAccountMismatch(
                            f"Evidence {evidence_id} has no matching account ownership."
                        )
                    facts.append(
                        {
                            "evidence_id": evidence_id,
                            "kind": artifact.kind,
                            "job_id": artifact.job_id,
                            "job_state": JobState(artifact.job.state).value,
                            "account_user_id": artifact_account,
                            "job_input": job_input,
                            "trusted_shop_result": self._trusted_shop_result(artifact),
                        }
                    )
                elif prefix == "rank-item":
                    item = session.get(RankItemRecord, int(raw_id))
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
                else:
                    raise EvidenceNotFound(f"Unknown evidence id: {evidence_id}")
        return facts

    def _trusted_shop_result(
        self, artifact: JobArtifactRecord
    ) -> dict[str, Any] | None:
        job = artifact.job
        if (
            job.type not in ANDROID_SHOP_JOB_TYPES
            or artifact.kind != "shop_collection_result"
            or artifact.producer != "android_shop_worker_v1"
            or JobState(job.state) is not JobState.succeeded
        ):
            return None
        expected_path = Path("evidence") / "shops" / job.id / "result.json"
        if artifact.path != expected_path.as_posix():
            return None
        try:
            raw_result = _read_contained_regular_file(
                self.runtime_dir,
                expected_path,
                limit=MAX_TRUSTED_RESULT_BYTES,
            )
            if raw_result is None:
                return None
            file_result = json.loads(raw_result.decode("utf-8", errors="strict"))
            metadata_result = artifact.metadata_json.get("result")
            if not isinstance(file_result, dict) or file_result != metadata_result:
                return None
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
            return None
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
            return None
        return parsed.model_dump(mode="json")


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


def _read_contained_regular_file(
    root: Path, relative_path: Path, *, limit: int
) -> bytes | None:
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
    return payload


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


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
