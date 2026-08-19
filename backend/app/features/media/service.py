"""Trusted content-image generation and advisory visual analysis boundary."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError

from backend.app.adapters.contracts import (
    GeneratedImage,
    ImageGenerationAdapter,
    ImageGenerationRequest,
    ModelAdapterError,
    VisionAdapter,
    VisionImage,
    VisionRequest,
    VisualAssessment,
)
from backend.app.db import Database
from backend.app.features.content.cleanup import (
    ArtifactCleanupCandidate,
    ArtifactCleanupService,
)
from backend.app.features.content.export import (
    MAX_MATERIAL_BYTES,
    UnsafeContentPath,
    inspect_contained_artifact,
    read_contained_regular,
    write_contained_atomic,
)
from backend.app.features.content.models import (
    ArtifactCleanupRecord,
    ContentItemRecord,
    ContentRevisionRecord,
    ProductMaterialRecord,
)
from backend.app.features.content.schemas import MaterialRead, windows_name_key
from backend.app.features.content.service import (
    ContentError,
    ContentService,
    ContentValidationError,
)
from backend.app.features.media.models import ContentMediaRunRecord
from backend.app.features.media.schemas import (
    ContentMediaRunCreate,
    ContentMediaRunRead,
    VisualAssessmentRead,
)
from backend.app.features.media.store import (
    ContentMediaRunStore,
    MediaRunConflict,
    MediaRunTransactionUnknown,
)
from backend.app.models.jobs import JobArtifactRecord, JobRecord, JobState
from backend.app.services.jobs import JobService


GENERATION_PROMPT_VERSION = "content-image-generation-v1"
ANALYSIS_PROMPT_VERSION = "content-image-analysis-v1"
_EXTENSION_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}
MEDIA_RESERVED_JOB_TYPES = {
    "content_image_generation",
    "content_image_analysis",
}
MEDIA_RESERVED_ARTIFACT_KINDS = {
    "generated_image_provenance",
    "visual_assessment",
}


class MediaError(RuntimeError):
    pass


class MediaNotFound(MediaError):
    pass


class MediaStateError(MediaError):
    pass


class MediaValidationError(MediaError):
    pass


class MediaExecutionCancelled(MediaError):
    pass


class ContentMediaService:
    """Persist provider results without granting them approval authority."""

    def __init__(
        self,
        database: Database,
        *,
        content_service: ContentService,
        image_adapter: ImageGenerationAdapter,
        vision_adapter: VisionAdapter,
        runtime_dir: Path,
        cleanup_service: ArtifactCleanupService | None = None,
    ) -> None:
        self.database = database
        self.content_service = content_service
        self.image_adapter = image_adapter
        self.vision_adapter = vision_adapter
        self.runtime_dir = runtime_dir.resolve(strict=True)
        self.cleanup_service = cleanup_service or content_service.cleanup_service
        self.run_store = ContentMediaRunStore(database)
        self.job_service = JobService(database, runtime_dir=self.runtime_dir)

    def get_run(self, run_id: str) -> ContentMediaRunRead:
        run = self.run_store.get(run_id)
        if run is None:
            raise MediaNotFound("Media run does not exist.")
        return run

    def list_runs(self, content_item_id: str) -> list[ContentMediaRunRead]:
        try:
            self.content_service.get_content_item(content_item_id)
        except ContentError as error:
            raise MediaNotFound("Content item does not exist.") from error
        return self.run_store.list_for_item(content_item_id)

    def submit_generation(
        self,
        item_id: str,
        *,
        expected_revision_id: str,
        image_plan_entry_id: str,
    ) -> ContentMediaRunRead:
        facts = self._current_generation_facts(
            item_id, expected_revision_id, image_plan_entry_id
        )
        return self._reserve_run(
            capability="generate",
            facts=facts,
            plan_entry_id=image_plan_entry_id,
            provider=self.image_adapter.provider,
            model=self.image_adapter.model,
            prompt_version=GENERATION_PROMPT_VERSION,
            allowed_material_ids=[],
        )

    def submit_analysis(
        self,
        item_id: str,
        *,
        expected_revision_id: str,
        material_ids: list[str],
    ) -> ContentMediaRunRead:
        facts = self._current_analysis_facts(
            item_id, expected_revision_id, material_ids
        )
        return self._reserve_run(
            capability="analyze",
            facts=facts,
            plan_entry_id=None,
            provider=self.vision_adapter.provider,
            model=self.vision_adapter.model,
            prompt_version=ANALYSIS_PROMPT_VERSION,
            allowed_material_ids=material_ids,
        )

    def _reserve_run(
        self,
        *,
        capability: str,
        facts: dict[str, Any],
        plan_entry_id: str | None,
        provider: str,
        model: str,
        prompt_version: str,
        allowed_material_ids: list[str],
    ) -> ContentMediaRunRead:
        run_id = str(uuid4())
        job_type = (
            "content_image_generation"
            if capability == "generate"
            else "content_image_analysis"
        )
        job = self.job_service.create(
            job_type=job_type,
            input_data={
                "run_id": run_id,
                "content_item_id": facts["item_id"],
                "revision_id": facts["revision_id"],
            },
            progress_total=1,
            current_stage="media_reserved",
        )
        request = ContentMediaRunCreate(
            id=run_id,
            job_id=job.id,
            owner_product_id=facts["product_id"],
            content_item_id=facts["item_id"],
            revision_id=facts["revision_id"],
            plan_entry_id=plan_entry_id,
            capability=capability,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            input_digest=self._digest(facts),
            allowed_evidence_ids=facts["evidence_ids"],
            allowed_material_ids=allowed_material_ids,
        )
        try:
            return self.run_store.create(request)
        except Exception:
            try:
                self.job_service.transition(job.id, JobState.cancelled)
            except Exception:
                pass
            raise

    def run_generation(
        self,
        run_id: str,
        *,
        admission_check: Callable[[], bool] | None = None,
    ) -> ContentMediaRunRead:
        run = self._claim_for_execution(run_id, "generate")
        try:
            self._require_execution_admission(admission_check)
            facts = self._current_generation_facts(
                run.content_item_id, run.revision_id, run.plan_entry_id or ""
            )
            if self._digest(facts) != run.input_digest:
                raise MediaStateError("Generation trust facts changed.")
            prompt = self._generation_prompt(facts)
            images = self.image_adapter.generate_images(ImageGenerationRequest(
                prompt=prompt, prompt_version=run.prompt_version
            ))
            self._require_execution_admission(admission_check)
            if len(images) != 1:
                raise MediaValidationError("Generation must return exactly one image.")
            facts_after = self._current_generation_facts(
                run.content_item_id, run.revision_id, run.plan_entry_id or ""
            )
            if self._digest(facts_after) != run.input_digest:
                raise MediaStateError("Generation trust facts changed after provider call.")
            return self._persist_generated_image(run, images[0])
        except MediaExecutionCancelled:
            self._cancel_claimed_run(run)
            raise
        except (MediaError, ContentError, ModelAdapterError) as error:
            self._record_failure(run, error)
            raise
        except Exception as error:
            self._record_failure(run, error)
            raise

    def run_analysis(
        self,
        run_id: str,
        *,
        admission_check: Callable[[], bool] | None = None,
    ) -> ContentMediaRunRead:
        run = self._claim_for_execution(run_id, "analyze")
        try:
            self._require_execution_admission(admission_check)
            facts = self._current_analysis_facts(
                run.content_item_id, run.revision_id, run.allowed_material_ids
            )
            if self._digest(facts) != run.input_digest:
                raise MediaStateError("Visual-analysis trust facts changed.")
            images, identities = self._read_verified_images(
                run.owner_product_id, run.allowed_material_ids
            )
            result = self.vision_adapter.analyze_images(
                VisionRequest(
                    prompt=self._analysis_prompt(facts),
                    prompt_version=run.prompt_version,
                    material_ids=run.allowed_material_ids,
                    images=images,
                ),
                VisualAssessment,
            )
            self._require_execution_admission(admission_check)
            if result.model != run.model:
                raise MediaValidationError(
                    "Visual result model does not match the reserved run model."
                )
            facts_after = self._current_analysis_facts(
                run.content_item_id, run.revision_id, run.allowed_material_ids
            )
            _, identities_after = self._read_verified_images(
                run.owner_product_id, run.allowed_material_ids
            )
            if (
                self._digest(facts_after) != run.input_digest
                or identities_after != identities
            ):
                raise MediaStateError("Visual-analysis inputs changed after provider call.")
            return self._persist_analysis(run, result, identities)
        except MediaExecutionCancelled:
            self._cancel_claimed_run(run)
            raise
        except (MediaError, ContentError, ModelAdapterError) as error:
            self._record_failure(run, error)
            raise
        except Exception as error:
            self._record_failure(run, error)
            raise

    def get_output_material(self, run_id: str) -> MaterialRead:
        run = self.get_run(run_id)
        if run.status != "succeeded" or run.output_material_id is None:
            raise MediaStateError("Generation run has no successful output material.")
        with self.database.session() as session:
            material = session.get(ProductMaterialRecord, run.output_material_id)
            artifact = session.scalar(select(JobArtifactRecord).where(
                JobArtifactRecord.job_id == run.job_id,
                JobArtifactRecord.kind == "generated_image_provenance",
            ))
            if (
                material is None
                or material.product_id != run.owner_product_id
                or material.kind != "output_image"
                or artifact is None
                or artifact.path != material.path
                or artifact.metadata_json.get("run_id") != run.id
                or artifact.metadata_json.get("material_id") != material.id
                or artifact.metadata_json.get("sha256") != material.sha256
                or artifact.metadata_json.get("mime_type") != material.media_type
            ):
                raise MediaValidationError("Generated material provenance is invalid.")
            provenance = dict(artifact.metadata_json)
            provenance_digest = provenance.pop("provenance_digest", None)
            if provenance_digest != self._digest(provenance):
                raise MediaValidationError("Generated material provenance changed.")
            read = self.content_service._material_read(material)
            if read.availability != "available":
                raise MediaValidationError("Generated material changed after persistence.")
            return read

    def get_analysis_assessment(self, run_id: str) -> VisualAssessmentRead:
        run = self.get_run(run_id)
        if run.status != "succeeded" or run.analysis_artifact_id is None:
            raise MediaStateError("Analysis run has no successful assessment.")
        current_facts = self._current_analysis_facts(
            run.content_item_id, run.revision_id, run.allowed_material_ids
        )
        if self._digest(current_facts) != run.input_digest:
            raise MediaValidationError("Visual assessment content trust changed.")
        with self.database.session() as session:
            artifact = session.get(JobArtifactRecord, run.analysis_artifact_id)
            if (
                artifact is None
                or artifact.job_id != run.job_id
                or artifact.kind != "visual_assessment"
                or artifact.producer != "content_media_service_v1"
            ):
                raise MediaValidationError("Visual assessment binding is invalid.")
            metadata = dict(artifact.metadata_json)
        if metadata.get("run_id") != run.id:
            raise MediaValidationError("Visual assessment run binding is invalid.")
        assessment_digest = metadata.pop("assessment_digest", None)
        if assessment_digest != self._digest(metadata):
            raise MediaValidationError("Visual assessment facts changed.")
        _, current_identities = self._read_verified_images(
            run.owner_product_id, run.allowed_material_ids
        )
        if metadata.get("material_facts") != current_identities:
            raise MediaValidationError("Visual assessment input material changed.")
        return VisualAssessmentRead.model_validate({
            "run_id": run.id,
            "content_item_id": run.content_item_id,
            "revision_id": run.revision_id,
            "material_ids": run.allowed_material_ids,
            "provider": run.provider,
            "model": run.model,
            "prompt_version": run.prompt_version,
            "provider_request_id": metadata.get("provider_request_id"),
            "assessment": metadata.get("assessment"),
            "usage": run.usage,
            "duration_ms": run.duration_ms,
        })

    def _claim_for_execution(self, run_id: str, capability: str) -> ContentMediaRunRead:
        run = self.get_run(run_id)
        if run.capability != capability:
            raise MediaStateError("Media run capability does not match execution path.")
        if run.status != "queued":
            raise MediaStateError("Only a queued media run can be executed.")
        token = str(uuid4())
        return self.run_store.claim(
            run.id,
            expected_version=run.state_version,
            lease_token=token,
            lease_expires_at=_now() + timedelta(minutes=5),
        )

    def cancel_run(self, run_id: str) -> ContentMediaRunRead:
        run = self.get_run(run_id)
        if run.status not in {"queued", "running"}:
            return run
        return self.run_store.cancel(
            run.id,
            expected_version=run.state_version,
            lease_token=run.lease_token,
        )

    def _cancel_claimed_run(self, run: ContentMediaRunRead) -> None:
        try:
            self.run_store.cancel(
                run.id,
                expected_version=run.state_version,
                lease_token=run.lease_token,
            )
        except (MediaRunConflict, MediaRunTransactionUnknown):
            pass

    @staticmethod
    def _require_execution_admission(
        admission_check: Callable[[], bool] | None,
    ) -> None:
        if admission_check is not None and not admission_check():
            raise MediaExecutionCancelled("Media worker shutdown cancelled execution.")

    def _current_generation_facts(
        self, item_id: str, revision_id: str, plan_entry_id: str
    ) -> dict[str, Any]:
        with self.database.session() as session:
            item = session.get(ContentItemRecord, item_id)
            if item is None:
                raise MediaNotFound("Content item does not exist.")
            try:
                self.content_service._validate_item_trust(item, session=session)
            except ContentError as error:
                raise MediaValidationError("Content trust is no longer valid.") from error
            if item.current_revision_id != revision_id:
                raise MediaStateError("The expected content revision is stale.")
            revision = session.get(ContentRevisionRecord, revision_id)
            if revision is None or revision.content_item_id != item.id:
                raise MediaStateError("The expected content revision is missing.")
            entry = next(
                (
                    value for value in revision.image_plan_json
                    if value.get("material_id") == plan_entry_id
                ),
                None,
            )
            if entry is None:
                raise MediaValidationError("Image-plan entry does not belong to this revision.")
            return {
                "item_id": item.id,
                "product_id": item.product_id,
                "revision_id": revision.id,
                "evidence_ids": list(item.evidence_ids_json),
                "entry": dict(entry),
                "title": revision.title,
            }

    def _current_analysis_facts(
        self, item_id: str, revision_id: str, material_ids: list[str]
    ) -> dict[str, Any]:
        if not material_ids or len(material_ids) != len(set(material_ids)) or len(material_ids) > 20:
            raise MediaValidationError("Analysis requires unique managed material IDs.")
        with self.database.session() as session:
            item = session.get(ContentItemRecord, item_id)
            if item is None:
                raise MediaNotFound("Content item does not exist.")
            try:
                self.content_service._validate_item_trust(item, session=session)
            except ContentError as error:
                raise MediaValidationError("Content trust is no longer valid.") from error
            if item.current_revision_id != revision_id:
                raise MediaStateError("The expected content revision is stale.")
            for material_id in material_ids:
                material = session.get(ProductMaterialRecord, material_id)
                generated_binding = session.scalar(select(ContentMediaRunRecord.id).where(
                    ContentMediaRunRecord.content_item_id == item.id,
                    ContentMediaRunRecord.revision_id == revision_id,
                    ContentMediaRunRecord.status == "succeeded",
                    ContentMediaRunRecord.output_material_id == material_id,
                ))
                if (
                    material is None
                    or material.product_id != item.product_id
                    or material.kind != "output_image"
                    or (
                        material_id not in item.image_material_ids_json
                        and generated_binding is None
                    )
                ):
                    raise MediaValidationError(
                        "Visual analysis accepts only images bound to this content item."
                    )
            return {
                "item_id": item.id,
                "product_id": item.product_id,
                "revision_id": revision_id,
                "evidence_ids": list(item.evidence_ids_json),
                "material_ids": list(material_ids),
            }

    def _read_verified_images(
        self, product_id: str, material_ids: list[str]
    ) -> tuple[list[VisionImage], list[dict[str, object]]]:
        images: list[VisionImage] = []
        identities: list[dict[str, object]] = []
        with self.database.session() as session:
            materials = [session.get(ProductMaterialRecord, value) for value in material_ids]
            for material in materials:
                if (
                    material is None
                    or material.product_id != product_id
                    or material.kind != "output_image"
                ):
                    raise MediaValidationError("Visual input material owner changed.")
                before = inspect_contained_artifact(self.runtime_dir, material.path)
                try:
                    payload = read_contained_regular(
                        self.runtime_dir,
                        material.path,
                        limit=min(MAX_MATERIAL_BYTES, material.size_bytes + 1),
                    )
                except UnsafeContentPath as error:
                    raise MediaValidationError("Visual input material changed or is unsafe.") from error
                after = inspect_contained_artifact(self.runtime_dir, material.path)
                if (
                    before.status != "trusted"
                    or after.status != "trusted"
                    or before.identity != after.identity
                    or len(payload) != material.size_bytes
                    or sha256(payload).hexdigest() != material.sha256
                ):
                    raise MediaValidationError("Visual input material changed.")
                images.append(VisionImage(
                    material_id=material.id,
                    mime_type=material.media_type,
                    data=payload,
                ))
                identities.append({
                    "material_id": material.id,
                    "path": material.path,
                    "sha256": material.sha256,
                    "size_bytes": material.size_bytes,
                    "mime_type": material.media_type,
                    "file_identity": list(before.identity or ()),
                })
        return images, identities

    def _persist_generated_image(
        self, run: ContentMediaRunRead, image: GeneratedImage
    ) -> ContentMediaRunRead:
        material_id = str(uuid4())
        suffix = _EXTENSION_BY_MIME[image.mime_type]
        relative_path = (
            f"content-generated/{run.content_item_id}/{run.id}/{material_id}{suffix}"
        )
        logical_name = f"generated-{run.plan_entry_id}{suffix}"
        candidate = ArtifactCleanupCandidate(
            owner_type="material",
            owner_id=material_id,
            relative_path=relative_path,
            expected_sha256=image.sha256,
            expected_size_bytes=len(image.data),
            reason="generated_image_write_reserved",
            not_before=_now() + timedelta(hours=24),
        )
        with self.database.session() as session:
            cleanup = self.cleanup_service.enqueue_in_session(session, candidate)
            session.commit()
        try:
            write_contained_atomic(self.runtime_dir, relative_path, image.data)
        except Exception:
            self._make_cleanup_due(cleanup.id, candidate, "generated_image_write_failed")
            raise

        now = _now()
        try:
            with self.database.session() as session:
                current = session.get(ContentMediaRunRecord, run.id)
                if (
                    current is None
                    or current.status != "running"
                    or current.state_version != run.state_version
                    or current.lease_token != run.lease_token
                ):
                    raise MediaRunConflict("media run state changed")
                count, aggregate = session.execute(select(
                    func.count(ProductMaterialRecord.id),
                    func.coalesce(func.sum(ProductMaterialRecord.size_bytes), 0),
                ).where(ProductMaterialRecord.product_id == run.owner_product_id)).one()
                if count >= 100 or int(aggregate) + len(image.data) > 200 * 1024 * 1024:
                    raise MediaValidationError("Product material limit exceeded.")
                material = ProductMaterialRecord(
                    id=material_id,
                    product_id=run.owner_product_id,
                    logical_name=logical_name,
                    logical_key=windows_name_key(logical_name),
                    version=1,
                    path=relative_path,
                    sha256=image.sha256,
                    size_bytes=len(image.data),
                    media_type=image.mime_type,
                    kind="output_image",
                    created_at=now,
                )
                session.add(material)
                session.flush()
                if not self.cleanup_service.cancel_in_session(
                    session, cleanup.id, candidate=candidate
                ):
                    raise MediaStateError("Generated image cleanup reservation changed.")
                provenance = {
                    "run_id": run.id,
                    "material_id": material_id,
                    "sha256": image.sha256,
                    "size_bytes": len(image.data),
                    "mime_type": image.mime_type,
                    "width": image.width,
                    "height": image.height,
                    "provider": run.provider,
                    "model": run.model,
                    "prompt_version": run.prompt_version,
                    "provider_request_id": image.provider_request_id,
                }
                provenance["provenance_digest"] = self._digest(provenance)
                session.add(JobArtifactRecord(
                    job_id=run.job_id,
                    kind="generated_image_provenance",
                    producer="content_media_service_v1",
                    path=relative_path,
                    metadata_json=provenance,
                    created_at=now,
                ))
                self._finish_run_in_session(
                    session,
                    run,
                    output_material_id=material_id,
                    analysis_artifact_id=None,
                    usage=image.usage,
                    duration_ms=image.duration_ms or 0,
                    attempts=self._safe_attempts(image.raw_evidence),
                    now=now,
                )
                session.commit()
        except SQLAlchemyError as error:
            proven = self._prove_generated_commit(run.id, material_id, cleanup.id, candidate)
            if proven is not None:
                return proven
            self._make_cleanup_due(cleanup.id, candidate, "generated_image_transaction_unknown")
            raise MediaStateError("Generated image transaction outcome is unknown.") from error
        except Exception:
            self._make_cleanup_due(cleanup.id, candidate, "generated_image_persistence_failed")
            raise
        proven = self._prove_generated_commit(run.id, material_id, cleanup.id, candidate)
        if proven is None:
            self._make_cleanup_due(cleanup.id, candidate, "generated_image_transaction_unknown")
            raise MediaStateError("Generated image transaction outcome is unknown.")
        return proven

    def _persist_analysis(self, run, result, identities) -> ContentMediaRunRead:
        now = _now()
        metadata = {
            "schema_version": 1,
            "run_id": run.id,
            "content_item_id": run.content_item_id,
            "revision_id": run.revision_id,
            "material_facts": identities,
            "provider": run.provider,
            "model": run.model,
            "prompt_version": run.prompt_version,
            "provider_request_id": result.raw_evidence.get("provider_request_id"),
            "assessment": result.output.model_dump(mode="json"),
        }
        metadata["assessment_digest"] = self._digest(metadata)
        artifact_id: int | None = None
        try:
            with self.database.session() as session:
                artifact = JobArtifactRecord(
                    job_id=run.job_id,
                    kind="visual_assessment",
                    producer="content_media_service_v1",
                    path=identities[0]["path"],
                    metadata_json=metadata,
                    created_at=now,
                )
                session.add(artifact)
                session.flush()
                artifact_id = artifact.id
                self._finish_run_in_session(
                    session,
                    run,
                    output_material_id=None,
                    analysis_artifact_id=artifact.id,
                    usage=result.usage,
                    duration_ms=result.duration_ms or 0,
                    attempts=self._safe_attempts(result.raw_evidence),
                    now=now,
                )
                session.commit()
        except SQLAlchemyError as error:
            if artifact_id is not None:
                proven = self._prove_analysis_commit(run, artifact_id, metadata)
                if proven is not None:
                    return proven
            raise MediaStateError("Visual assessment transaction outcome is unknown.") from error
        return self.get_run(run.id)

    def _prove_analysis_commit(
        self,
        expected: ContentMediaRunRead,
        artifact_id: int,
        metadata: dict[str, Any],
    ) -> ContentMediaRunRead | None:
        with self.database.session() as session:
            run = session.get(ContentMediaRunRecord, expected.id)
            artifact = session.get(JobArtifactRecord, artifact_id)
            job = session.get(JobRecord, expected.job_id)
            if (
                run is not None
                and run.status == "succeeded"
                and run.analysis_artifact_id == artifact_id
                and artifact is not None
                and artifact.job_id == expected.job_id
                and artifact.kind == "visual_assessment"
                and artifact.producer == "content_media_service_v1"
                and artifact.metadata_json == metadata
                and job is not None
                and str(job.state).removeprefix("JobState.") == "succeeded"
            ):
                return self.run_store._read(run)
        return None

    @staticmethod
    def _finish_run_in_session(
        session,
        run,
        *,
        output_material_id,
        analysis_artifact_id,
        usage,
        duration_ms,
        attempts,
        now,
    ) -> None:
        won = session.execute(update(ContentMediaRunRecord).where(
            ContentMediaRunRecord.id == run.id,
            ContentMediaRunRecord.status == "running",
            ContentMediaRunRecord.state_version == run.state_version,
            ContentMediaRunRecord.lease_token == run.lease_token,
        ).values(
            status="succeeded",
            state_version=run.state_version + 1,
            lease_token=None,
            lease_expires_at=None,
            output_material_id=output_material_id,
            analysis_artifact_id=analysis_artifact_id,
            usage_json=usage,
            duration_ms=duration_ms,
            attempts_json=attempts,
            updated_at=now,
            completed_at=now,
        )).rowcount
        job_won = session.execute(update(JobRecord).where(
            JobRecord.id == run.job_id,
            JobRecord.state == JobState.running,
        ).values(
            state=JobState.succeeded,
            progress_current=1,
            current_stage="media_succeeded",
            lease_expires_at=None,
            updated_at=now,
            completed_at=now,
        )).rowcount
        if won != 1 or job_won != 1:
            raise MediaRunConflict("media run or reserved job state changed")

    def _prove_generated_commit(self, run_id, material_id, cleanup_id, candidate):
        with self.database.session() as session:
            run = session.get(ContentMediaRunRecord, run_id)
            material = session.get(ProductMaterialRecord, material_id)
            cleanup = session.get(ArtifactCleanupRecord, cleanup_id)
            job = None if run is None else session.get(JobRecord, run.job_id)
            if (
                run is not None
                and run.status == "succeeded"
                and run.output_material_id == material_id
                and material is not None
                and material.product_id == run.owner_product_id
                and material.path == candidate.relative_path
                and material.sha256 == candidate.expected_sha256
                and material.size_bytes == candidate.expected_size_bytes
                and cleanup is not None
                and cleanup.state == "cancelled"
                and cleanup.relative_path == candidate.relative_path
                and job is not None
                and str(job.state).removeprefix("JobState.") == "succeeded"
            ):
                return self.run_store._read(run)
        return None

    def _make_cleanup_due(self, cleanup_id, candidate, reason):
        try:
            with self.database.session() as session:
                self.cleanup_service.make_due_in_session(
                    session,
                    cleanup_id,
                    candidate=candidate,
                    due_at=_now(),
                    reason=reason,
                )
                session.commit()
        except SQLAlchemyError as error:
            raise MediaStateError("Generated image cleanup state is unknown.") from error

    def _record_failure(self, run: ContentMediaRunRead, error: Exception) -> None:
        category = error.category if isinstance(error, ModelAdapterError) else (
            "trust_changed" if isinstance(error, (MediaStateError, ContentError))
            else "validation_failed"
        )
        try:
            self.run_store.fail(
                run.id,
                expected_version=run.state_version,
                lease_token=run.lease_token,
                error_category=category,
                error_detail="Media execution failed.",
            )
        except (MediaRunConflict, MediaRunTransactionUnknown):
            pass

    @staticmethod
    def _safe_attempts(raw_evidence: dict[str, Any]) -> list[dict[str, object]]:
        attempts = raw_evidence.get("attempts", [])
        if not isinstance(attempts, list):
            return []
        safe: list[dict[str, object]] = []
        for value in attempts[:20]:
            if (
                isinstance(value, dict)
                and isinstance(value.get("attempt"), int)
                and 1 <= value["attempt"] <= 20
                and isinstance(value.get("category"), str)
                and value["category"].replace("_", "a").isalnum()
                and value["category"].lower() == value["category"]
            ):
                safe.append({"attempt": value["attempt"], "category": value["category"]})
        return safe

    @staticmethod
    def _digest(value: dict[str, Any]) -> str:
        return sha256(json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()

    @staticmethod
    def _generation_prompt(facts: dict[str, Any]) -> str:
        entry = facts["entry"]
        return (
            f"为内容《{facts['title']}》生成一张图片。"
            f"页面角色：{entry['role']}。标题：{entry['headline']}。"
            f"视觉方向：{entry['visual_direction']}。不要添加未提供的事实。"
        )

    @staticmethod
    def _analysis_prompt(facts: dict[str, Any]) -> str:
        return (
            "仅评估这些受管图片与当前内容计划的匹配度、文字可读性、"
            "明显瑕疵和安全问题。输出只是建议，不能作出批准决定。"
        )


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
