"""HTTP routes for the tutorial-defined Phase-D research workflow."""

from fastapi import APIRouter, HTTPException, Request, status

from backend.app.features.content_research.schemas import (
    FinishedProductDossierCreate,
    FinishedProductDossierRead,
    KeywordPlanRead,
    KeywordPlanReplace,
)
from backend.app.features.content_research.service import (
    ContentResearchConflict,
    ContentResearchModelFailure,
    ContentResearchModelUnavailable,
    ContentResearchNotFound,
    ContentResearchService,
)


router = APIRouter(prefix="/api/v1/content-research", tags=["content-research"])


def _service(request: Request) -> ContentResearchService:
    service: ContentResearchService | None = request.app.state.content_research_service
    if service is None:
        raise HTTPException(status_code=503, detail="SQLite database is unavailable.")
    return service


def _translate(error: Exception) -> HTTPException:
    if isinstance(error, ContentResearchNotFound):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, ContentResearchConflict):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, ContentResearchModelUnavailable):
        return HTTPException(status_code=503, detail=str(error))
    if isinstance(error, ContentResearchModelFailure):
        return HTTPException(status_code=502, detail=str(error))
    return HTTPException(status_code=422, detail=str(error))


@router.post(
    "/dossiers",
    response_model=FinishedProductDossierRead,
    status_code=status.HTTP_201_CREATED,
)
def create_dossier(
    payload: FinishedProductDossierCreate, request: Request
) -> FinishedProductDossierRead:
    try:
        return _service(request).create_dossier(payload)
    except (ContentResearchConflict, ContentResearchNotFound) as error:
        raise _translate(error) from error


@router.get("/dossiers", response_model=list[FinishedProductDossierRead])
def list_dossiers(request: Request) -> list[FinishedProductDossierRead]:
    return _service(request).list_dossiers()


@router.get("/dossiers/{dossier_id}", response_model=FinishedProductDossierRead)
def get_dossier(dossier_id: str, request: Request) -> FinishedProductDossierRead:
    try:
        return _service(request).get_dossier(dossier_id)
    except ContentResearchNotFound as error:
        raise _translate(error) from error


@router.put("/dossiers/{dossier_id}/keywords", response_model=KeywordPlanRead)
def replace_keyword_plan(
    dossier_id: str, payload: KeywordPlanReplace, request: Request
) -> KeywordPlanRead:
    try:
        return _service(request).replace_keyword_plan(dossier_id, payload)
    except (ContentResearchConflict, ContentResearchNotFound) as error:
        raise _translate(error) from error


@router.post("/dossiers/{dossier_id}/keywords/generate", response_model=KeywordPlanRead)
def generate_keyword_plan(dossier_id: str, request: Request) -> KeywordPlanRead:
    try:
        return _service(request).generate_keyword_plan(dossier_id)
    except (
        ContentResearchConflict,
        ContentResearchNotFound,
        ContentResearchModelUnavailable,
        ContentResearchModelFailure,
    ) as error:
        raise _translate(error) from error


@router.get("/dossiers/{dossier_id}/keywords", response_model=KeywordPlanRead)
def get_keyword_plan(dossier_id: str, request: Request) -> KeywordPlanRead:
    try:
        return _service(request).get_keyword_plan(dossier_id)
    except ContentResearchNotFound as error:
        raise _translate(error) from error


@router.get("/dossiers/{dossier_id}/keyword-runs", response_model=list[KeywordPlanRead])
def list_keyword_plan_runs(dossier_id: str, request: Request) -> list[KeywordPlanRead]:
    try:
        return _service(request).list_keyword_plan_runs(dossier_id)
    except ContentResearchNotFound as error:
        raise _translate(error) from error
