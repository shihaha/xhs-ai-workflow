"""HTTP boundary for persisted grounded analyses and opportunity cards."""

from fastapi import APIRouter, HTTPException, Request, status

from backend.app.features.analysis.schemas import (
    AnalysisCreate,
    AnalysisEvidenceRead,
    AnalysisRead,
    OpportunityRead,
)
from backend.app.features.analysis.service import (
    AnalysisNotFound,
    AnalysisService,
    EvidenceAccountMismatch,
    EvidenceNotFound,
)


router = APIRouter(prefix="/api/v1", tags=["analysis"])


def _service(request: Request) -> AnalysisService:
    service: AnalysisService | None = request.app.state.analysis_service
    if service is None:
        raise HTTPException(status_code=503, detail="SQLite database is unavailable.")
    return service


@router.post("/analyses", response_model=AnalysisRead, status_code=status.HTTP_201_CREATED)
def create_analysis(payload: AnalysisCreate, request: Request) -> AnalysisRead:
    adapter = request.app.state.bailian_adapter
    if adapter is None or not adapter.configured:
        raise HTTPException(status_code=503, detail="Bailian is not configured.")
    try:
        return _service(request).create(payload)
    except (EvidenceNotFound, EvidenceAccountMismatch) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/analyses", response_model=list[AnalysisRead])
def list_analyses(request: Request) -> list[AnalysisRead]:
    return _service(request).list()


@router.get("/analysis-evidence", response_model=list[AnalysisEvidenceRead])
def list_analysis_evidence(
    request: Request, account_user_id: str | None = None
) -> list[AnalysisEvidenceRead]:
    return _service(request).list_evidence(account_user_id=account_user_id)


@router.get("/analyses/{analysis_id}", response_model=AnalysisRead)
def get_analysis(analysis_id: str, request: Request) -> AnalysisRead:
    try:
        return _service(request).get(analysis_id)
    except AnalysisNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/opportunities", response_model=list[OpportunityRead])
def list_opportunities(request: Request) -> list[OpportunityRead]:
    return _service(request).list_opportunities()
