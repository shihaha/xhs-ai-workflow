"""HTTP boundary for business-first workbench projections."""

from fastapi import APIRouter, HTTPException, Request, Response

from backend.app.features.business.schemas import DemandRadarRead
from backend.app.features.business.service import (
    BusinessWorkbenchService,
    DemandRadarMediaNotFound,
    DemandRadarMediaUnsafe,
)

router = APIRouter(prefix="/api/v1/business", tags=["business-workbench"])


def _service(request: Request) -> BusinessWorkbenchService:
    service: BusinessWorkbenchService | None = request.app.state.business_workbench_service
    if service is None:
        raise HTTPException(status_code=503, detail="SQLite database is unavailable.")
    return service


@router.get("/demand-radar", response_model=DemandRadarRead)
def demand_radar(request: Request) -> DemandRadarRead:
    return _service(request).demand_radar()


@router.get("/demand-radar/{opportunity_id}/media/{artifact_id}")
def demand_radar_media(
    opportunity_id: str,
    artifact_id: int,
    request: Request,
) -> Response:
    try:
        image = _service(request).demand_radar_image(opportunity_id, artifact_id)
    except DemandRadarMediaNotFound as error:
        # Do not reveal whether the id exists outside this Opportunity's
        # immutable evidence scope.
        raise HTTPException(status_code=404, detail="Demand Radar image not found.") from error
    except DemandRadarMediaUnsafe as error:
        raise HTTPException(status_code=409, detail="Demand Radar image failed integrity checks.") from error
    return Response(
        content=image.payload,
        media_type=image.media_type,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
            "ETag": f'"sha256-{image.sha256}"',
        },
    )
