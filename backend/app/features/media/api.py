"""Write-only submission and read-only durable media-run HTTP routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.app.db import is_canonical_uuid_text
from backend.app.features.media.schemas import ContentMediaRunRead, VisualAssessmentRead
from backend.app.features.media.service import (
    ContentMediaService,
    MediaNotFound,
    MediaStateError,
    MediaValidationError,
)
from backend.app.features.media.store import MediaRunConflict, MediaRunTransactionUnknown
from backend.app.features.media.worker import (
    ContentMediaWorker,
    MediaProviderUnavailable,
    MediaWorkerClosed,
)


router = APIRouter(prefix="/api/v1", tags=["content-media"])


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ImageGenerationCreate(_StrictRequest):
    expected_revision_id: str = Field(min_length=36, max_length=36)
    image_plan_entry_id: str = Field(min_length=1, max_length=200)

    @field_validator("expected_revision_id")
    @classmethod
    def canonical_revision(cls, value: str) -> str:
        if not is_canonical_uuid_text(value):
            raise ValueError("expected_revision_id must be a canonical UUID")
        return value


class ImageAnalysisCreate(_StrictRequest):
    expected_revision_id: str = Field(min_length=36, max_length=36)
    material_ids: list[str] = Field(min_length=1, max_length=20)

    @field_validator("expected_revision_id")
    @classmethod
    def canonical_revision(cls, value: str) -> str:
        if not is_canonical_uuid_text(value):
            raise ValueError("expected_revision_id must be a canonical UUID")
        return value

    @field_validator("material_ids")
    @classmethod
    def canonical_materials(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(
            not is_canonical_uuid_text(item) for item in value
        ):
            raise ValueError("material_ids must be unique canonical UUIDs")
        return value


def _service(request: Request) -> ContentMediaService:
    service: ContentMediaService | None = request.app.state.content_media_service
    if service is None:
        raise HTTPException(status_code=503, detail="Content media service is unavailable.")
    return service


def _worker(request: Request) -> ContentMediaWorker:
    worker: ContentMediaWorker | None = request.app.state.content_media_worker
    if worker is None:
        raise HTTPException(status_code=503, detail="Content media worker is unavailable.")
    return worker


def _translate(error: Exception) -> HTTPException:
    if isinstance(error, (MediaProviderUnavailable, MediaWorkerClosed)):
        return HTTPException(status_code=503, detail=str(error))
    if isinstance(error, MediaNotFound):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, (MediaStateError, MediaRunConflict)):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, MediaRunTransactionUnknown):
        return HTTPException(status_code=503, detail="Media run outcome is unavailable.")
    return HTTPException(status_code=422, detail=str(error))


@router.post(
    "/content-items/{item_id}/image-generations",
    response_model=ContentMediaRunRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def submit_generation(
    item_id: str, payload: ImageGenerationCreate, request: Request
) -> ContentMediaRunRead:
    try:
        return _worker(request).submit_generation(
            item_id,
            expected_revision_id=payload.expected_revision_id,
            image_plan_entry_id=payload.image_plan_entry_id,
        )
    except (
        MediaProviderUnavailable,
        MediaWorkerClosed,
        MediaNotFound,
        MediaStateError,
        MediaValidationError,
        MediaRunConflict,
        MediaRunTransactionUnknown,
    ) as error:
        raise _translate(error) from error


@router.post(
    "/content-items/{item_id}/image-analyses",
    response_model=ContentMediaRunRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def submit_analysis(
    item_id: str, payload: ImageAnalysisCreate, request: Request
) -> ContentMediaRunRead:
    try:
        return _worker(request).submit_analysis(
            item_id,
            expected_revision_id=payload.expected_revision_id,
            material_ids=payload.material_ids,
        )
    except (
        MediaProviderUnavailable,
        MediaWorkerClosed,
        MediaNotFound,
        MediaStateError,
        MediaValidationError,
        MediaRunConflict,
        MediaRunTransactionUnknown,
    ) as error:
        raise _translate(error) from error


@router.get(
    "/content-items/{item_id}/media-runs",
    response_model=list[ContentMediaRunRead],
)
def list_media_runs(item_id: str, request: Request) -> list[ContentMediaRunRead]:
    try:
        return _service(request).list_runs(item_id)
    except (MediaNotFound, MediaStateError, MediaValidationError) as error:
        raise _translate(error) from error

@router.get("/content-media-runs/{run_id}", response_model=ContentMediaRunRead)
def get_media_run(run_id: str, request: Request) -> ContentMediaRunRead:
    try:
        return _service(request).get_run(run_id)
    except (MediaNotFound, MediaStateError, MediaValidationError) as error:
        raise _translate(error) from error


@router.get(
    "/content-media-runs/{run_id}/assessment",
    response_model=VisualAssessmentRead,
)
def get_media_assessment(run_id: str, request: Request) -> VisualAssessmentRead:
    """Read sealed advisory output; this route has no approval side effect."""

    try:
        return _service(request).get_analysis_assessment(run_id)
    except (MediaNotFound, MediaStateError, MediaValidationError) as error:
        raise _translate(error) from error
