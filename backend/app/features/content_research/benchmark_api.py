"""HTTP routes for tutorial benchmark-note collection."""

from fastapi import APIRouter, HTTPException, Request, status

from backend.app.features.content_research.benchmark_schemas import (
    BenchmarkOverviewRead,
    BenchmarkSearchCreate,
    BenchmarkSearchRead,
)
from backend.app.features.content_research.benchmark_service import (
    BenchmarkResearchConflict,
    BenchmarkResearchNotFound,
    BenchmarkResearchService,
    BenchmarkResearchUnavailable,
)


router = APIRouter(
    prefix="/api/v1/content-research",
    tags=["content-research-benchmarks"],
)


def _service(request: Request) -> BenchmarkResearchService:
    service: BenchmarkResearchService | None = request.app.state.benchmark_research_service
    if service is None:
        raise HTTPException(status_code=503, detail="SQLite database is unavailable.")
    return service


def _translate(error: Exception) -> HTTPException:
    if isinstance(error, BenchmarkResearchNotFound):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, BenchmarkResearchUnavailable):
        return HTTPException(status_code=503, detail=str(error))
    if isinstance(error, BenchmarkResearchConflict):
        return HTTPException(status_code=409, detail=str(error))
    return HTTPException(status_code=422, detail=str(error))


@router.post(
    "/dossiers/{dossier_id}/benchmark-searches",
    response_model=BenchmarkSearchRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_benchmark_search(
    dossier_id: str,
    payload: BenchmarkSearchCreate,
    request: Request,
) -> BenchmarkSearchRead:
    try:
        return _service(request).start_search(dossier_id, payload)
    except (
        BenchmarkResearchNotFound,
        BenchmarkResearchUnavailable,
        BenchmarkResearchConflict,
    ) as error:
        raise _translate(error) from error


@router.get(
    "/dossiers/{dossier_id}/benchmarks",
    response_model=BenchmarkOverviewRead,
)
def benchmark_overview(
    dossier_id: str, request: Request
) -> BenchmarkOverviewRead:
    try:
        return _service(request).overview(dossier_id)
    except (
        BenchmarkResearchNotFound,
        BenchmarkResearchUnavailable,
        BenchmarkResearchConflict,
    ) as error:
        raise _translate(error) from error
