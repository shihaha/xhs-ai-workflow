"""HTTP endpoints for durable workbench jobs and evidence."""

from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Request, status

from backend.app.schemas.jobs import (
    JobArtifactCreate,
    JobArtifactRead,
    JobCreate,
    JobLogCreate,
    JobLogRead,
    JobRead,
    JobTransition,
)
from backend.app.services.jobs import InvalidJobTransition, Job, JobNotFound, JobService


router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


def _service(request: Request) -> JobService:
    service: JobService | None = request.app.state.job_service
    if service is None:
        raise HTTPException(status_code=503, detail="SQLite database is unavailable.")
    return service


@router.post("", response_model=JobRead, status_code=status.HTTP_201_CREATED)
def create_job(payload: JobCreate, request: Request) -> JobRead:
    return _job_read(
        _service(request).create(
            job_type=payload.type,
            input_data=payload.input,
            progress_current=payload.progress_current,
            progress_total=payload.progress_total,
            current_stage=payload.current_stage,
        )
    )


@router.get("", response_model=list[JobRead])
def list_jobs(request: Request) -> list[JobRead]:
    return [_job_read(job) for job in _service(request).list()]


@router.get("/{job_id}", response_model=JobRead)
def get_job(job_id: str, request: Request) -> JobRead:
    return _job_read_or_404(lambda: _service(request).get(job_id))


@router.post("/{job_id}/claim", response_model=JobRead)
def claim_job(job_id: str, request: Request) -> JobRead:
    return _job_read_or_409(lambda: _service(request).claim(job_id))


@router.post("/{job_id}/transition", response_model=JobRead)
def transition_job(job_id: str, payload: JobTransition, request: Request) -> JobRead:
    return _job_read_or_409(
        lambda: _service(request).transition(
            job_id,
            payload.state,
            progress_current=payload.progress_current,
            progress_total=payload.progress_total,
            current_stage=payload.current_stage,
            error_category=payload.error_category,
        )
    )


@router.post(
    "/{job_id}/logs", response_model=JobLogRead, status_code=status.HTTP_201_CREATED
)
def append_log(job_id: str, payload: JobLogCreate, request: Request) -> JobLogRead:
    try:
        log = _service(request).append_log(
            job_id, level=payload.level, message=payload.message
        )
    except JobNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return JobLogRead(level=log.level, message=log.message)


@router.post(
    "/{job_id}/artifacts",
    response_model=JobArtifactRead,
    status_code=status.HTTP_201_CREATED,
)
def attach_artifact(
    job_id: str, payload: JobArtifactCreate, request: Request
) -> JobArtifactRead:
    try:
        artifact = _service(request).attach_artifact(
            job_id, kind=payload.kind, path=payload.path, metadata=payload.metadata
        )
    except JobNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return JobArtifactRead(
        kind=artifact.kind, path=artifact.path, metadata=artifact.metadata
    )


def _job_read_or_404(action: Callable[[], Job]) -> JobRead:
    try:
        return _job_read(action())
    except JobNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


def _job_read_or_409(action: Callable[[], Job]) -> JobRead:
    try:
        return _job_read_or_404(action)
    except InvalidJobTransition as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


def _job_read(job: object) -> JobRead:
    return JobRead.model_validate(job, from_attributes=True)
