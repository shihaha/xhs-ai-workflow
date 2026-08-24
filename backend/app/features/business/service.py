"""Business-first read projection over existing durable opportunity/product truth."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.db import Database
from backend.app.features.analysis.models import OpportunityRecord
from backend.app.features.content.export import UnsafeContentPath, read_contained_regular
from backend.app.features.content.models import ProductRecord
from backend.app.features.business.schemas import (
    DemandRadarDirectionRead,
    DemandRadarLinkedProductRead,
    DemandRadarProductRead,
    DemandRadarRead,
    DemandRadarSummaryRead,
)
from backend.app.models.jobs import JobArtifactRecord


_IMAGE_ARTIFACT_KIND = "android_screenshot"
_SHOP_RESULT_ARTIFACT_KIND = "shop_collection_result"
_SHOP_RESULT_PRODUCER = "android_shop_worker_v1"
_SAFE_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
_SAFE_IMAGE_TYPES = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
}
_MAX_IMAGE_BYTES = 12 * 1024 * 1024
_MAX_IMAGE_PIXELS = 24_000_000


class DemandRadarMediaNotFound(LookupError):
    """Requested media is not part of this Opportunity's immutable evidence scope."""


class DemandRadarMediaUnsafe(RuntimeError):
    """An allowed Artifact failed bounded runtime-file/image integrity checks."""


@dataclass(frozen=True)
class DemandRadarImagePayload:
    payload: bytes
    media_type: str
    sha256: str


class BusinessWorkbenchService:
    def __init__(
        self,
        database: Database,
        *,
        runtime_dir: Path | None = None,
        today: Callable[[], date] = date.today,
    ) -> None:
        self.database = database
        resolved_runtime = runtime_dir or database.runtime_dir
        self.runtime_dir = resolved_runtime.resolve() if resolved_runtime is not None else None
        self.today = today

    def demand_radar(self) -> DemandRadarRead:
        generated_on = self.today()
        with self.database.session() as session:
            opportunities = list(
                session.scalars(
                    select(OpportunityRecord)
                    .options(selectinload(OpportunityRecord.analysis))
                    .order_by(OpportunityRecord.created_at.desc(), OpportunityRecord.id)
                )
            )
            products = list(
                session.scalars(
                    select(ProductRecord).order_by(ProductRecord.created_at, ProductRecord.id)
                )
            )

        image_context = self._image_context(opportunities)

        products_by_opportunity: dict[str, list[ProductRecord]] = defaultdict(list)
        for product in products:
            products_by_opportunity[product.opportunity_id].append(product)

        directions = [
            self._direction(
                opportunity,
                linked_products=products_by_opportunity.get(opportunity.id, []),
                generated_on=generated_on,
                image_context=image_context,
            )
            for opportunity in opportunities
        ]
        directions.sort(key=_direction_sort_key)

        return DemandRadarRead(
            summary=DemandRadarSummaryRead(
                generated_on=generated_on,
                total_direction_count=len(directions),
                new_today_count=sum(item.is_new_today for item in directions),
                warming_count=sum(
                    item.evidence_level == "warming_candidate"
                    and item.review_status != "rejected"
                    for item in directions
                ),
                validated_count=sum(
                    item.evidence_level == "validated_candidate"
                    and item.review_status != "rejected"
                    for item in directions
                ),
                pending_decision_count=sum(
                    item.review_status == "pending_review" for item in directions
                ),
                approved_count=sum(item.review_status == "approved" for item in directions),
                linked_product_count=sum(len(item.linked_products) for item in directions),
            ),
            directions=directions,
        )

    def demand_radar_image(
        self, opportunity_id: str, artifact_id: int
    ) -> DemandRadarImagePayload:
        """Return one bounded image only when it belongs to immutable Opportunity evidence."""

        if artifact_id < 1 or self.runtime_dir is None:
            raise DemandRadarMediaNotFound("Demand Radar media is unavailable.")
        with self.database.session() as session:
            opportunity = session.scalar(
                select(OpportunityRecord)
                .options(selectinload(OpportunityRecord.analysis))
                .where(OpportunityRecord.id == opportunity_id)
            )
        if opportunity is None:
            raise DemandRadarMediaNotFound("Demand Radar media is unavailable.")

        image_context = self._image_context([opportunity])
        allowed_ids: set[int] = set()
        for support in opportunity.supporting_products_json or []:
            if not isinstance(support, dict):
                continue
            key = _support_product_key(support)
            if key is None:
                continue
            allowed_ids.update(image_context.get(key, {}).get("image_artifact_ids", []))
        if artifact_id not in allowed_ids:
            raise DemandRadarMediaNotFound("Demand Radar media is unavailable.")

        with self.database.session() as session:
            artifact = session.get(JobArtifactRecord, artifact_id)
        if artifact is None or artifact.kind != _IMAGE_ARTIFACT_KIND:
            raise DemandRadarMediaNotFound("Demand Radar media is unavailable.")

        expected_sha = artifact.metadata_json.get("sha256")
        if not isinstance(expected_sha, str) or len(expected_sha) != 64:
            raise DemandRadarMediaUnsafe("Image Artifact has no trusted digest.")
        if not artifact.path.lower().endswith(_SAFE_IMAGE_SUFFIXES):
            raise DemandRadarMediaUnsafe("Image Artifact uses an unsupported file type.")
        try:
            payload = read_contained_regular(
                self.runtime_dir,
                artifact.path,
                limit=_MAX_IMAGE_BYTES,
            )
        except UnsafeContentPath as error:
            raise DemandRadarMediaUnsafe("Image Artifact is unavailable or unsafe.") from error
        digest = sha256(payload).hexdigest()
        if digest != expected_sha:
            raise DemandRadarMediaUnsafe("Image Artifact digest changed after analysis.")

        try:
            with Image.open(BytesIO(payload)) as image:
                image_format = image.format
                width, height = image.size
                if (
                    image_format not in _SAFE_IMAGE_TYPES
                    or width < 1
                    or height < 1
                    or width * height > _MAX_IMAGE_PIXELS
                    or getattr(image, "is_animated", False)
                ):
                    raise DemandRadarMediaUnsafe("Image Artifact is outside the safe media contract.")
                image.verify()
        except DemandRadarMediaUnsafe:
            raise
        except (OSError, UnidentifiedImageError, ValueError, TypeError) as error:
            raise DemandRadarMediaUnsafe("Image Artifact failed image validation.") from error

        assert image_format is not None
        return DemandRadarImagePayload(
            payload=payload,
            media_type=_SAFE_IMAGE_TYPES[image_format],
            sha256=digest,
        )

    def _direction(
        self,
        opportunity: OpportunityRecord,
        *,
        linked_products: list[ProductRecord],
        generated_on: date,
        image_context: dict[tuple[str, str, str], dict[str, Any]],
    ) -> DemandRadarDirectionRead:
        representative_products = _representative_products(
            opportunity, image_context=image_context
        )
        if opportunity.review_status == "rejected":
            journey_stage = "closed"
            next_business_action = "这个方向已经被拒绝；保留证据用于审计，不进入产品定义。"
        elif opportunity.review_status == "pending_review":
            journey_stage = "demand_decision"
            next_business_action = "查看支撑账号、商品和图片证据后，决定跟进、继续观察或拒绝。"
        elif linked_products:
            # Existing ProductRecord belongs to the pre-Stage-7 content model.  It
            # proves that a product workspace exists, but not that the new
            # Product Definition Gate has been implemented or approved.
            journey_stage = "legacy_product_workspace"
            next_business_action = "已有历史产品工作区；进入 Stage 7 产品定义前先核对现有产品资料。"
        else:
            journey_stage = "product_definition"
            next_business_action = "方向已批准；下一步进入产品研究并形成 Product Definition 草案。"

        return DemandRadarDirectionRead(
            opportunity_id=opportunity.id,
            title=opportunity.title,
            summary=opportunity.summary,
            evidence_level=opportunity.evidence_level,
            review_status=opportunity.review_status,
            can_follow_up=_has_positive_shared_demand(opportunity),
            journey_stage=journey_stage,
            is_new_today=opportunity.created_at.date() == generated_on,
            supporting_account_count=len(opportunity.supporting_accounts_json or []),
            supporting_product_count=len(opportunity.supporting_products_json or []),
            supporting_note_count=len(opportunity.supporting_notes_json or []),
            image_evidence_count=sum(
                max(0, int(item.get("image_evidence_count") or 0))
                for item in opportunity.supporting_products_json or []
                if isinstance(item, dict)
            ),
            representative_products=representative_products,
            linked_products=[
                DemandRadarLinkedProductRead(
                    product_id=product.id,
                    name=product.name,
                    target_user=product.target_user,
                    created_at=product.created_at,
                )
                for product in linked_products
            ],
            next_business_action=next_business_action,
            created_at=opportunity.created_at,
            reviewed_at=opportunity.reviewed_at,
        )

    def _image_context(
        self, opportunities: list[OpportunityRecord]
    ) -> dict[tuple[str, str, str], dict[str, Any]]:
        """Resolve immutable snapshot product image paths to opaque Artifact ids."""

        result_artifact_ids: set[int] = set()
        for opportunity in opportunities:
            snapshot = (
                opportunity.analysis.evidence_snapshot_json
                if opportunity.analysis is not None
                else None
            )
            facts = snapshot.get("facts") if isinstance(snapshot, dict) else None
            if not isinstance(facts, list):
                continue
            for fact in facts:
                if not isinstance(fact, dict):
                    continue
                artifact_id = _artifact_evidence_id(fact.get("evidence_id"))
                if artifact_id is not None:
                    result_artifact_ids.add(artifact_id)
        if not result_artifact_ids:
            return {}

        with self.database.session() as session:
            result_artifacts = list(
                session.scalars(
                    select(JobArtifactRecord).where(
                        JobArtifactRecord.id.in_(result_artifact_ids),
                        JobArtifactRecord.kind == _SHOP_RESULT_ARTIFACT_KIND,
                        JobArtifactRecord.producer == _SHOP_RESULT_PRODUCER,
                    )
                )
            )
            job_ids = {artifact.job_id for artifact in result_artifacts}
            screenshot_artifacts = (
                list(
                    session.scalars(
                        select(JobArtifactRecord).where(
                            JobArtifactRecord.job_id.in_(job_ids),
                            JobArtifactRecord.kind == _IMAGE_ARTIFACT_KIND,
                        )
                    )
                )
                if job_ids
                else []
            )
        result_by_id = {artifact.id: artifact for artifact in result_artifacts}
        screenshot_by_job_path = {
            (artifact.job_id, artifact.path): artifact for artifact in screenshot_artifacts
        }

        context: dict[tuple[str, str, str], dict[str, Any]] = {}
        for opportunity in opportunities:
            snapshot = (
                opportunity.analysis.evidence_snapshot_json
                if opportunity.analysis is not None
                else None
            )
            facts = snapshot.get("facts") if isinstance(snapshot, dict) else None
            if not isinstance(facts, list):
                continue
            for fact in facts:
                if not isinstance(fact, dict):
                    continue
                evidence_id = fact.get("evidence_id")
                result_artifact_id = _artifact_evidence_id(evidence_id)
                result_artifact = result_by_id.get(result_artifact_id or -1)
                trusted = fact.get("trusted_shop_result")
                if (
                    not isinstance(evidence_id, str)
                    or result_artifact is None
                    or not isinstance(trusted, dict)
                ):
                    continue
                items = trusted.get("items")
                evidence_artifacts = trusted.get("evidence_artifacts")
                if not isinstance(items, list) or not isinstance(evidence_artifacts, list):
                    continue
                for product in items:
                    if not isinstance(product, dict):
                        continue
                    data = product.get("data") if isinstance(product.get("data"), dict) else {}
                    raw = (
                        product.get("raw_evidence")
                        if isinstance(product.get("raw_evidence"), dict)
                        else {}
                    )
                    product_id = str(product.get("id") or "")
                    source_url = str(product.get("source_url") or "")
                    key = (evidence_id, product_id, source_url)
                    image_ids: list[int] = []
                    detail_screen = (
                        raw.get("detail_screen")
                        if isinstance(raw.get("detail_screen"), dict)
                        else None
                    )
                    detail_paths = (
                        detail_screen.get("artifacts")
                        if isinstance(detail_screen, dict)
                        and isinstance(detail_screen.get("artifacts"), list)
                        else []
                    )
                    detail_transition = (
                        detail_screen.get("transition")
                        if isinstance(detail_screen, dict)
                        and isinstance(detail_screen.get("transition"), str)
                        else None
                    )
                    detail_sha = (
                        detail_screen.get("screenshot_sha256")
                        if isinstance(detail_screen, dict)
                        and isinstance(detail_screen.get("screenshot_sha256"), str)
                        else None
                    )
                    for raw_path in detail_paths:
                        if (
                            not isinstance(raw_path, str)
                            or raw_path not in evidence_artifacts
                            or not raw_path.lower().endswith(_SAFE_IMAGE_SUFFIXES)
                        ):
                            continue
                        artifact = screenshot_by_job_path.get(
                            (result_artifact.job_id, raw_path)
                        )
                        if artifact is None:
                            continue
                        digest = artifact.metadata_json.get("sha256")
                        transition = artifact.metadata_json.get("transition")
                        if (
                            not isinstance(digest, str)
                            or len(digest) != 64
                            or detail_sha != digest
                            or detail_transition != transition
                        ):
                            continue
                        image_ids.append(artifact.id)
                    context[key] = {
                        "price": data.get("price") or raw.get("price"),
                        "sold": data.get("sold") or raw.get("sold"),
                        "image_artifact_ids": image_ids,
                    }
        return context


def _representative_products(
    opportunity: OpportunityRecord,
    *,
    image_context: dict[tuple[str, str, str], dict[str, Any]],
) -> list[DemandRadarProductRead]:
    fallback = _snapshot_product_facts(opportunity)
    rows: list[DemandRadarProductRead] = []
    for item in (opportunity.supporting_products_json or [])[:3]:
        if not isinstance(item, dict):
            continue
        source_url = str(item.get("source_url") or "")
        product_id = str(item.get("product_id") or "")
        account_user_id = str(item.get("account_user_id") or "")
        key = (str(item.get("evidence_id") or ""), product_id, source_url)
        observed = image_context.get(key) or fallback.get(key, {})
        rows.append(
            DemandRadarProductRead(
                account_user_id=account_user_id,
                product_id=product_id,
                title=item.get("title") if isinstance(item.get("title"), str) else None,
                source_url=source_url,
                price=_safe_text(observed.get("price")),
                sold=_safe_text(observed.get("sold")),
                image_evidence_count=max(0, int(item.get("image_evidence_count") or 0)),
                image_artifact_ids=[
                    int(artifact_id)
                    for artifact_id in observed.get("image_artifact_ids", [])
                    if isinstance(artifact_id, int) and not isinstance(artifact_id, bool)
                ],
            )
        )
    return rows


def _artifact_evidence_id(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    prefix, separator, raw_id = value.partition(":")
    if prefix != "artifact" or separator != ":" or not raw_id.isdigit():
        return None
    artifact_id = int(raw_id)
    return artifact_id if artifact_id > 0 else None


def _support_product_key(item: dict[str, Any]) -> tuple[str, str, str] | None:
    evidence_id = item.get("evidence_id")
    if not isinstance(evidence_id, str) or _artifact_evidence_id(evidence_id) is None:
        return None
    return (
        evidence_id,
        str(item.get("product_id") or ""),
        str(item.get("source_url") or ""),
    )


def _snapshot_product_facts(opportunity: OpportunityRecord) -> dict[tuple[str, str, str], dict[str, Any]]:
    snapshot = opportunity.analysis.evidence_snapshot_json if opportunity.analysis is not None else None
    facts = snapshot.get("facts") if isinstance(snapshot, dict) else None
    if not isinstance(facts, list):
        return {}
    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        evidence_id = fact.get("evidence_id")
        trusted = fact.get("trusted_shop_result")
        items = trusted.get("items") if isinstance(trusted, dict) else None
        if not isinstance(evidence_id, str) or not isinstance(items, list):
            continue
        for product in items:
            if not isinstance(product, dict):
                continue
            data = product.get("data") if isinstance(product.get("data"), dict) else {}
            raw = product.get("raw_evidence") if isinstance(product.get("raw_evidence"), dict) else {}
            product_id = str(product.get("id") or "")
            source_url = str(product.get("source_url") or "")
            result[(evidence_id, product_id, source_url)] = {
                "price": data.get("price") or raw.get("price"),
                "sold": data.get("sold") or raw.get("sold"),
            }
    return result


def _safe_text(value: object) -> str | None:
    if not isinstance(value, (str, int, float)):
        return None
    rendered = str(value).strip()
    return rendered[:200] if rendered else None


def _has_positive_shared_demand(opportunity: OpportunityRecord) -> bool:
    output = opportunity.analysis.output_json if opportunity.analysis is not None else None
    conclusion = output.get("cross_account_conclusion") if isinstance(output, dict) else None
    return bool(
        isinstance(conclusion, dict)
        and conclusion.get("has_specific_shared_demand") is True
    )


def _direction_sort_key(item: DemandRadarDirectionRead) -> tuple[int, int, float, str]:
    review_priority = {"pending_review": 0, "approved": 1, "rejected": 2}[item.review_status]
    evidence_priority = {
        "validated_candidate": 0,
        "warming_candidate": 1,
        "legacy_ungraded": 2,
    }[item.evidence_level]
    return (review_priority, evidence_priority, -item.created_at.timestamp(), item.opportunity_id)
