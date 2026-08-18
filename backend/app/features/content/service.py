"""Transactional, evidence-grounded content production service."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import Enum
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
from backend.app.features.content.cleanup import (
    ArtifactCleanupCandidate,
    ArtifactCleanupService,
)
from backend.app.features.analysis.models import OpportunityRecord
from backend.app.features.radar.models import RankItemRecord
from backend.app.models.jobs import JobArtifactRecord
from backend.app.features.content.export import (
    MAX_MATERIAL_BYTES, MAX_PACKAGE_BYTES, MAX_UNCOMPRESSED_PACKAGE_BYTES,
    MAX_ZIP_ENTRIES, UnsafeContentPath, deterministic_zip, entry_manifest,
    read_contained_regular, write_contained_atomic,
)
from backend.app.features.content.models import (
    ArtifactCleanupRecord, ContentItemRecord, ContentPackageRecord, ContentReviewRecord, ContentRevisionRecord,
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


class _ReservationOutcome(str, Enum):
    LANDED = "landed"
    NOT_LANDED = "not_landed"
    UNKNOWN = "unknown"


class _MaterialPersistenceOutcome(str, Enum):
    LANDED = "landed"
    NOT_LANDED = "not_landed"
    UNKNOWN = "unknown"


class _FailureOutcome(str, Enum):
    FAILED = "failed"
    PROVEN_FAILED = "proven_failed"
    UNKNOWN = "unknown"
    LOST = "lost"


class ContentService:
    def __init__(
        self,
        database: Database,
        model_adapter: ModelAdapter,
        *,
        runtime_dir: Path,
        cleanup_service: ArtifactCleanupService | None = None,
    ) -> None:
        self.database = database
        self.model_adapter = model_adapter
        self.runtime_dir = runtime_dir
        self.cleanup_service = cleanup_service or ArtifactCleanupService(
            database, runtime_dir=runtime_dir
        )

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
            candidate = ArtifactCleanupCandidate(
                owner_type="material",
                owner_id=record.id,
                relative_path=record.path,
                expected_sha256=record.sha256,
                expected_size_bytes=record.size_bytes,
                reason="material_write_reserved",
                not_before=_now() + timedelta(hours=24),
            )
            cleanup = self.cleanup_service.enqueue_in_session(session, candidate)
            session.commit()
        try:
            write_contained_atomic(self.runtime_dir, record.path, data)
        except UnsafeContentPath as error:
            self._make_material_cleanup_due(cleanup.id, candidate)
            raise ContentValidationError(str(error)) from error
        except Exception:
            self._make_material_cleanup_due(cleanup.id, candidate)
            raise
        try:
            with self.database.session() as session:
                session.add(record)
                if not self.cleanup_service.cancel_in_session(
                    session, cleanup.id, candidate=candidate
                ):
                    raise ContentStateError(
                        "Material cleanup reservation changed before persistence."
                    )
                session.commit()
                return self._material_read(
                    session.get(ProductMaterialRecord, record.id)
                )
        except SQLAlchemyError as error:
            return self._resolve_material_persistence_error(
                error, record, cleanup.id, candidate
            )

    def _resolve_material_persistence_error(
        self,
        error: SQLAlchemyError,
        expected: ProductMaterialRecord,
        cleanup_id: str,
        candidate: ArtifactCleanupCandidate,
    ) -> MaterialRead:
        outcome, proven = self._material_persistence_after_unknown(
            expected, cleanup_id, candidate
        )
        if outcome is _MaterialPersistenceOutcome.LANDED:
            assert proven is not None
            return proven
        reason = (
            "material_persistence_failed"
            if outcome is _MaterialPersistenceOutcome.NOT_LANDED
            else "material_transaction_unknown"
        )
        try:
            self._make_material_cleanup_due(
                cleanup_id, candidate, reason=reason
            )
        except ContentStateError as due_error:
            raise ContentStateError(
                "material_failure_transaction_unknown: persistence and cleanup due state could not be proven."
            ) from due_error
        if outcome is _MaterialPersistenceOutcome.NOT_LANDED:
            raise ContentStateError(
                "material_persistence_not_landed: material row did not commit; cleanup is due."
            ) from error
        raise ContentStateError(
            "transaction_unknown: material persistence is unknown; cleanup is due."
        ) from error

    def _material_persistence_after_unknown(
        self,
        expected: ProductMaterialRecord,
        cleanup_id: str,
        candidate: ArtifactCleanupCandidate,
    ) -> tuple[_MaterialPersistenceOutcome, MaterialRead | None]:
        """Classify an uncertain commit from fresh exact material and outbox facts."""
        try:
            with self.database.session() as session:
                record = session.get(ProductMaterialRecord, expected.id)
                cleanup = session.get(ArtifactCleanupRecord, cleanup_id)
                record_matches = bool(
                    record is not None
                    and record.product_id == expected.product_id
                    and record.logical_name == expected.logical_name
                    and record.logical_key == expected.logical_key
                    and record.version == expected.version
                    and record.path == candidate.relative_path
                    and record.sha256 == candidate.expected_sha256
                    and record.size_bytes == candidate.expected_size_bytes
                    and record.media_type == expected.media_type
                    and record.kind == expected.kind
                )
                if record_matches and self._cleanup_matches(
                    cleanup, candidate, state="cancelled"
                ):
                    return (
                        _MaterialPersistenceOutcome.LANDED,
                        self._material_read(record),
                    )
                if record is None and self._cleanup_matches(
                    cleanup, candidate, state="pending"
                ):
                    return _MaterialPersistenceOutcome.NOT_LANDED, None
                return _MaterialPersistenceOutcome.UNKNOWN, None
        except SQLAlchemyError:
            return _MaterialPersistenceOutcome.UNKNOWN, None

    def _make_material_cleanup_due(
        self,
        cleanup_id: str,
        candidate: ArtifactCleanupCandidate,
        *,
        reason: str | None = None,
    ) -> None:
        due_at = _now()
        try:
            with self.database.session() as session:
                if not self.cleanup_service.make_due_in_session(
                    session, cleanup_id, candidate=candidate, due_at=due_at,
                    reason=reason,
                ):
                    raise ContentStateError(
                        "material_failure_transaction_unknown: cleanup reservation changed."
                    )
                session.commit()
                return
        except ContentStateError:
            raise
        except SQLAlchemyError as error:
            try:
                with self.database.session() as session:
                    cleanup = session.get(ArtifactCleanupRecord, cleanup_id)
                    proven = self._cleanup_matches(
                        cleanup, candidate, state="pending"
                    ) and cleanup.not_before <= due_at and (
                        reason is None or cleanup.reason == reason
                    )
            except SQLAlchemyError:
                proven = False
            if not proven:
                raise ContentStateError(
                    "material_failure_transaction_unknown: cleanup due state could not be proven."
                ) from error

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
            product = session.get(ProductRecord, payload.product_id)
            self._validate_product_opportunity_binding(
                product, product_id=payload.product_id,
                opportunity_id=payload.opportunity_id,
            )
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
            self._require_model_configured()
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
        # Build the deterministic bytes before reserving their filesystem path so
        # the durable cleanup outbox can bind the exact hash and size.
        with self.database.session() as session:
            item = _load_item(session, item_id)
            self._validate_item_trust(
                item, session=session, validate_material_bytes=False,
            )
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
            if existing is not None:
                self._assert_path_not_quarantined(session, existing.path)
                projected = self._package_read(existing)
                if existing.status == "ready" and projected.availability == "available":
                    return projected
                if existing.status == "building":
                    raise ContentStateError("This revision package is already building.")
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
            fixed_entries, material_plan, manifest = self._package_preflight(
                item, revision, product, materials,
            )
        entries = self._read_package_entries(
            fixed_entries, material_plan, manifest=manifest,
        )
        archive = deterministic_zip(entries, manifest)
        archive_sha = sha256(archive).hexdigest()
        archive_size = len(archive)
        build_token = str(uuid4())

        with self.database.session() as session:
            current_item = _load_item(session, item_id)
            self._validate_item_trust(current_item, session=session)
            if (
                current_item.current_revision_id != payload.expected_revision_id
                or current_item.status not in {"approved", "exported"}
            ):
                raise ContentStateError("Package or content state changed before reservation.")
            existing = session.scalar(select(ContentPackageRecord).where(
                ContentPackageRecord.revision_id == payload.expected_revision_id
            ))
            if existing is not None and existing.status == "ready" and self._package_read(existing).availability == "available":
                return self._package_read(existing)
            if existing is not None and existing.status == "building":
                raise ContentStateError("This revision package is already building.")
            replaced_candidate: ArtifactCleanupCandidate | None = None
            replaced_cleanup = None
            prior_package: tuple[str, str, str, int, str | None] | None = None
            if existing is not None:
                package_id = existing.id
                old_path = existing.path
                previous_status = existing.status
                prior_package = (
                    previous_status, old_path, existing.sha256,
                    existing.size_bytes, existing.build_token,
                )
                package_path = f"content-packages/{item_id}/{package_id}-{uuid4().hex}.zip"
                self._assert_path_not_quarantined(session, package_path)
                replaced_candidate = ArtifactCleanupCandidate(
                    owner_type="content_package", owner_id=package_id,
                    relative_path=old_path, expected_sha256=existing.sha256,
                    expected_size_bytes=existing.size_bytes, reason="package_replaced",
                    not_before=_now(), source_build_token=existing.build_token,
                )
                try:
                    replaced_cleanup = self.cleanup_service.enqueue_in_session(
                        session, replaced_candidate
                    )
                except IntegrityError as error:
                    session.rollback()
                    raise ContentStateError(
                        "This revision package was concurrently reserved."
                    ) from error
                won = session.execute(update(ContentPackageRecord).where(
                    ContentPackageRecord.id == package_id,
                    ContentPackageRecord.content_item_id == item_id,
                    ContentPackageRecord.revision_id == payload.expected_revision_id,
                    ContentPackageRecord.status == previous_status,
                    ContentPackageRecord.path == old_path,
                ).values(
                    status="building", path=package_path, sha256=archive_sha,
                    size_bytes=archive_size, error_detail=None, build_token=build_token,
                )).rowcount
                if won != 1:
                    raise ContentStateError("This revision package was concurrently reserved.")
            else:
                package_id = str(uuid4())
                package_path = f"content-packages/{item_id}/{package_id}.zip"
                self._assert_path_not_quarantined(session, package_path)
                session.add(ContentPackageRecord(
                    id=package_id, content_item_id=item_id,
                    revision_id=payload.expected_revision_id, status="building",
                    path=package_path, sha256=archive_sha, size_bytes=archive_size,
                    created_at=_now(), error_detail=None, build_token=build_token,
                ))
                session.flush()
            build_candidate = ArtifactCleanupCandidate(
                owner_type="content_package", owner_id=package_id,
                relative_path=package_path, expected_sha256=archive_sha,
                expected_size_bytes=archive_size, reason="package_build_reserved",
                not_before=_now() + timedelta(hours=24),
                source_build_token=build_token,
            )
            build_cleanup = self.cleanup_service.enqueue_in_session(
                session, build_candidate
            )
            try:
                session.commit()
            except SQLAlchemyError as error:
                session.rollback()
                outcome = self._package_reservation_after_unknown(
                    package_id, item_id, payload.expected_revision_id,
                    package_path, build_token, archive_sha, archive_size,
                    build_cleanup.id, build_candidate, prior_package,
                    replaced_cleanup.id if replaced_cleanup is not None else None,
                    replaced_candidate,
                )
                if outcome is _ReservationOutcome.NOT_LANDED:
                    raise ContentStateError(
                        "package_reservation_failed: reservation transaction did not commit."
                    ) from error
                if outcome is _ReservationOutcome.UNKNOWN:
                    raise ContentStateError(
                        "package_reservation_transaction_unknown: reservation facts could not be proven."
                    ) from error
        reserved_item_id = item_id
        reserved_revision_id = payload.expected_revision_id

        try:
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
                    ContentPackageRecord.build_token == build_token,
                ).values(
                    status="ready", sha256=archive_sha,
                    size_bytes=archive_size, error_detail=None,
                )).rowcount
                item_won = session.execute(update(ContentItemRecord).where(
                    ContentItemRecord.id == item_id,
                    ContentItemRecord.current_revision_id == payload.expected_revision_id,
                    ContentItemRecord.status.in_(("approved", "exported")),
                ).values(status="exported", updated_at=_now())).rowcount
                if package_won != 1 or item_won != 1:
                    session.rollback()
                    raise ContentStateError("Package or content state changed before finalization.")
                if not self.cleanup_service.cancel_in_session(
                    session, build_cleanup.id, candidate=build_candidate
                ):
                    session.rollback()
                    raise ContentStateError(
                        "Package cleanup reservation changed before finalization."
                    )
                try:
                    session.commit()
                except SQLAlchemyError as error:
                    proven = self._package_after_unknown(
                        package_id, item_id, payload.expected_revision_id,
                        package_path, build_token, archive_sha, archive_size,
                        build_cleanup.id, build_candidate,
                    )
                    if proven is not None:
                        return proven
                    raise ContentStateError(
                        "transaction_unknown: package finalization could not be proven."
                    ) from error
                package = session.get(ContentPackageRecord, package_id)
                if package is None:
                    raise ContentStateError("Package disappeared after finalization.")
                return self._package_read(package)
        except UnsafeContentPath as error:
            failure = self._fail_package_reservation(
                package_id, reserved_item_id, reserved_revision_id, package_path,
                build_token, build_cleanup.id, build_candidate,
            )
            if failure is _FailureOutcome.UNKNOWN:
                raise ContentStateError(
                    "package_failure_transaction_unknown: failed package facts could not be proven."
                ) from error
            if failure is _FailureOutcome.LOST:
                raise ContentStateError(
                    "package_builder_lost: package reservation state changed."
                ) from error
            raise ContentValidationError(
                "Package build failed; no ready artifact was recorded."
            ) from error
        except Exception as error:
            failure = self._fail_package_reservation(
                package_id, reserved_item_id, reserved_revision_id, package_path,
                build_token, build_cleanup.id, build_candidate,
            )
            if failure is _FailureOutcome.UNKNOWN:
                raise ContentStateError(
                    "package_failure_transaction_unknown: failed package facts could not be proven."
                ) from error
            if failure is _FailureOutcome.LOST:
                raise ContentStateError(
                    "package_builder_lost: package reservation state changed."
                ) from error
            raise

    def _package_reservation_after_unknown(
        self,
        package_id: str,
        item_id: str,
        revision_id: str,
        package_path: str,
        build_token: str,
        expected_sha256: str,
        expected_size_bytes: int,
        cleanup_id: str,
        candidate: ArtifactCleanupCandidate,
        prior_package: tuple[str, str, str, int, str | None] | None,
        replaced_cleanup_id: str | None,
        replaced_candidate: ArtifactCleanupCandidate | None,
    ) -> _ReservationOutcome:
        try:
            with self.database.session() as session:
                package = session.get(ContentPackageRecord, package_id)
                cleanup = session.get(ArtifactCleanupRecord, cleanup_id)
                replaced = (
                    session.get(ArtifactCleanupRecord, replaced_cleanup_id)
                    if replaced_cleanup_id is not None else None
                )
                landed = bool(
                    package is not None and package.content_item_id == item_id
                    and package.revision_id == revision_id
                    and package.status == "building" and package.path == package_path
                    and package.build_token == build_token
                    and package.sha256 == expected_sha256
                    and package.size_bytes == expected_size_bytes
                    and self._cleanup_matches(cleanup, candidate, state="pending")
                    and cleanup.not_before <= _naive_datetime(candidate.not_before)
                )
                if landed and replaced_candidate is not None:
                    landed = bool(
                        self._cleanup_matches(
                            replaced, replaced_candidate, state="pending"
                        )
                        and replaced.not_before
                        <= _naive_datetime(replaced_candidate.not_before)
                    )
                if landed:
                    return _ReservationOutcome.LANDED
                if prior_package is None:
                    if package is None and cleanup is None:
                        return _ReservationOutcome.NOT_LANDED
                elif package is not None and cleanup is None:
                    old_status, old_path, old_sha, old_size, old_token = prior_package
                    if (
                        package.content_item_id == item_id
                        and package.revision_id == revision_id
                        and package.status == old_status and package.path == old_path
                        and package.sha256 == old_sha and package.size_bytes == old_size
                        and package.build_token == old_token
                        and replaced_cleanup_id is not None
                        and replaced is None
                    ):
                        return _ReservationOutcome.NOT_LANDED
                return _ReservationOutcome.UNKNOWN
        except SQLAlchemyError:
            return _ReservationOutcome.UNKNOWN

    def _fail_package_reservation(
        self,
        package_id: str,
        item_id: str,
        revision_id: str,
        package_path: str,
        build_token: str,
        cleanup_id: str,
        candidate: ArtifactCleanupCandidate,
    ) -> _FailureOutcome:
        due_at = _now()
        try:
            with self.database.session() as session:
                won = session.execute(update(ContentPackageRecord).where(
                    ContentPackageRecord.id == package_id,
                    ContentPackageRecord.content_item_id == item_id,
                    ContentPackageRecord.revision_id == revision_id,
                    ContentPackageRecord.status == "building",
                    ContentPackageRecord.path == package_path,
                    ContentPackageRecord.build_token == build_token,
                ).values(
                    status="failed",
                    error_detail="package_build_failed",
                    sha256=candidate.expected_sha256,
                    size_bytes=candidate.expected_size_bytes,
                )).rowcount
                if won != 1:
                    session.rollback()
                    return _FailureOutcome.LOST
                if not self.cleanup_service.make_due_in_session(
                    session, cleanup_id, candidate=candidate, due_at=due_at
                ):
                    session.rollback()
                    return _FailureOutcome.UNKNOWN
                session.commit()
                return _FailureOutcome.FAILED
        except SQLAlchemyError:
            return self._package_failed_after_unknown(
                package_id, item_id, revision_id, package_path, build_token,
                cleanup_id, candidate, due_at,
            )

    def _package_after_unknown(
        self,
        package_id: str,
        item_id: str,
        revision_id: str,
        package_path: str,
        build_token: str,
        expected_sha256: str,
        expected_size_bytes: int,
        cleanup_id: str,
        candidate: ArtifactCleanupCandidate,
    ) -> ContentPackageRead | None:
        try:
            with self.database.session() as session:
                package = session.get(ContentPackageRecord, package_id)
                item = session.get(ContentItemRecord, item_id)
                cleanup = session.get(ArtifactCleanupRecord, cleanup_id)
                if (
                    package is None or package.content_item_id != item_id
                    or package.revision_id != revision_id or package.status != "ready"
                    or package.path != package_path or package.build_token != build_token
                    or package.sha256 != expected_sha256
                    or package.size_bytes != expected_size_bytes
                    or item is None or item.status != "exported"
                    or item.current_revision_id != revision_id
                    or not self._cleanup_matches(
                        cleanup, candidate, state="cancelled"
                    )
                ):
                    return None
                return self._package_read(package)
        except SQLAlchemyError:
            return None

    def _package_failed_after_unknown(
        self,
        package_id: str,
        item_id: str,
        revision_id: str,
        package_path: str,
        build_token: str,
        cleanup_id: str,
        candidate: ArtifactCleanupCandidate,
        due_at: datetime,
    ) -> _FailureOutcome:
        try:
            with self.database.session() as session:
                package = session.get(ContentPackageRecord, package_id)
                cleanup = session.get(ArtifactCleanupRecord, cleanup_id)
                if (
                    package is not None and package.content_item_id == item_id
                    and package.revision_id == revision_id and package.status == "failed"
                    and package.path == package_path and package.build_token == build_token
                    and package.sha256 == candidate.expected_sha256
                    and package.size_bytes == candidate.expected_size_bytes
                    and self._cleanup_matches(cleanup, candidate, state="pending")
                    and cleanup.not_before <= due_at
                ):
                    return _FailureOutcome.PROVEN_FAILED
                if package is not None and (
                    package.content_item_id != item_id
                    or package.revision_id != revision_id
                    or package.path != package_path
                    or package.build_token != build_token
                    or package.status not in {"building", "failed"}
                ):
                    return _FailureOutcome.LOST
                return _FailureOutcome.UNKNOWN
        except SQLAlchemyError:
            return _FailureOutcome.UNKNOWN

    @staticmethod
    def _cleanup_matches(
        cleanup: ArtifactCleanupRecord | None,
        candidate: ArtifactCleanupCandidate,
        *,
        state: str,
    ) -> bool:
        return bool(
            cleanup is not None and cleanup.state == state
            and cleanup.owner_type == candidate.owner_type
            and cleanup.owner_id == candidate.owner_id
            and cleanup.source_build_token == candidate.source_build_token
            and cleanup.relative_path == candidate.relative_path
            and cleanup.expected_sha256 == candidate.expected_sha256
            and cleanup.expected_size_bytes == candidate.expected_size_bytes
        )

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

    def _validate_request_scope(
        self, payload: ContentItemCreate, *, allowed: set[str], session: Any,
        validate_material_bytes: bool = True,
    ) -> None:
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
            if validate_material_bytes:
                self._require_material_available(material)
        for material_id in payload.image_material_ids:
            material = session.get(ProductMaterialRecord, material_id)
            if material is None or material.product_id != payload.product_id or material.kind != "output_image":
                raise ContentValidationError("Every output image must belong to this product and be typed as output_image.")
            if validate_material_bytes:
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
        self._require_model_configured()
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

    def _require_model_configured(self) -> None:
        if getattr(self.model_adapter, "configured", False) is not True:
            raise ContentModelUnavailable("Model provider is not configured.")

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

    def _validate_item_trust(
        self, item: ContentItemRecord, *, session: Any,
        validate_material_bytes: bool = True,
    ) -> None:
        product = session.get(ProductRecord, item.product_id)
        self._validate_product_opportunity_binding(
            product, product_id=item.product_id,
            opportunity_id=item.opportunity_id,
        )
        opportunity = session.get(OpportunityRecord, item.opportunity_id)
        self._validate_opportunity(opportunity, session=session)
        payload = ContentItemCreate(
            product_id=item.product_id, opportunity_id=item.opportunity_id,
            template_key=item.template_key, evidence_ids=list(item.evidence_ids_json),
            material_ids=list(item.material_ids_json),
            image_material_ids=list(item.image_material_ids_json), cover_material_id=item.cover_material_id,
            research_facts=list(item.research_facts_json),
        )
        self._validate_request_scope(
            payload, allowed=set(opportunity.evidence_ids_json), session=session,
            validate_material_bytes=validate_material_bytes,
        )

    @staticmethod
    def _validate_product_opportunity_binding(
        product: ProductRecord | None, *, product_id: str, opportunity_id: str,
    ) -> None:
        if (
            product is None
            or product.id != product_id
            or product.opportunity_id != opportunity_id
        ):
            raise ContentValidationError(
                "Content product no longer belongs to its opportunity."
            )

    @staticmethod
    def _validate_visual_checks(item: ContentItemRecord, payload: ReviewCreate) -> None:
        ids = [check.material_id for check in payload.visual_checks]
        if len(ids) != len(set(ids)) or set(ids) - set(item.image_material_ids_json):
            raise ContentValidationError("Visual checks contain duplicates or foreign images.")
        if payload.decision == "approve":
            if ids != list(item.image_material_ids_json) or any(not check.passed for check in payload.visual_checks):
                raise ContentValidationError("Approval requires one passing visual check per ordered image.")

    def _package_preflight(
        self, item: ContentItemRecord, revision: ContentRevisionRecord,
        product: ProductRecord, materials: list[ProductMaterialRecord],
    ) -> tuple[
        dict[str, bytes], list[tuple[str, ProductMaterialRecord]], dict[str, object],
    ]:
        fixed_entries: dict[str, bytes] = {
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
        material_plan: list[tuple[str, ProductMaterialRecord]] = []
        planned_keys = {name.casefold() for name in fixed_entries}
        for material in sorted(materials, key=lambda value: value.id):
            if material.size_bytes > MAX_MATERIAL_BYTES:
                raise ContentValidationError(
                    "Approved material size exceeds its owner limit."
                )
            if material.kind == "output_image":
                position = item.image_material_ids_json.index(material.id) + 1
                name = f"images/{position:02d}{Path(material.logical_name).suffix.lower()}"
            else:
                name = f"materials/{material.id}/{material.logical_name}"
            if name.casefold() in planned_keys:
                raise ContentValidationError("Approved material export path collision.")
            planned_keys.add(name.casefold())
            material_plan.append((name, material))

        if len(fixed_entries) + len(material_plan) + 1 > MAX_ZIP_ENTRIES:
            raise ContentValidationError("Content package ZIP entry count limit exceeded.")

        declared_entries = entry_manifest(fixed_entries) + [
            {
                "path": name,
                "sha256": material.sha256,
                "size_bytes": material.size_bytes,
            }
            for name, material in material_plan
        ]
        declared_entries.sort(key=lambda value: str(value["path"]))
        manifest: dict[str, object] = {
            "schema_version": 1, "content_item_id": item.id,
            "revision_id": revision.id, "product_id": product.id,
            "opportunity_id": item.opportunity_id, "entries": declared_entries,
            "materials": [
                {
                    "id": material.id, "logical_name": material.logical_name,
                    "version": material.version, "media_type": material.media_type,
                    "sha256": material.sha256, "entry_path": name,
                }
                for name, material in material_plan
            ],
            "images": {
                "count": len(item.image_material_ids_json),
                "cover_material_id": item.cover_material_id,
                "ordered_material_ids": list(item.image_material_ids_json),
            },
            "automatic_publish": False,
        }
        declared_total = (
            sum(len(value) for value in fixed_entries.values())
            + sum(material.size_bytes for _, material in material_plan)
            + len(_json_bytes(manifest))
        )
        if declared_total > MAX_UNCOMPRESSED_PACKAGE_BYTES:
            raise ContentValidationError(
                "Content package declared uncompressed size limit exceeded."
            )
        return fixed_entries, material_plan, manifest

    def _read_package_entries(
        self, fixed_entries: dict[str, bytes],
        material_plan: list[tuple[str, ProductMaterialRecord]], *,
        manifest: dict[str, object],
    ) -> dict[str, bytes]:
        entries = dict(fixed_entries)
        actual_total = sum(len(value) for value in entries.values()) + len(
            _json_bytes(manifest)
        )
        for name, material in material_plan:
            remaining = MAX_UNCOMPRESSED_PACKAGE_BYTES - actual_total
            if remaining <= 0:
                raise ContentValidationError(
                    "Content package uncompressed size limit exceeded."
                )
            try:
                content = read_contained_regular(
                    self.runtime_dir, material.path,
                    limit=min(MAX_MATERIAL_BYTES, remaining),
                )
            except UnsafeContentPath as error:
                raise ContentValidationError(
                    "An approved material is unavailable or unsafe."
                ) from error
            actual_total += len(content)
            if actual_total > MAX_UNCOMPRESSED_PACKAGE_BYTES:
                raise ContentValidationError(
                    "Content package uncompressed size limit exceeded."
                )
            if (
                sha256(content).hexdigest() != material.sha256
                or len(content) != material.size_bytes
            ):
                raise ContentValidationError(
                    "An approved material changed after its immutable version was recorded."
                )
            entries[name] = content
        if entry_manifest(entries) != manifest["entries"]:
            raise ContentValidationError(
                "Content package entry manifest changed during bounded reads."
            )
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


def _naive_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)
