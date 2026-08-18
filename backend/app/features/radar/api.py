"""HTTP boundary for ranking snapshots and explainable candidate accounts."""

from datetime import date

from fastapi import APIRouter, HTTPException, Query, Request, status

from backend.app.features.radar.models import AccountRead, RankSnapshotInput, RankSnapshotRead
from backend.app.features.radar.service import RadarService
from backend.app.features.radar.qianfan_service import (
    QianfanCollectionCreate,
    QianfanCollectionQueued,
    QianfanCollectionService,
    QianfanCollectionServiceClosed,
)


router = APIRouter(prefix="/api/v1/radar", tags=["radar"])


def _service(request: Request) -> RadarService:
    service: RadarService | None = request.app.state.radar_service
    if service is None:
        raise HTTPException(status_code=503, detail="SQLite database is unavailable.")
    return service


def _qianfan_service(request: Request) -> QianfanCollectionService:
    service: QianfanCollectionService | None = request.app.state.qianfan_collection_service
    if service is None:
        raise HTTPException(status_code=503, detail="Qianfan collection service is unavailable.")
    return service


@router.post(
    "/qianfan-collections",
    response_model=QianfanCollectionQueued,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_qianfan_collection(
    payload: QianfanCollectionCreate, request: Request
) -> QianfanCollectionQueued:
    try:
        return _qianfan_service(request).enqueue(payload)
    except QianfanCollectionServiceClosed as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@router.post(
    "/rank-snapshots", response_model=RankSnapshotRead, status_code=status.HTTP_201_CREATED
)
def ingest_rank_snapshot(payload: RankSnapshotInput, request: Request) -> RankSnapshotRead:
    return _service(request).ingest_snapshot(payload)


@router.get("/rank-snapshots", response_model=list[RankSnapshotRead])
def list_rank_snapshots(
    request: Request,
    source_date: date | None = None,
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[RankSnapshotRead]:
    return _service(request).list_snapshots(
        source_date=source_date, limit=limit, offset=offset
    )


@router.get("/accounts", response_model=list[AccountRead])
def list_accounts(
    request: Request,
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[AccountRead]:
    return _service(request).list_accounts(limit=limit, offset=offset)


@router.get("/candidates", response_model=list[AccountRead])
def list_candidates(
    request: Request,
    source_date: date,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[AccountRead]:
    return _service(request).list_candidates(source_date=source_date, limit=limit)
