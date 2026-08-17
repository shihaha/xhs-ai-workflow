"""Transactional, evidence-grounded content production service."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from backend.app.adapters.contracts import ModelAdapter, ModelAdapterError, StructuredModelRequest
from backend.app.db import Database
from backend.app.features.analysis.models import OpportunityRecord
from backend.app.features.radar.models import RankItemRecord
from backend.app.models.jobs import JobArtifactRecord
from backend.app.features.content.export import (
    UnsafeContentPath, deterministic_zip, entry_manifest, read_contained_regular,
    write_contained_atomic,
)
from backend.app.features.content.models import (
    ContentItemRecord, ContentPackageRecord, ContentReviewRecord, ContentRevisionRecord,
    ProductMaterialRecord, ProductRecord,
)
from backend.app.features.content.schemas import (
    ContentDraftOutput, ContentItemCreate, ContentItemRead, ContentPackageRead,
    MaterialCreate, MaterialRead, ProductCreate, ProductRead, ReviewCreate, ReviewRead,
    RevisionRead,
)
from backend.app.features.content.templates import TEMPLATE_SEEDS


PROMPT_VERSION = "content-production-v1"
_SAFE_ATTEMPT = re.compile(r"^(?:timeout|network|authentication|response_received|http_(?:408|429|5[0-9]{2}))$")


class ContentError(RuntimeError):
    pass


class ContentNotFound(ContentError):
    pass


class ContentValidationError(ContentError):
    pass


class ContentStateError(ContentError):
    pass


class ContentService:
    def __init__(self, database: Database, model_adapter: ModelAdapter, *, runtime_dir: Path) -> None:
        self.database = database
        self.model_adapter = model_adapter
        self.runtime_dir = runtime_dir

    def create_product(self, payload: ProductCreate) -> ProductRead:
        with self.database.session() as session:
            opportunity = session.get(OpportunityRecord, payload.opportunity_id)
            if (
                opportunity is None
                or opportunity.analysis.status != "succeeded"
                or opportunity.analysis.output_json is None
                or not set(opportunity.evidence_ids_json).issubset(
                    set(opportunity.analysis.evidence_ids_json)
                )
            ):
                raise ContentValidationError("Product requires a persisted successful opportunity.")
            record = ProductRecord(
                opportunity_id=payload.opportunity_id, name=payload.name.strip(),
                target_user=payload.target_user.strip(), created_at=_now(),
            )
            session.add(record)
            session.commit()
            return _product_read(_load_product(session, record.id))

    def list_products(self) -> list[ProductRead]:
        with self.database.session() as session:
            records = session.scalars(
                select(ProductRecord).options(selectinload(ProductRecord.materials)).order_by(ProductRecord.created_at, ProductRecord.id)
            ).all()
            return [_product_read(record) for record in records]

    def get_product(self, product_id: str) -> ProductRead:
        with self.database.session() as session:
            return _product_read(_load_product(session, product_id))

    def add_material(self, product_id: str, payload: MaterialCreate) -> MaterialRead:
        try:
            data = read_contained_regular(self.runtime_dir, payload.path)
        except UnsafeContentPath as error:
            raise ContentValidationError(str(error)) from error
        with self.database.session() as session:
            if session.get(ProductRecord, product_id) is None:
                raise ContentNotFound(f"Product {product_id} does not exist.")
            version = int(session.scalar(
                select(func.coalesce(func.max(ProductMaterialRecord.version), 0)).where(
                    ProductMaterialRecord.product_id == product_id,
                    ProductMaterialRecord.logical_name == payload.logical_name,
                )
            )) + 1
            record = ProductMaterialRecord(
                product_id=product_id, logical_name=payload.logical_name, version=version,
                path="pending",
                sha256=sha256(data).hexdigest(), size_bytes=len(data),
                media_type=payload.media_type, created_at=_now(),
            )
            session.add(record)
            session.flush()
            suffix = Path(payload.logical_name).suffix[:20]
            record.path = f"content-materials/{product_id}/{record.id}/source{suffix}"
            try:
                write_contained_atomic(self.runtime_dir, record.path, data)
                session.commit()
            except UnsafeContentPath as error:
                raise ContentValidationError(str(error)) from error
            except Exception:
                raise
            return _material_read(record)

    def create_content_item(self, payload: ContentItemCreate) -> ContentItemRead:
        with self.database.session() as session:
            product = session.get(ProductRecord, payload.product_id)
            if product is None:
                raise ContentNotFound(f"Product {payload.product_id} does not exist.")
            if product.opportunity_id != payload.opportunity_id:
                raise ContentValidationError("Opportunity does not belong to this product.")
            opportunity = session.get(OpportunityRecord, payload.opportunity_id)
            assert opportunity is not None
            if (
                opportunity.analysis.status != "succeeded"
                or opportunity.analysis.output_json is None
                or not set(opportunity.evidence_ids_json).issubset(
                    set(opportunity.analysis.evidence_ids_json)
                )
            ):
                raise ContentValidationError("Product opportunity grounding is no longer valid.")
            allowed = set(opportunity.evidence_ids_json)
            self._validate_request_scope(payload, allowed=allowed, session=session)
        output, model_meta = self._generate(payload, revision_number=1, prior=None, review_notes=[])
        self._validate_output(output, allowed=set(payload.evidence_ids))
        now = _now()
        with self.database.session() as session:
            record = ContentItemRecord(
                product_id=payload.product_id, opportunity_id=payload.opportunity_id,
                template_key=payload.template_key, status="research",
                evidence_ids_json=list(payload.evidence_ids), material_ids_json=list(payload.material_ids),
                research_facts_json=[item.model_dump(mode="json") for item in payload.research_facts],
                current_revision_id=None, created_at=now, updated_at=now,
            )
            session.add(record)
            session.flush()
            revision = self._revision(record.id, 1, output, model_meta)
            session.add(revision)
            session.flush()
            record.current_revision_id = revision.id
            record.status = "review"
            record.updated_at = _now()
            session.commit()
            return _item_read(_load_item(session, record.id))

    def list_content_items(self) -> list[ContentItemRead]:
        with self.database.session() as session:
            records = session.scalars(
                _items_query().order_by(ContentItemRecord.created_at, ContentItemRecord.id)
            ).all()
            return [_item_read(record) for record in records]

    def get_content_item(self, item_id: str) -> ContentItemRead:
        with self.database.session() as session:
            return _item_read(_load_item(session, item_id))

    def review(self, item_id: str, payload: ReviewCreate) -> ContentItemRead:
        with self.database.session() as session:
            record = _load_item(session, item_id)
            if record.status != "review" or not record.current_revision_id:
                raise ContentStateError("Only the current review revision can be approved or rejected.")
            decision_status = "approved" if payload.decision == "approve" else "rejected"
            session.add(ContentReviewRecord(
                content_item_id=record.id, revision_id=record.current_revision_id,
                decision=payload.decision, actor=payload.actor.strip(), note=payload.note.strip(),
                created_at=_now(),
            ))
            record.status = decision_status
            record.updated_at = _now()
            session.commit()
            session.expire_all()
            return _item_read(_load_item(session, record.id))

    def regenerate(self, item_id: str) -> ContentItemRead:
        with self.database.session() as session:
            record = _load_item(session, item_id)
            if record.status != "rejected" or not record.current_revision_id:
                raise ContentStateError("Only a rejected current revision can be regenerated.")
            payload = ContentItemCreate(
                product_id=record.product_id, opportunity_id=record.opportunity_id,
                template_key=record.template_key, evidence_ids=list(record.evidence_ids_json),
                material_ids=list(record.material_ids_json), research_facts=list(record.research_facts_json),
            )
            prior = next(item for item in record.revisions if item.id == record.current_revision_id)
            notes = [review.note for review in record.reviews if review.revision_id == prior.id and review.decision == "reject"]
            number = max(item.number for item in record.revisions) + 1
            prior_snapshot = {"title": prior.title, "body": prior.body}
        output, model_meta = self._generate(payload, revision_number=number, prior=prior_snapshot, review_notes=notes)
        self._validate_output(output, allowed=set(payload.evidence_ids))
        with self.database.session() as session:
            record = _load_item(session, item_id)
            if record.status != "rejected" or record.current_revision_id != prior.id:
                raise ContentStateError("Content item changed during regeneration.")
            revision = self._revision(record.id, number, output, model_meta)
            session.add(revision)
            session.flush()
            session.add(ContentReviewRecord(
                content_item_id=record.id, revision_id=prior.id, decision="regenerate",
                actor="system", note="Regenerated from the rejected revision and review notes.",
                created_at=_now(),
            ))
            record.current_revision_id = revision.id
            record.status = "review"
            record.updated_at = _now()
            session.commit()
            session.expire_all()
            return _item_read(_load_item(session, item_id))

    def export_package(self, item_id: str) -> ContentPackageRead:
        with self.database.session() as session:
            item = _load_item(session, item_id)
            if item.status not in {"approved", "exported"} or not item.current_revision_id:
                raise ContentStateError("An approved current revision is required for export.")
            approval = session.scalar(select(ContentReviewRecord).where(
                ContentReviewRecord.content_item_id == item.id,
                ContentReviewRecord.revision_id == item.current_revision_id,
                ContentReviewRecord.decision == "approve",
            ))
            if approval is None:
                raise ContentStateError("The current revision has no approval decision.")
            existing = session.scalar(select(ContentPackageRecord).where(ContentPackageRecord.revision_id == item.current_revision_id))
            if existing is not None:
                try:
                    payload = read_contained_regular(self.runtime_dir, existing.path)
                except UnsafeContentPath as error:
                    raise ContentValidationError("Existing package artifact is unavailable.") from error
                if sha256(payload).hexdigest() != existing.sha256 or len(payload) != existing.size_bytes:
                    raise ContentValidationError("Existing package artifact no longer matches its record.")
                return _package_read(existing)
            revision = next(value for value in item.revisions if value.id == item.current_revision_id)
            product = _load_product(session, item.product_id)
            materials = []
            for material_id in item.material_ids_json:
                material = session.get(ProductMaterialRecord, material_id)
                if material is None or material.product_id != item.product_id:
                    raise ContentValidationError("Approved material is missing or foreign to the product.")
                materials.append(material)
            entries = self._package_entries(item, revision, product, materials)
            manifest = {
                "schema_version": 1, "content_item_id": item.id,
                "revision_id": revision.id, "product_id": product.id,
                "opportunity_id": item.opportunity_id, "entries": entry_manifest(entries),
                "materials": [
                    {
                        "id": material.id,
                        "logical_name": material.logical_name,
                        "version": material.version,
                        "media_type": material.media_type,
                        "sha256": material.sha256,
                        "entry_path": f"materials/{material.id}/{material.logical_name}",
                    }
                    for material in sorted(materials, key=lambda value: value.id)
                ],
                "automatic_publish": False,
            }
            archive = deterministic_zip(entries, manifest)
            package = ContentPackageRecord(
                content_item_id=item.id, revision_id=revision.id, status="ready",
                path=f"content-packages/{item.id}/{revision.id}.zip",
                sha256=sha256(archive).hexdigest(), size_bytes=len(archive), created_at=_now(),
            )
            try:
                write_contained_atomic(self.runtime_dir, package.path, archive)
            except UnsafeContentPath as error:
                raise ContentValidationError("Package output path is unsafe or unavailable.") from error
            session.add(package)
            item.status = "exported"
            item.updated_at = _now()
            session.commit()
            return _package_read(package)

    def list_packages(self) -> list[ContentPackageRead]:
        with self.database.session() as session:
            return [_package_read(record) for record in session.scalars(
                select(ContentPackageRecord).order_by(ContentPackageRecord.created_at, ContentPackageRecord.id)
            ).all()]

    def get_package(self, package_id: str) -> ContentPackageRead:
        with self.database.session() as session:
            record = session.get(ContentPackageRecord, package_id)
            if record is None:
                raise ContentNotFound(f"Content package {package_id} does not exist.")
            return _package_read(record)

    def _validate_request_scope(self, payload: ContentItemCreate, *, allowed: set[str], session: Any) -> None:
        if not set(payload.evidence_ids).issubset(allowed):
            raise ContentValidationError("Every content source must belong to the product opportunity.")
        for fact in payload.research_facts:
            if not set(fact.evidence_ids).issubset(set(payload.evidence_ids)):
                raise ContentValidationError("Every research fact must cite this content request scope.")
        for evidence_id in payload.evidence_ids:
            prefix, _, raw_id = evidence_id.partition(":")
            model = JobArtifactRecord if prefix == "artifact" else RankItemRecord
            if session.get(model, int(raw_id)) is None:
                raise ContentValidationError("Every citation must identify a persisted evidence record.")
        for material_id in payload.material_ids:
            material = session.get(ProductMaterialRecord, material_id)
            if material is None or material.product_id != payload.product_id:
                raise ContentValidationError("Every material must belong to this product.")

    def _generate(self, payload: ContentItemCreate, *, revision_number: int, prior: dict[str, str] | None, review_notes: list[str]) -> tuple[ContentDraftOutput, dict[str, Any]]:
        if getattr(self.model_adapter, "configured", False) is not True:
            raise ContentValidationError("Model provider is not configured.")
        prompt_data = {
            "template_key": payload.template_key,
            "template_seed": TEMPLATE_SEEDS.get(payload.template_key, {
                "version": "operator-template-v1",
                "rules": ["Use only explicitly cited product evidence."],
            }),
            "research_facts": [item.model_dump(mode="json") for item in payload.research_facts],
            "allowed_evidence_ids": payload.evidence_ids,
            "revision_number": revision_number,
            "prior_revision": prior,
            "review_notes": review_notes,
        }
        request = StructuredModelRequest(
            system_prompt=(
                "Return one JSON object matching the schema. Use only research facts and allowed "
                "evidence IDs. Every claim must cite canonical allowed IDs. Create a draft for "
                "human review; do not publish or claim it was published."
            ),
            user_prompt=json.dumps(prompt_data, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            prompt_version=PROMPT_VERSION, evidence_ids=list(payload.evidence_ids),
        )
        try:
            result = self.model_adapter.generate_structured(request, ContentDraftOutput)
            output = ContentDraftOutput.model_validate(result.output)
        except (ModelAdapterError, TimeoutError) as error:
            raise ContentValidationError("Content model generation failed; no revision was created.") from error
        except ValidationError as error:
            raise ContentValidationError("Content model returned invalid structured output.") from error
        return output, {
            "provider": _safe_identifier(getattr(self.model_adapter, "provider", None), limit=100),
            "model": _safe_identifier(result.model, limit=300), "usage": _safe_usage(result.usage),
            "attempts": _safe_attempts(result.raw_evidence),
        }

    @staticmethod
    def _validate_output(output: ContentDraftOutput, *, allowed: set[str]) -> None:
        if not set(output.source_evidence_ids).issubset(allowed):
            raise ContentValidationError("Draft source list escaped the request evidence scope.")
        declared_sources = set(output.source_evidence_ids)
        for claim in output.claims:
            if not set(claim.evidence_ids).issubset(allowed):
                raise ContentValidationError("A generated claim is ungrounded or cross-request.")
            if not set(claim.evidence_ids).issubset(declared_sources):
                raise ContentValidationError("A generated claim citation is absent from the source list.")

    @staticmethod
    def _revision(item_id: str, number: int, output: ContentDraftOutput, model_meta: dict[str, Any]) -> ContentRevisionRecord:
        return ContentRevisionRecord(
            content_item_id=item_id, number=number, title=output.title, body=output.body,
            claims_json=[item.model_dump(mode="json") for item in output.claims],
            source_evidence_ids_json=list(output.source_evidence_ids),
            model_provider=model_meta["provider"], model_name=model_meta["model"],
            prompt_version=PROMPT_VERSION, usage_json=model_meta["usage"],
            attempts_json=model_meta["attempts"], created_at=_now(),
        )

    def _package_entries(self, item: ContentItemRecord, revision: ContentRevisionRecord, product: ProductRecord, materials: list[ProductMaterialRecord]) -> dict[str, bytes]:
        entries: dict[str, bytes] = {
            "content/final.md": f"# {revision.title}\n\n{revision.body}\n".encode("utf-8"),
            "sources/evidence.json": _json_bytes({
                "opportunity_id": item.opportunity_id,
                "evidence_ids": list(revision.source_evidence_ids_json),
                "claims": list(revision.claims_json),
                "research_facts": list(item.research_facts_json),
            }),
            "reviews/history.json": _json_bytes([
                {"id": review.id, "revision_id": review.revision_id, "decision": review.decision,
                 "actor": review.actor, "note": review.note, "created_at": review.created_at.isoformat()}
                for review in item.reviews
            ]),
            "product/product.json": _json_bytes({
                "id": product.id, "name": product.name, "target_user": product.target_user,
                "opportunity_id": product.opportunity_id,
            }),
        }
        for material in sorted(materials, key=lambda value: value.id):
            try:
                content = read_contained_regular(self.runtime_dir, material.path)
            except UnsafeContentPath as error:
                raise ContentValidationError("An approved material is unavailable or unsafe.") from error
            if sha256(content).hexdigest() != material.sha256 or len(content) != material.size_bytes:
                raise ContentValidationError("An approved material changed after its immutable version was recorded.")
            name = f"materials/{material.id}/{material.logical_name}"
            if name in entries or name.casefold() in {value.casefold() for value in entries}:
                raise ContentValidationError("Approved material export path collision.")
            entries[name] = content
        return entries


def _items_query():
    return select(ContentItemRecord).options(
        selectinload(ContentItemRecord.revisions), selectinload(ContentItemRecord.reviews)
    )


def _load_item(session: Any, item_id: str) -> ContentItemRecord:
    record = session.scalar(_items_query().where(ContentItemRecord.id == item_id))
    if record is None:
        raise ContentNotFound(f"Content item {item_id} does not exist.")
    return record


def _load_product(session: Any, product_id: str) -> ProductRecord:
    record = session.scalar(select(ProductRecord).options(selectinload(ProductRecord.materials)).where(ProductRecord.id == product_id))
    if record is None:
        raise ContentNotFound(f"Product {product_id} does not exist.")
    return record


def _material_read(record: ProductMaterialRecord) -> MaterialRead:
    return MaterialRead.model_validate(record, from_attributes=True)


def _product_read(record: ProductRecord) -> ProductRead:
    return ProductRead(
        id=record.id, name=record.name, target_user=record.target_user,
        opportunity_id=record.opportunity_id,
        materials=[_material_read(item) for item in record.materials], created_at=record.created_at,
    )


def _revision_read(record: ContentRevisionRecord) -> RevisionRead:
    return RevisionRead(
        id=record.id, number=record.number, title=record.title, body=record.body,
        claims=list(record.claims_json), source_evidence_ids=list(record.source_evidence_ids_json),
        model_provider=record.model_provider, model_name=record.model_name,
        prompt_version=record.prompt_version, usage=dict(record.usage_json),
        attempts=list(record.attempts_json), created_at=record.created_at,
    )


def _item_read(record: ContentItemRecord) -> ContentItemRead:
    revisions = [_revision_read(item) for item in record.revisions]
    current = next((item for item in revisions if item.id == record.current_revision_id), None)
    return ContentItemRead(
        id=record.id, product_id=record.product_id, opportunity_id=record.opportunity_id,
        template_key=record.template_key, status=record.status,
        evidence_ids=list(record.evidence_ids_json), material_ids=list(record.material_ids_json),
        research_facts=list(record.research_facts_json), current_revision=current,
        revisions=revisions,
        reviews=[ReviewRead(
            id=item.id, revision_id=item.revision_id, decision=item.decision,
            actor=item.actor, note=item.note, created_at=item.created_at,
        ) for item in record.reviews],
        created_at=record.created_at, updated_at=record.updated_at,
    )


def _package_read(record: ContentPackageRecord) -> ContentPackageRead:
    return ContentPackageRead.model_validate(record, from_attributes=True)


def _safe_usage(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    allowed = {
        "prompt_tokens", "completion_tokens", "total_tokens", "input_tokens",
        "output_tokens", "cached_tokens",
    }
    for key, item in value.items():
        if key not in allowed:
            continue
        if isinstance(item, bool) or not isinstance(item, int) or item < 0 or item > 1_000_000_000:
            continue
        result[key] = item
    return result


def _safe_identifier(value: object, *, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        return "unknown"
    if re.fullmatch(r"[A-Za-z0-9._-]+", value, re.ASCII) is None:
        return "unknown"
    return value


def _safe_attempts(raw: object) -> list[dict[str, object]]:
    if not isinstance(raw, dict) or not isinstance(raw.get("attempts"), list):
        return []
    result = []
    for item in raw["attempts"]:
        if not isinstance(item, dict):
            continue
        attempt, category = item.get("attempt"), item.get("category")
        if isinstance(attempt, int) and not isinstance(attempt, bool) and 1 <= attempt <= 1000 and isinstance(category, str) and _SAFE_ATTEMPT.fullmatch(category):
            result.append({"attempt": attempt, "category": category})
            if len(result) == 20:
                break
    return result


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
