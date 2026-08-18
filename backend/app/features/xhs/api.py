"""HTTP boundary for durable XHS account and note read collection."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from backend.app.features.xhs.service import (
    AccountNoteRead,
    AccountProfileRead,
    CollectionFactNotFound,
    CollectionServiceClosed,
    SearchResultsRead,
    XhsCollectionService,
)
from backend.app.services.jobs import JobNotFound


router = APIRouter(prefix="/api/v1", tags=["xhs-collections"])


class AccountCollectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_note_count: StrictInt = Field(ge=0, le=1000)


class SearchCollectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    keyword: str = Field(min_length=1, max_length=500)
    expected_count: StrictInt = Field(ge=0, le=1000)

    @field_validator("keyword")
    @classmethod
    def normalize_keyword(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or normalized.startswith("-") or any(ord(char) < 32 for char in normalized):
            raise ValueError("keyword must be a safe non-empty search term")
        return normalized


class CollectionQueued(BaseModel):
    job_id: str
    status: str = "queued"


def _service(request: Request) -> XhsCollectionService:
    service: XhsCollectionService | None = request.app.state.xhs_collection_service
    if service is None:
        raise HTTPException(status_code=503, detail="XHS collection service is unavailable.")
    return service


@router.post(
    "/accounts/{user_id}/collections",
    response_model=CollectionQueued,
    status_code=status.HTTP_202_ACCEPTED,
)
def collect_account(user_id: str, payload: AccountCollectionCreate, request: Request) -> CollectionQueued:
    try:
        job = _service(request).submit_account(user_id, payload.expected_note_count)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except CollectionServiceClosed as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return CollectionQueued(job_id=job.id)


@router.post(
    "/notes/search-collections",
    response_model=CollectionQueued,
    status_code=status.HTTP_202_ACCEPTED,
)
def collect_search(payload: SearchCollectionCreate, request: Request) -> CollectionQueued:
    try:
        job = _service(request).submit_search(payload.keyword, payload.expected_count)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except CollectionServiceClosed as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return CollectionQueued(job_id=job.id)


@router.get("/accounts/{user_id}/profile", response_model=AccountProfileRead)
def get_account_profile(user_id: str, request: Request) -> AccountProfileRead:
    try:
        return _service(request).get_profile(user_id)
    except CollectionFactNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/accounts/{user_id}/notes", response_model=list[AccountNoteRead])
def list_account_notes(user_id: str, request: Request) -> list[AccountNoteRead]:
    try:
        return _service(request).list_account_notes(user_id)
    except CollectionFactNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/note-search-results", response_model=SearchResultsRead)
def get_note_search_results(
    request: Request,
    job_id: Annotated[str, Query(min_length=1, max_length=36)],
) -> SearchResultsRead:
    try:
        return _service(request).get_search_results(job_id)
    except (CollectionFactNotFound, JobNotFound, OSError, ValueError, KeyError) as error:
        raise HTTPException(status_code=404, detail=f"Search results for job {job_id} do not exist.") from error
