"""HTTP boundary for business-first workbench projections."""

from fastapi import APIRouter, HTTPException, Request

from backend.app.features.business.schemas import DemandRadarRead
from backend.app.features.business.service import BusinessWorkbenchService

router = APIRouter(prefix="/api/v1/business", tags=["business-workbench"])


def _service(request: Request) -> BusinessWorkbenchService:
    service: BusinessWorkbenchService | None = request.app.state.business_workbench_service
    if service is None:
        raise HTTPException(status_code=503, detail="SQLite database is unavailable.")
    return service


@router.get("/demand-radar", response_model=DemandRadarRead)
def demand_radar(request: Request) -> DemandRadarRead:
    return _service(request).demand_radar()
