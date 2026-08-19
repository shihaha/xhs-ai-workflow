"""Small persistence boundary for media-run CAS and restart recovery."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, InvalidRequestError, SQLAlchemyError

from backend.app.db import Database
from backend.app.features.media.models import ContentMediaRunRecord
from backend.app.features.media.schemas import (
    ContentMediaRunCreate,
    ContentMediaRunRead,
    MediaCompletionFacts,
)


class MediaRunConflict(RuntimeError):
    """The requested run identity or state no longer matches durable facts."""


class MediaRunTransactionUnknown(RuntimeError):
    """A commit acknowledgement was lost and the exact result cannot be proven."""


_SAFE_ERROR_CATEGORY = {
    "media_unconfigured": "unconfigured",
    "media_authentication_failed": "authentication_failed",
    "media_retry_exhausted": "provider_unavailable",
    "media_request_failed": "provider_rejected",
    "media_output_invalid": "invalid_output",
    "provider_failure": "provider_failure",
    "validation_failed": "validation_failed",
    "trust_changed": "trust_changed",
    "transaction_unknown": "transaction_unknown",
    "state_changed": "state_changed",
    "safety_rejected": "safety_rejected",
}


def _safe_failure_facts(category: str, *, needs_human: bool) -> tuple[str, str]:
    """Map an internal category to bounded facts; never retain caller exception text."""

    safe_category = _SAFE_ERROR_CATEGORY.get(category, "internal_failure")
    safe_detail = (
        "Media run requires operator review."
        if needs_human
        else "Media run failed."
    )
    return safe_category, safe_detail


class ContentMediaRunStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _read(record: ContentMediaRunRecord) -> ContentMediaRunRead:
        return ContentMediaRunRead.model_validate({
            "id": record.id,
            "job_id": record.job_id,
            "owner_product_id": record.owner_product_id,
            "content_item_id": record.content_item_id,
            "revision_id": record.revision_id,
            "plan_entry_id": record.plan_entry_id,
            "capability": record.capability,
            "status": record.status,
            "state_version": record.state_version,
            "provider": record.provider,
            "model": record.model,
            "prompt_version": record.prompt_version,
            "input_digest": record.input_digest,
            "allowed_evidence_ids": record.allowed_evidence_ids_json,
            "allowed_material_ids": record.allowed_material_ids_json,
            "output_material_id": record.output_material_id,
            "analysis_artifact_id": record.analysis_artifact_id,
            "usage": record.usage_json,
            "duration_ms": record.duration_ms,
            "attempts": record.attempts_json,
            "error_category": record.error_category,
            "error_detail": record.error_detail,
            "lease_token": record.lease_token,
            "lease_expires_at": record.lease_expires_at,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "completed_at": record.completed_at,
        })

    def get(self, run_id: str) -> ContentMediaRunRead | None:
        with self.database.session() as session:
            record = session.get(ContentMediaRunRecord, run_id)
            return None if record is None else self._read(record)

    def create(self, request: ContentMediaRunCreate) -> ContentMediaRunRead:
        now = datetime.now(timezone.utc)
        with self.database.session() as session:
            owner = session.execute(
                select(ContentMediaRunRecord.id).where(ContentMediaRunRecord.id == request.id)
            ).scalar_one_or_none()
            if owner is not None:
                raise MediaRunConflict("media run id already exists")
            from backend.app.features.content.models import ContentItemRecord, ContentRevisionRecord
            from backend.app.models.jobs import JobRecord

            item = session.get(ContentItemRecord, request.content_item_id)
            revision = session.get(ContentRevisionRecord, request.revision_id)
            job = session.get(JobRecord, request.job_id)
            expected_job_type = f"content_image_{'generation' if request.capability == 'generate' else 'analysis'}"
            if (
                item is None or item.product_id != request.owner_product_id
                or revision is None or revision.content_item_id != request.content_item_id
                or job is None or job.type != expected_job_type or str(job.state) not in {"queued", "JobState.queued"}
            ):
                raise MediaRunConflict("media run owner, revision, or reserved job is stale")
            record = ContentMediaRunRecord(
                id=request.id, job_id=request.job_id,
                owner_product_id=request.owner_product_id,
                content_item_id=request.content_item_id, revision_id=request.revision_id,
                plan_entry_id=request.plan_entry_id, capability=request.capability,
                status="queued", state_version=0, provider=request.provider,
                model=request.model, prompt_version=request.prompt_version,
                input_digest=request.input_digest,
                allowed_evidence_ids_json=request.allowed_evidence_ids,
                allowed_material_ids_json=request.allowed_material_ids,
                usage_json={}, attempts_json=[], created_at=now, updated_at=now,
            )
            session.add(record)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise MediaRunConflict("an open generation run already exists") from exc
            except SQLAlchemyError as exc:
                try:
                    session.rollback()
                except InvalidRequestError:
                    session.close()
                proven = self.get(request.id)
                if proven is not None and self._matches_create_request(proven, request):
                    return proven
                raise MediaRunTransactionUnknown("media run creation outcome is unknown") from exc
            session.refresh(record)
            return self._read(record)

    @staticmethod
    def _matches_create_request(
        proven: ContentMediaRunRead, request: ContentMediaRunCreate,
    ) -> bool:
        return (
            proven.status == "queued" and proven.state_version == 0
            and proven.job_id == request.job_id
            and proven.owner_product_id == request.owner_product_id
            and proven.content_item_id == request.content_item_id
            and proven.revision_id == request.revision_id
            and proven.plan_entry_id == request.plan_entry_id
            and proven.capability == request.capability
            and proven.provider == request.provider and proven.model == request.model
            and proven.prompt_version == request.prompt_version
            and proven.input_digest == request.input_digest
            and proven.allowed_evidence_ids == request.allowed_evidence_ids
            and proven.allowed_material_ids == request.allowed_material_ids
            and proven.output_material_id is None
            and proven.analysis_artifact_id is None
            and proven.error_category is None and proven.completed_at is None
        )

    def claim(
        self, run_id: str, *, expected_version: int,
        lease_token: str, lease_expires_at: datetime,
    ) -> ContentMediaRunRead:
        now = datetime.now(timezone.utc)
        return self._cas_terminal_or_claim(
            run_id, expected_version=expected_version, expected_status="queued",
            values={"status": "running", "state_version": expected_version + 1,
                    "lease_token": lease_token, "lease_expires_at": lease_expires_at,
                    "updated_at": now},
        )

    def fail(
        self, run_id: str, *, expected_version: int, lease_token: str | None,
        error_category: str, error_detail: str,
    ) -> ContentMediaRunRead:
        del error_detail
        safe_category, safe_detail = _safe_failure_facts(
            error_category, needs_human=False
        )
        now = datetime.now(timezone.utc)
        return self._cas_terminal_or_claim(
            run_id, expected_version=expected_version, expected_status="running",
            expected_lease_token=lease_token,
            values={"status": "failed", "state_version": expected_version + 1,
                    "lease_token": None, "lease_expires_at": None,
                    "error_category": safe_category, "error_detail": safe_detail,
                    "updated_at": now, "completed_at": now},
        )

    def needs_human(
        self, run_id: str, *, expected_version: int, lease_token: str | None,
        error_category: str, error_detail: str,
    ) -> ContentMediaRunRead:
        del error_detail
        safe_category, safe_detail = _safe_failure_facts(
            error_category, needs_human=True
        )
        now = datetime.now(timezone.utc)
        return self._cas_terminal_or_claim(
            run_id, expected_version=expected_version, expected_status="running",
            expected_lease_token=lease_token,
            values={
                "status": "needs_human", "state_version": expected_version + 1,
                "lease_token": None, "lease_expires_at": None,
                "error_category": safe_category, "error_detail": safe_detail,
                "updated_at": now, "completed_at": now,
            },
        )

    def cancel(
        self, run_id: str, *, expected_version: int, lease_token: str | None,
    ) -> ContentMediaRunRead:
        current = self.get(run_id)
        if current is None or current.status not in {"queued", "running"}:
            raise MediaRunConflict("only unfinished media runs may be cancelled")
        if current.status == "running" and current.lease_token != lease_token:
            raise MediaRunConflict("media run lease changed")
        now = datetime.now(timezone.utc)
        return self._cas_terminal_or_claim(
            run_id, expected_version=expected_version,
            expected_status=current.status,
            expected_lease_token=lease_token,
            values={
                "status": "cancelled", "state_version": expected_version + 1,
                "lease_token": None, "lease_expires_at": None,
                "updated_at": now, "completed_at": now,
            },
        )

    def succeed_generation(
        self, run_id: str, *, expected_version: int, lease_token: str | None,
        output_material_id: str, usage: dict[str, int], duration_ms: int,
        attempts: list[dict[str, object]],
    ) -> ContentMediaRunRead:
        facts = MediaCompletionFacts.model_validate({
            "usage": usage, "duration_ms": duration_ms, "attempts": attempts,
        })
        with self.database.session() as session:
            from backend.app.features.content.models import ProductMaterialRecord
            run = session.get(ContentMediaRunRecord, run_id)
            material = session.get(ProductMaterialRecord, output_material_id)
            if (
                run is None or run.capability != "generate" or material is None
                or material.product_id != run.owner_product_id
                or material.kind != "output_image"
            ):
                raise MediaRunConflict("generated output is not bound to this run owner")
        now = datetime.now(timezone.utc)
        return self._cas_terminal_or_claim(
            run_id, expected_version=expected_version, expected_status="running",
            expected_lease_token=lease_token,
            values={
                "status": "succeeded", "state_version": expected_version + 1,
                "lease_token": None, "lease_expires_at": None,
                "output_material_id": output_material_id,
                "usage_json": facts.usage,
                "duration_ms": facts.duration_ms,
                "attempts_json": [item.model_dump() for item in facts.attempts],
                "updated_at": now, "completed_at": now,
            },
        )

    def succeed_analysis(
        self, run_id: str, *, expected_version: int, lease_token: str | None,
        analysis_artifact_id: int, usage: dict[str, int], duration_ms: int,
        attempts: list[dict[str, object]],
    ) -> ContentMediaRunRead:
        facts = MediaCompletionFacts.model_validate({
            "usage": usage, "duration_ms": duration_ms, "attempts": attempts,
        })
        with self.database.session() as session:
            from backend.app.models.jobs import JobArtifactRecord
            run = session.get(ContentMediaRunRecord, run_id)
            artifact = session.get(JobArtifactRecord, analysis_artifact_id)
            if (
                run is None or run.capability != "analyze" or artifact is None
                or artifact.job_id != run.job_id
            ):
                raise MediaRunConflict("analysis artifact is not bound to this run job")
        now = datetime.now(timezone.utc)
        return self._cas_terminal_or_claim(
            run_id, expected_version=expected_version, expected_status="running",
            expected_lease_token=lease_token,
            values={
                "status": "succeeded", "state_version": expected_version + 1,
                "lease_token": None, "lease_expires_at": None,
                "analysis_artifact_id": analysis_artifact_id,
                "usage_json": facts.usage,
                "duration_ms": facts.duration_ms,
                "attempts_json": [item.model_dump() for item in facts.attempts],
                "updated_at": now, "completed_at": now,
            },
        )

    def _cas_terminal_or_claim(
        self, run_id: str, *, expected_version: int, expected_status: str,
        values: dict[str, object], expected_lease_token: str | None = None,
    ) -> ContentMediaRunRead:
        with self.database.session() as session:
            from backend.app.models.jobs import JobRecord

            predicate = (
                (ContentMediaRunRecord.id == run_id)
                & (ContentMediaRunRecord.status == expected_status)
                & (ContentMediaRunRecord.state_version == expected_version)
            )
            if expected_status == "running":
                predicate &= ContentMediaRunRecord.lease_token == expected_lease_token
            result = session.execute(update(ContentMediaRunRecord).where(predicate).values(**values))
            if result.rowcount != 1:
                session.rollback()
                raise MediaRunConflict("media run state changed")
            job_values: dict[str, object] = {
                "state": values["status"],
                "updated_at": values["updated_at"],
            }
            if values["status"] == "running":
                job_values.update({
                    "started_at": values["updated_at"],
                    "lease_expires_at": values["lease_expires_at"],
                    "current_stage": "media_provider",
                })
            else:
                job_values.update({
                    "completed_at": values["completed_at"],
                    "lease_expires_at": None,
                    "error_category": values.get("error_category"),
                    "current_stage": f"media_{values['status']}",
                })
            job_result = session.execute(
                update(JobRecord)
                .where(
                    (JobRecord.id == select(ContentMediaRunRecord.job_id).where(ContentMediaRunRecord.id == run_id).scalar_subquery())
                    & (JobRecord.state == expected_status)
                )
                .values(**job_values)
            )
            if job_result.rowcount != 1:
                session.rollback()
                raise MediaRunConflict("reserved job state changed")
            try:
                session.commit()
            except SQLAlchemyError as exc:
                try:
                    session.rollback()
                except InvalidRequestError:
                    session.close()
                proven = self.get(run_id)
                job_is_proven = False
                if proven is not None:
                    from backend.app.models.jobs import JobRecord
                    with self.database.session() as proof_session:
                        proof_job = proof_session.get(JobRecord, proven.job_id)
                        job_is_proven = (
                            proof_job is not None
                            and str(proof_job.state).removeprefix("JobState.") == values["status"]
                        )
                if proven is not None and job_is_proven and all(
                    getattr(proven, key) == value for key, value in values.items()
                    if key not in {"updated_at", "completed_at"}
                ):
                    return proven
                raise MediaRunTransactionUnknown("media run transition outcome is unknown") from exc
        proven = self.get(run_id)
        if proven is None:
            raise MediaRunTransactionUnknown("committed media run cannot be read back")
        return proven

    def list_recoverable(self, *, now: datetime) -> list[ContentMediaRunRead]:
        with self.database.session() as session:
            records = session.scalars(
                select(ContentMediaRunRecord).where(
                    (ContentMediaRunRecord.status == "queued")
                    | ((ContentMediaRunRecord.status == "running") & (ContentMediaRunRecord.lease_expires_at <= now))
                ).order_by(ContentMediaRunRecord.created_at, ContentMediaRunRecord.id)
            ).all()
            return [self._read(record) for record in records]

    def list_startup_recoverable(self) -> list[ContentMediaRunRead]:
        """Return all work that predates a fresh worker process."""

        with self.database.session() as session:
            records = session.scalars(
                select(ContentMediaRunRecord)
                .where(ContentMediaRunRecord.status.in_(("queued", "running")))
                .order_by(ContentMediaRunRecord.created_at, ContentMediaRunRecord.id)
            ).all()
            return [self._read(record) for record in records]

    def list_for_item(self, content_item_id: str) -> list[ContentMediaRunRead]:
        with self.database.session() as session:
            records = session.scalars(
                select(ContentMediaRunRecord)
                .where(ContentMediaRunRecord.content_item_id == content_item_id)
                .order_by(ContentMediaRunRecord.created_at, ContentMediaRunRecord.id)
            ).all()
            return [self._read(record) for record in records]
