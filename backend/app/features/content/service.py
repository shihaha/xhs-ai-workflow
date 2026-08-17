"""Transactional, evidence-grounded content production service."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import selectinload

from backend.app.adapters.contracts import ModelAdapter, ModelAdapterError, StructuredModelRequest
from backend.app.db import Database
from backend.app.features.analysis.models import OpportunityRecord
from backend.app.features.radar.models import RankItemRecord
from backend.app.models.jobs import JobArtifactRecord
from backend.app.features.content.export import (
    MAX_PACKAGE_BYTES, UnsafeContentPath, deterministic_zip, entry_manifest,
    read_contained_regular, remove_contained_regular, write_contained_atomic,
    windows_artifact_reference, windows_artifact_references_conflict,
)
from backend.app.features.content.models import (
    ContentItemRecord, ContentPackageRecord, ContentReviewRecord, ContentRevisionRecord,
    ProductMaterialRecord, ProductRecord,
)
from backend.app.features.content.schemas import (
    ContentDraftOutput, ContentItemCreate, ContentItemRead, ContentPackageRead,
    ExportCreate, MaterialCreate, MaterialRead, ProductCreate, ProductRead,
    RegenerateCreate, ReviewCreate, ReviewRead,
    RevisionRead,
    windows_name_key,
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


class ContentModelUnavailable(ContentError):
    pass


class ContentModelFailure(ContentError):
    pass


class ContentService:
    def __init__(self, database: Database, model_adapter: ModelAdapter, *, runtime_dir: Path) -> None:
        self.database = database
        self.model_adapter = model_adapter
        self.runtime_dir = runtime_dir

    def create_product(self, payload: ProductCreate) -> ProductRead:
        with self.database.session() as session:
            opportunity = session.get(OpportunityRecord, payload.opportunity_id)
            self._validate_opportunity(opportunity, session=session)
            record = ProductRecord(
                opportunity_id=payload.opportunity_id, name=payload.name.strip(),
                target_user=payload.target_user.strip(), created_at=_now(),
            )
            session.add(record)
            session.commit()
            return self._product_read(_load_product(session, record.id))

    def list_products(self) -> list[ProductRead]:
        with self.database.session() as session:
            records = session.scalars(
                select(ProductRecord).options(selectinload(ProductRecord.materials)).order_by(ProductRecord.created_at, ProductRecord.id)
            ).all()
            return [self._product_read(record) for record in records]

    def get_product(self, product_id: str) -> ProductRead:
        with self.database.session() as session:
            return self._product_read(_load_product(session, product_id))

    def add_material(self, product_id: str, payload: MaterialCreate) -> MaterialRead:
        try:
            data = read_contained_regular(self.runtime_dir, payload.path)
        except UnsafeContentPath as error:
            raise ContentValidationError(str(error)) from error
        detected = _validate_material_bytes(data, declared=payload.media_type, kind=payload.kind)
        managed_path: str | None = None
        with self.database.session() as session:
            if session.get(ProductRecord, product_id) is None:
                raise ContentNotFound(f"Product {product_id} does not exist.")
            count, aggregate = session.execute(
                select(func.count(ProductMaterialRecord.id), func.coalesce(func.sum(ProductMaterialRecord.size_bytes), 0)).where(
                    ProductMaterialRecord.product_id == product_id
                )
            ).one()
            if count >= 100 or int(aggregate) + len(data) > 200 * 1024 * 1024:
                raise ContentValidationError("Product material count or aggregate size limit exceeded.")
            logical_key = windows_name_key(payload.logical_name)
            version = int(session.scalar(
                select(func.coalesce(func.max(ProductMaterialRecord.version), 0)).where(
                    ProductMaterialRecord.product_id == product_id,
                    ProductMaterialRecord.logical_key == logical_key,
                )
            )) + 1
            record_id = str(uuid4())
            suffix = Path(payload.logical_name).suffix[:20]
            managed_path = (
                f"content-materials/{product_id}/{record_id}/source{suffix}"
            )
            self._assert_path_not_quarantined(session, managed_path)
            record = ProductMaterialRecord(
                id=record_id,
                product_id=product_id, logical_name=payload.logical_name, version=version,
                logical_key=logical_key,
                path=managed_path,
                sha256=sha256(data).hexdigest(), size_bytes=len(data),
                media_type=detected, kind=payload.kind, created_at=_now(),
            )
            session.add(record)
            session.flush()
            try:
                write_contained_atomic(self.runtime_dir, record.path, data)
                session.commit()
            except UnsafeContentPath as error:
                session.rollback()
                self._cleanup_unpersisted_material(record.id, managed_path)
                raise ContentValidationError(str(error)) from error
            except IntegrityError as error:
                session.rollback()
                self._cleanup_unpersisted_material(record.id, managed_path)
                raise ContentStateError("Concurrent material version conflict; retry the request.") from error
            except Exception:
                session.rollback()
                self._cleanup_unpersisted_material(record.id, managed_path)
                raise
            return self._material_read(record)

    def _cleanup_unpersisted_material(self, material_id: str, managed_path: str) -> None:
        """Delete only when a fresh connection proves no database row owns the file."""
        try:
            with self.database.engine.connect() as connection:
                persisted = connection.exec_driver_sql(
                    "SELECT path FROM content_product_materials WHERE id=?", (material_id,)
                ).scalar_one_or_none()
                if persisted is not None:
                    return
                references = connection.exec_driver_sql(
                    "SELECT path FROM content_product_materials UNION ALL SELECT path FROM content_packages"
                ).scalars().all()
            candidate = windows_artifact_reference(self.runtime_dir, managed_path)
            if candidate is None:
                return
            if any(
                isinstance(path, str)
                and windows_artifact_references_conflict(
                    candidate, windows_artifact_reference(self.runtime_dir, path)
                )
                for path in references
            ):
                return
        except (SQLAlchemyError, OSError, ValueError, TypeError):
            return
        remove_contained_regular(self.runtime_dir, managed_path)

    def create_content_item(self, payload: ContentItemCreate) -> ContentItemRead:
        with self.database.session() as session:
            product = session.get(ProductRecord, payload.product_id)
            if product is None:
                raise ContentNotFound(f"Product {payload.product_id} does not exist.")
            if product.opportunity_id != payload.opportunity_id:
                raise ContentValidationError("Opportunity does not belong to this product.")
            opportunity = session.get(OpportunityRecord, payload.opportunity_id)
            assert opportunity is not None
            self._validate_opportunity(opportunity, session=session)
            allowed = set(opportunity.evidence_ids_json)
            self._validate_request_scope(payload, allowed=allowed, session=session)
            generation_context = self._generation_context(product, payload, session)
        output, model_meta = self._generate(payload, revision_number=1, prior=None, review_notes=[], generation_context=generation_context)
        self._validate_output(output, allowed=set(payload.evidence_ids), image_ids=payload.image_material_ids)
        now = _now()
        with self.database.session() as session:
            record = ContentItemRecord(
                product_id=payload.product_id, opportunity_id=payload.opportunity_id,
                template_key=payload.template_key, status="research",
                evidence_ids_json=list(payload.evidence_ids), material_ids_json=list(payload.material_ids),
                image_material_ids_json=list(payload.image_material_ids),
                cover_material_id=payload.cover_material_id,
                research_facts_json=[item.model_dump(mode="json") for item in payload.research_facts],
                current_revision_id=None, created_at=now, updated_at=now,
            )
            session.add(record)
            session.flush()
            opportunity = session.get(OpportunityRecord, payload.opportunity_id)
            self._validate_opportunity(opportunity, session=session)
            self._validate_request_scope(payload, allowed=set(opportunity.evidence_ids_json), session=session)
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
            return [self._item_projection(record, session=session) for record in records]

    def get_content_item(self, item_id: str) -> ContentItemRead:
        with self.database.session() as session:
            return self._item_projection(_load_item(session, item_id), session=session)

    def review(self, item_id: str, payload: ReviewCreate) -> ContentItemRead:
        with self.database.session() as session:
            record = _load_item(session, item_id)
            self._validate_item_trust(record, session=session)
            if record.current_revision_id != payload.expected_revision_id:
                raise ContentStateError("The expected revision is stale.")
            self._validate_visual_checks(record, payload)
            decision_status = "approved" if payload.decision == "approve" else "rejected"
            won = session.execute(
                update(ContentItemRecord).where(
                    ContentItemRecord.id == item_id,
                    ContentItemRecord.status == "review",
                    ContentItemRecord.current_revision_id == payload.expected_revision_id,
                ).values(status=decision_status, updated_at=_now())
            ).rowcount
            if won != 1:
                session.rollback()
                raise ContentStateError("The content item changed before this review could commit.")
            session.add(ContentReviewRecord(
                content_item_id=record.id, revision_id=payload.expected_revision_id,
                decision=payload.decision, actor=payload.actor.strip(), note=payload.note.strip(),
                visual_checks_json=[item.model_dump(mode="json") for item in payload.visual_checks],
                created_at=_now(),
            ))
            try:
                session.commit()
            except IntegrityError as error:
                session.rollback()
                raise ContentStateError("This revision already has a terminal review.") from error
            session.expire_all()
            return _item_read(_load_item(session, record.id))

    def regenerate(self, item_id: str, payload_request: RegenerateCreate) -> ContentItemRead:
        with self.database.session() as session:
            record = _load_item(session, item_id)
            self._validate_item_trust(record, session=session)
            if record.current_revision_id != payload_request.expected_revision_id:
                raise ContentStateError("The expected revision is stale.")
            if record.status != "rejected" or not record.current_revision_id:
                raise ContentStateError("Only a rejected current revision can be regenerated.")
            payload = ContentItemCreate(
                product_id=record.product_id, opportunity_id=record.opportunity_id,
                template_key=record.template_key, evidence_ids=list(record.evidence_ids_json),
                material_ids=list(record.material_ids_json), research_facts=list(record.research_facts_json),
                image_material_ids=list(record.image_material_ids_json),
                cover_material_id=record.cover_material_id,
            )
            prior = next(item for item in record.revisions if item.id == record.current_revision_id)
            notes = [review.note for review in record.reviews if review.revision_id == prior.id and review.decision == "reject"]
            number = max(item.number for item in record.revisions) + 1
            prior_snapshot = {"title": prior.title, "body": prior.body}
            context = self._generation_context(session.get(ProductRecord, record.product_id), payload, session)
            won = session.execute(update(ContentItemRecord).where(
                ContentItemRecord.id == item_id,
                ContentItemRecord.status == "rejected",
                ContentItemRecord.current_revision_id == payload_request.expected_revision_id,
            ).values(status="draft", updated_at=_now())).rowcount
            if won != 1:
                session.rollback()
                raise ContentStateError("Another regeneration already reserved this revision.")
            session.add(ContentReviewRecord(
                content_item_id=record.id, revision_id=prior.id, decision="regenerate",
                actor="system", note="Regeneration reserved from the rejected revision.",
                visual_checks_json=[], created_at=_now(),
            ))
            session.commit()
        try:
            output, model_meta = self._generate(payload, revision_number=number, prior=prior_snapshot, review_notes=notes, generation_context=context)
            self._validate_output(output, allowed=set(payload.evidence_ids), image_ids=payload.image_material_ids)
        except Exception:
            with self.database.session() as session:
                session.execute(update(ContentItemRecord).where(
                    ContentItemRecord.id == item_id,
                    ContentItemRecord.status == "draft",
                    ContentItemRecord.current_revision_id == payload_request.expected_revision_id,
                ).values(status="rejected", updated_at=_now()))
                session.commit()
            raise
        try:
            with self.database.session() as session:
                record = _load_item(session, item_id)
                self._validate_item_trust(record, session=session)
                if record.status != "draft" or record.current_revision_id != prior.id:
                    raise ContentStateError("Content item changed during regeneration.")
                revision = self._revision(record.id, number, output, model_meta)
                session.add(revision)
                session.flush()
                record.current_revision_id = revision.id
                record.status = "review"
                record.updated_at = _now()
                session.commit()
                session.expire_all()
                return _item_read(_load_item(session, item_id))
        except Exception:
            self._restore_rejected_reservation(item_id, payload_request.expected_revision_id)
            raise

    def _restore_rejected_reservation(self, item_id: str, revision_id: str) -> None:
        with self.database.session() as session:
            session.execute(update(ContentItemRecord).where(
                ContentItemRecord.id == item_id,
                ContentItemRecord.status == "draft",
                ContentItemRecord.current_revision_id == revision_id,
            ).values(status="rejected", updated_at=_now()))
            session.commit()

    def export_package(self, item_id: str, payload: ExportCreate) -> ContentPackageRead:
        with self.database.session() as session:
            item = _load_item(session, item_id)
            self._validate_item_trust(item, session=session)
            if item.current_revision_id != payload.expected_revision_id:
                raise ContentStateError("The expected revision is stale.")
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
            old_path_to_remove: str | None = None
            if existing is not None:
                self._assert_path_not_quarantined(session, existing.path)
                projected = self._package_read(existing)
                if existing.status == "ready" and projected.availability == "available":
                    return projected
                if existing.status == "building":
                    raise ContentStateError("This revision package is already building.")
                old_path = existing.path
                previous_status = existing.status
                replacement_path = f"content-packages/{item.id}/{existing.id}-{uuid4().hex}.zip"
                self._assert_path_not_quarantined(session, replacement_path)
                won = session.execute(update(ContentPackageRecord).where(
                    ContentPackageRecord.id == existing.id,
                    ContentPackageRecord.status == previous_status,
                    ContentPackageRecord.path == old_path,
                ).values(
                    status="building", error_detail=None, sha256="0" * 64,
                    size_bytes=0, path=replacement_path,
                )).rowcount
                if won != 1:
                    session.rollback()
                    raise ContentStateError("This revision package was concurrently reserved.")
                session.commit()
                package_id = existing.id
                package_path = replacement_path
                old_path_to_remove = old_path
            else:
                package_id_value = str(uuid4())
                package_path_value = (
                    f"content-packages/{item.id}/{package_id_value}.zip"
                )
                self._assert_path_not_quarantined(session, package_path_value)
                package = ContentPackageRecord(
                    id=package_id_value,
                    content_item_id=item.id, revision_id=item.current_revision_id,
                    status="building", path=package_path_value,
                    sha256="0" * 64, size_bytes=0, created_at=_now(), error_detail=None,
                )
                session.add(package)
                try:
                    session.commit()
                except IntegrityError as error:
                    session.rollback()
                    raise ContentStateError("This revision package was concurrently reserved.") from error
                package_id = package.id
                package_path = package.path
        try:
            if old_path_to_remove is not None:
                self._cleanup_unreferenced_artifact(old_path_to_remove)
            with self.database.session() as session:
                item = _load_item(session, item_id)
                self._validate_item_trust(item, session=session)
                package = session.get(ContentPackageRecord, package_id)
                self._assert_path_not_quarantined(session, package_path)
                approval = session.scalar(select(ContentReviewRecord).where(
                    ContentReviewRecord.content_item_id == item.id,
                    ContentReviewRecord.revision_id == item.current_revision_id,
                    ContentReviewRecord.decision == "approve",
                ))
                if (
                    package is None or package.status != "building"
                    or package.path != package_path
                    or item.current_revision_id != payload.expected_revision_id
                    or item.status not in {"approved", "exported"}
                    or approval is None
                ):
                    raise ContentStateError("Package or content state changed after reservation.")
                revision = next(
                    value for value in item.revisions if value.id == item.current_revision_id
                )
                product = _load_product(session, item.product_id)
                materials = []
                for material_id in list(item.material_ids_json) + list(item.image_material_ids_json):
                    material = session.get(ProductMaterialRecord, material_id)
                    if material is None or material.product_id != item.product_id:
                        raise ContentValidationError(
                            "Approved material is missing or foreign to the product."
                        )
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
                        "entry_path": (
                            f"images/{item.image_material_ids_json.index(material.id) + 1:02d}{Path(material.logical_name).suffix.lower()}"
                            if material.kind == "output_image"
                            else f"materials/{material.id}/{material.logical_name}"
                        ),
                    }
                    for material in sorted(materials, key=lambda value: value.id)
                ],
                "images": {
                    "count": len(item.image_material_ids_json),
                    "cover_material_id": item.cover_material_id,
                    "ordered_material_ids": list(item.image_material_ids_json),
                },
                "automatic_publish": False,
            }
            archive = deterministic_zip(entries, manifest)
            write_contained_atomic(self.runtime_dir, package_path, archive)
            with self.database.session() as session:
                current_item = _load_item(session, item_id)
                self._validate_item_trust(current_item, session=session)
                package_won = session.execute(update(ContentPackageRecord).where(
                    ContentPackageRecord.id == package_id,
                    ContentPackageRecord.content_item_id == item_id,
                    ContentPackageRecord.revision_id == payload.expected_revision_id,
                    ContentPackageRecord.status == "building",
                    ContentPackageRecord.path == package_path,
                ).values(
                    status="ready", sha256=sha256(archive).hexdigest(),
                    size_bytes=len(archive), error_detail=None,
                )).rowcount
                item_won = session.execute(update(ContentItemRecord).where(
                    ContentItemRecord.id == item_id,
                    ContentItemRecord.current_revision_id == payload.expected_revision_id,
                    ContentItemRecord.status.in_(("approved", "exported")),
                ).values(status="exported", updated_at=_now())).rowcount
                if package_won != 1 or item_won != 1:
                    session.rollback()
                    raise ContentStateError("Package or content state changed before finalization.")
                session.commit()
                package = session.get(ContentPackageRecord, package_id)
                if package is None:
                    raise ContentStateError("Package disappeared after finalization.")
                return self._package_read(package)
        except UnsafeContentPath as error:
            failed_by_builder = self._fail_package_reservation(package_id, package_path)
            self._cleanup_unreferenced_artifact(
                package_path, ignored_package_id=package_id if failed_by_builder else None
            )
            raise ContentValidationError(
                "Package build failed; no ready artifact was recorded."
            ) from error
        except Exception:
            failed_by_builder = self._fail_package_reservation(package_id, package_path)
            self._cleanup_unreferenced_artifact(
                package_path, ignored_package_id=package_id if failed_by_builder else None
            )
            raise

    def _fail_package_reservation(self, package_id: str, package_path: str) -> bool:
        with self.database.session() as session:
            won = session.execute(update(ContentPackageRecord).where(
                ContentPackageRecord.id == package_id,
                ContentPackageRecord.status == "building",
                ContentPackageRecord.path == package_path,
            ).values(status="failed", error_detail="package_build_failed")).rowcount
            session.commit()
            return won == 1

    def _cleanup_unreferenced_artifact(
        self, relative_path: str, *, ignored_package_id: str | None = None
    ) -> None:
        """Remove an artifact only if no material or package owns an equivalent file."""
        try:
            candidate = windows_artifact_reference(self.runtime_dir, relative_path)
            if candidate is None:
                return
            with self.database.engine.connect() as connection:
                references = list(connection.exec_driver_sql(
                    "SELECT NULL AS id, path FROM content_product_materials"
                ).mappings().all())
                references.extend(connection.exec_driver_sql(
                    "SELECT id, path FROM content_packages"
                ).mappings().all())
            if any(
                reference["id"] != ignored_package_id
                and isinstance(reference["path"], str)
                and windows_artifact_references_conflict(
                    candidate,
                    windows_artifact_reference(self.runtime_dir, reference["path"]),
                )
                for reference in references
            ):
                return
        except (SQLAlchemyError, OSError, ValueError, TypeError):
            return
        remove_contained_regular(self.runtime_dir, relative_path)

    def _assert_path_not_quarantined(self, session, relative_path: str) -> None:
        conflict = session.scalar(
            text(
                "SELECT 1 FROM artifact_gc_queue "
                "WHERE quarantine_path IS NOT NULL "
                "AND state IN ('claimed','quarantined','deleted','needs_human') "
                "AND windows_artifact_path_key(quarantine_path) "
                "= windows_artifact_path_key(:path) LIMIT 1"
            ),
            {"path": relative_path},
        )
        if conflict is not None:
            raise ContentStateError(
                "Artifact path is reserved by quarantine cleanup."
            )

    def list_packages(self) -> list[ContentPackageRead]:
        with self.database.session() as session:
            return [self._package_read(record) for record in session.scalars(
                select(ContentPackageRecord).order_by(ContentPackageRecord.created_at, ContentPackageRecord.id)
            ).all()]

    def get_package(self, package_id: str) -> ContentPackageRead:
        with self.database.session() as session:
            record = session.get(ContentPackageRecord, package_id)
            if record is None:
                raise ContentNotFound(f"Content package {package_id} does not exist.")
            projected = self._package_read(record)
            if record.status != "ready" or projected.availability != "available":
                raise ContentStateError("Content package is not a verified ready artifact.")
            return projected

    def _package_read(self, record: ContentPackageRecord) -> ContentPackageRead:
        if record.status == "building":
            availability = "building"
        elif record.status == "failed":
            availability = "failed"
        else:
            try:
                payload = read_contained_regular(self.runtime_dir, record.path, limit=MAX_PACKAGE_BYTES)
            except UnsafeContentPath:
                candidate = self.runtime_dir.joinpath(*__import__("pathlib").PurePosixPath(record.path).parts)
                availability = "corrupt" if candidate.exists() else "missing"
            else:
                availability = (
                    "available"
                    if len(payload) == record.size_bytes and sha256(payload).hexdigest() == record.sha256
                    else "corrupt"
                )
        return ContentPackageRead(
            id=record.id, content_item_id=record.content_item_id,
            revision_id=record.revision_id, status=record.status,
            availability=availability, path=record.path, sha256=record.sha256,
            size_bytes=record.size_bytes, created_at=record.created_at,
        )

    def _item_projection(self, record: ContentItemRecord, *, session: Any) -> ContentItemRead:
        projected = _item_read(record)
        if record.status == "exported" and record.current_revision_id:
            package = session.scalar(select(ContentPackageRecord).where(
                ContentPackageRecord.revision_id == record.current_revision_id
            ))
            projected.export_availability = "missing" if package is None else self._package_read(package).availability
        return projected

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
            if material is None or material.product_id != payload.product_id or material.kind != "source":
                raise ContentValidationError("Every material must belong to this product.")
            self._require_material_available(material)
        for material_id in payload.image_material_ids:
            material = session.get(ProductMaterialRecord, material_id)
            if material is None or material.product_id != payload.product_id or material.kind != "output_image":
                raise ContentValidationError("Every output image must belong to this product and be typed as output_image.")
            self._require_material_available(material)

    def _require_material_available(self, material: ProductMaterialRecord) -> None:
        status = self._material_availability(material)
        if status != "available":
            raise ContentValidationError(f"Material {material.id} is {status}.")

    def _material_availability(self, record: ProductMaterialRecord) -> str:
        try:
            payload = read_contained_regular(self.runtime_dir, record.path)
        except UnsafeContentPath:
            candidate = self.runtime_dir.joinpath(*__import__("pathlib").PurePosixPath(record.path).parts)
            return "corrupt" if candidate.exists() else "missing"
        if len(payload) != record.size_bytes or sha256(payload).hexdigest() != record.sha256:
            return "corrupt"
        try:
            _validate_material_bytes(payload, declared=record.media_type, kind=record.kind)
        except ContentValidationError:
            return "corrupt"
        return "available"

    def _material_read(self, record: ProductMaterialRecord) -> MaterialRead:
        return MaterialRead(
            id=record.id, product_id=record.product_id, logical_name=record.logical_name,
            version=record.version, path=record.path, sha256=record.sha256,
            size_bytes=record.size_bytes, media_type=record.media_type, kind=record.kind,
            availability=self._material_availability(record), created_at=record.created_at,
        )

    def _product_read(self, record: ProductRecord) -> ProductRead:
        return ProductRead(
            id=record.id, name=record.name, target_user=record.target_user,
            opportunity_id=record.opportunity_id,
            materials=[self._material_read(item) for item in record.materials], created_at=record.created_at,
        )

    def _generate(self, payload: ContentItemCreate, *, revision_number: int, prior: dict[str, str] | None, review_notes: list[str], generation_context: dict[str, Any]) -> tuple[ContentDraftOutput, dict[str, Any]]:
        if getattr(self.model_adapter, "configured", False) is not True:
            raise ContentModelUnavailable("Model provider is not configured.")
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
            **generation_context,
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
            raise ContentModelFailure("Content model generation failed; no revision was created.") from error
        except ValidationError as error:
            raise ContentValidationError("Content model returned invalid structured output.") from error
        return output, {
            "provider": _safe_identifier(getattr(self.model_adapter, "provider", None), limit=100),
            "model": _safe_identifier(result.model, limit=300), "usage": _safe_usage(result.usage),
            "attempts": _safe_attempts(result.raw_evidence),
        }

    @staticmethod
    def _validate_output(output: ContentDraftOutput, *, allowed: set[str], image_ids: list[str]) -> None:
        if not set(output.source_evidence_ids).issubset(allowed):
            raise ContentValidationError("Draft source list escaped the request evidence scope.")
        declared_sources = set(output.source_evidence_ids)
        for claim in output.claims:
            if not set(claim.evidence_ids).issubset(allowed):
                raise ContentValidationError("A generated claim is ungrounded or cross-request.")
            if not set(claim.evidence_ids).issubset(declared_sources):
                raise ContentValidationError("A generated claim citation is absent from the source list.")
        plan_ids = [item.material_id for item in output.image_plan]
        if plan_ids != image_ids or [item.page_number for item in output.image_plan] != list(range(1, len(image_ids) + 1)):
            raise ContentValidationError("Image plan must cover ordered output images exactly once.")
        if output.image_plan[0].role != "cover" or any(item.role != "page" for item in output.image_plan[1:]):
            raise ContentValidationError("Image plan cover/page roles are invalid.")

    @staticmethod
    def _revision(item_id: str, number: int, output: ContentDraftOutput, model_meta: dict[str, Any]) -> ContentRevisionRecord:
        return ContentRevisionRecord(
            content_item_id=item_id, number=number, title=output.title, body=output.body,
            claims_json=[item.model_dump(mode="json") for item in output.claims],
            source_evidence_ids_json=list(output.source_evidence_ids),
            image_plan_json=[item.model_dump(mode="json") for item in output.image_plan],
            model_provider=model_meta["provider"], model_name=model_meta["model"],
            prompt_version=PROMPT_VERSION, usage_json=model_meta["usage"],
            attempts_json=model_meta["attempts"], created_at=_now(),
        )

    def _generation_context(self, product: ProductRecord, payload: ContentItemCreate, session: Any) -> dict[str, Any]:
        material_ids = list(payload.material_ids) + list(payload.image_material_ids)
        materials = [session.get(ProductMaterialRecord, item) for item in material_ids]
        return {
            "product": {"name": product.name, "target_user": product.target_user},
            "materials": [
                {
                    "id": item.id, "logical_name": item.logical_name, "kind": item.kind,
                    "media_type": item.media_type, "sha256": item.sha256,
                }
                for item in materials if item is not None
            ],
            "ordered_image_material_ids": list(payload.image_material_ids),
            "cover_material_id": payload.cover_material_id,
        }

    def _validate_opportunity(self, opportunity: OpportunityRecord | None, *, session: Any) -> None:
        if opportunity is None:
            raise ContentValidationError("Product requires a persisted successful opportunity.")
        analysis = opportunity.analysis
        if analysis.status != "succeeded" or analysis.output_json is None:
            raise ContentValidationError("Product opportunity analysis is no longer successful.")
        try:
            from backend.app.features.analysis.schemas import AnalysisOutput
            validated_output = AnalysisOutput.model_validate(analysis.output_json)
            canonical = __import__("backend.app.features.content.schemas", fromlist=["canonical_evidence_ids"]).canonical_evidence_ids
            canonical(list(opportunity.evidence_ids_json))
        except (ValidationError, ValueError) as error:
            raise ContentValidationError("Product opportunity output or citations are invalid.") from error
        if not set(opportunity.evidence_ids_json).issubset(set(analysis.evidence_ids_json)):
            raise ContentValidationError("Product opportunity citations escaped its analysis scope.")
        if not any(
            card.title == opportunity.title
            and card.status == opportunity.status
            and card.summary == opportunity.summary
            and card.evidence_ids == list(opportunity.evidence_ids_json)
            and card.next_action == opportunity.next_action
            for card in validated_output.opportunities
        ):
            raise ContentValidationError("Opportunity row is not bound to its validated analysis output.")
        for evidence_id in opportunity.evidence_ids_json:
            prefix, _, raw_id = evidence_id.partition(":")
            record = session.get(JobArtifactRecord if prefix == "artifact" else RankItemRecord, int(raw_id))
            if record is None:
                raise ContentValidationError("Product opportunity evidence is missing.")
            if prefix == "artifact":
                from backend.app.features.analysis.service import AnalysisService
                checker = AnalysisService(self.database, self.model_adapter, runtime_dir=self.runtime_dir)
                if checker._trusted_shop_result(record) is None:
                    raise ContentValidationError("Product opportunity artifact is no longer trusted.")

    def _validate_item_trust(self, item: ContentItemRecord, *, session: Any) -> None:
        opportunity = session.get(OpportunityRecord, item.opportunity_id)
        self._validate_opportunity(opportunity, session=session)
        payload = ContentItemCreate(
            product_id=item.product_id, opportunity_id=item.opportunity_id,
            template_key=item.template_key, evidence_ids=list(item.evidence_ids_json),
            material_ids=list(item.material_ids_json),
            image_material_ids=list(item.image_material_ids_json), cover_material_id=item.cover_material_id,
            research_facts=list(item.research_facts_json),
        )
        self._validate_request_scope(payload, allowed=set(opportunity.evidence_ids_json), session=session)

    @staticmethod
    def _validate_visual_checks(item: ContentItemRecord, payload: ReviewCreate) -> None:
        ids = [check.material_id for check in payload.visual_checks]
        if len(ids) != len(set(ids)) or set(ids) - set(item.image_material_ids_json):
            raise ContentValidationError("Visual checks contain duplicates or foreign images.")
        if payload.decision == "approve":
            if ids != list(item.image_material_ids_json) or any(not check.passed for check in payload.visual_checks):
                raise ContentValidationError("Approval requires one passing visual check per ordered image.")

    def _package_entries(self, item: ContentItemRecord, revision: ContentRevisionRecord, product: ProductRecord, materials: list[ProductMaterialRecord]) -> dict[str, bytes]:
        entries: dict[str, bytes] = {
            "content/final.md": f"# {revision.title}\n\n{revision.body}\n".encode("utf-8"),
            "content/image-plan.json": _json_bytes(list(revision.image_plan_json)),
            "sources/evidence.json": _json_bytes({
                "opportunity_id": item.opportunity_id,
                "evidence_ids": list(revision.source_evidence_ids_json),
                "claims": list(revision.claims_json),
                "research_facts": list(item.research_facts_json),
            }),
            "reviews/history.json": _json_bytes([
                {"id": review.id, "revision_id": review.revision_id, "decision": review.decision,
                 "actor": review.actor, "note": review.note, "created_at": review.created_at.isoformat()}
                 | {"visual_checks": list(review.visual_checks_json)}
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
            if material.kind == "output_image":
                position = item.image_material_ids_json.index(material.id) + 1
                name = f"images/{position:02d}{Path(material.logical_name).suffix.lower()}"
            else:
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


def _revision_read(record: ContentRevisionRecord) -> RevisionRead:
    return RevisionRead(
        id=record.id, number=record.number, title=record.title, body=record.body,
        claims=list(record.claims_json), source_evidence_ids=list(record.source_evidence_ids_json),
        image_plan=list(record.image_plan_json),
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
        image_material_ids=list(record.image_material_ids_json),
        cover_material_id=record.cover_material_id,
        research_facts=list(record.research_facts_json), current_revision=current,
        revisions=revisions,
        reviews=[ReviewRead(
            id=item.id, revision_id=item.revision_id, decision=item.decision,
            actor=item.actor, note=item.note, visual_checks=list(item.visual_checks_json),
            created_at=item.created_at,
        ) for item in record.reviews],
        created_at=record.created_at, updated_at=record.updated_at,
    )


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


def _validate_material_bytes(payload: bytes, *, declared: str, kind: str) -> str:
    if not payload:
        raise ContentValidationError("Material files must not be empty.")
    supported_images = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}
    supported_text = {"text/plain", "text/markdown", "application/json"}
    detected: str | None
    if declared in supported_images:
        from io import BytesIO
        import warnings
        from PIL import Image, UnidentifiedImageError
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(payload)) as image:
                    actual_format = image.format
                    if image.width < 1 or image.height < 1 or image.width * image.height > 100_000_000:
                        raise ValueError("unsafe image dimensions")
                    image.verify()
                with Image.open(BytesIO(payload)) as image:
                    if image.width < 1 or image.height < 1 or image.width * image.height > 100_000_000:
                        raise ValueError("unsafe image dimensions")
                    image.load()
                if image.width < 1 or image.height < 1 or image.width * image.height > 100_000_000:
                    raise ValueError("unsafe image dimensions")
        except (
            UnidentifiedImageError, OSError, ValueError, SyntaxError,
            Image.DecompressionBombWarning, Image.DecompressionBombError,
        ) as error:
            raise ContentValidationError("Image material cannot be fully decoded.") from error
        detected = next((media for media, image_format in supported_images.items() if image_format == actual_format), None)
    elif declared in supported_text:
        try:
            payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ContentValidationError("Text material is not valid UTF-8.") from error
        detected = declared
    else:
        detected = None
    if detected is None or detected != declared:
        raise ContentValidationError("Declared media type does not match supported file bytes.")
    if kind == "output_image" and not detected.startswith("image/"):
        raise ContentValidationError("output_image materials must contain a supported image.")
    if detected == "application/json":
        try:
            json.loads(payload.decode("utf-8"))
        except (json.JSONDecodeError, RecursionError) as error:
            raise ContentValidationError("JSON material is invalid.") from error
    return detected


def _valid_png(payload: bytes) -> bool:
    import zlib
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    offset, kinds = 8, []
    try:
        while offset + 12 <= len(payload):
            length = int.from_bytes(payload[offset:offset + 4], "big")
            kind = payload[offset + 4:offset + 8]
            end = offset + 12 + length
            if end > len(payload):
                return False
            data = payload[offset + 8:offset + 8 + length]
            expected = int.from_bytes(payload[offset + 8 + length:end], "big")
            if zlib.crc32(kind + data) & 0xFFFFFFFF != expected:
                return False
            kinds.append(kind)
            offset = end
            if kind == b"IEND":
                break
    except (OverflowError, ValueError):
        return False
    return offset == len(payload) and kinds[:1] == [b"IHDR"] and b"IDAT" in kinds and kinds[-1:] == [b"IEND"]


def _valid_jpeg(payload: bytes) -> bool:
    if len(payload) < 20 or not payload.startswith(b"\xff\xd8") or not payload.endswith(b"\xff\xd9"):
        return False
    return any(marker in payload for marker in (b"\xff\xc0", b"\xff\xc1", b"\xff\xc2")) and b"\xff\xda" in payload


def _valid_webp(payload: bytes) -> bool:
    return (
        len(payload) >= 20 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP"
        and int.from_bytes(payload[4:8], "little") + 8 == len(payload)
        and payload[12:16] in {b"VP8 ", b"VP8L", b"VP8X"}
    )


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
