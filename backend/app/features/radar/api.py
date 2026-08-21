"""HTTP boundary for ranking snapshots and explainable candidate accounts."""

from datetime import date

from fastapi import APIRouter, HTTPException, Query, Request, status

from backend.app.features.radar.models import (
    AccountRead,
    CandidateAdvanceCreate,
    CandidateAdvanceQueued,
    CandidateFunnelRead,
    CandidatePrescreenCreate,
    RankSnapshotInput,
    RankSnapshotRead,
)
from backend.app.features.radar.service import (
    CandidateFunnelBlocked,
    CandidateFunnelExhausted,
    RadarService,
)
from backend.app.features.radar.qianfan_service import (
    QianfanCollectionCreate,
    QianfanCollectionQueued,
    QianfanCollectionService,
    QianfanCollectionServiceClosed,
)
from backend.app.features.shops.service import ShopCollectionCreate, ShopCollectionService


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
    limit: int = Query(default=20, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[AccountRead]:
    return _service(request).list_candidates(
        source_date=source_date, limit=limit, offset=offset
    )


@router.post(
    "/candidate-prescreens",
    response_model=list[CandidateFunnelRead],
    status_code=status.HTTP_201_CREATED,
)
def prescreen_candidates(
    payload: CandidatePrescreenCreate, request: Request
) -> list[CandidateFunnelRead]:
    try:
        return _service(request).prescreen_candidates(payload)
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@router.get("/candidate-funnel", response_model=list[CandidateFunnelRead])
def list_candidate_funnel(
    request: Request,
    source_date: date,
    limit: int = Query(default=1000, ge=1, le=1000),
) -> list[CandidateFunnelRead]:
    return _service(request).list_candidate_funnel(
        source_date=source_date, limit=limit
    )


@router.post(
    "/candidate-funnel/advance",
    response_model=CandidateAdvanceQueued,
    status_code=status.HTTP_202_ACCEPTED,
)
def advance_candidate_funnel(
    payload: CandidateAdvanceCreate, request: Request
) -> CandidateAdvanceQueued:
    shop_service: ShopCollectionService | None = request.app.state.shop_service
    if shop_service is None:
        raise HTTPException(status_code=503, detail="Android shop service is unavailable.")
    try:
        candidate = _service(request).next_preflight_candidate(
            source_date=payload.source_date
        )
        queued = shop_service.enqueue(ShopCollectionCreate(
            account_user_id=candidate.user_id,
            account_name=candidate.account_name,
            collection_mode="preflight",
            device_id=payload.device_id,
        ))
    except (CandidateFunnelBlocked, CandidateFunnelExhausted) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return CandidateAdvanceQueued(
        account_user_id=candidate.user_id,
        candidate_position=candidate.candidate_position,
        job_id=queued.job_id,
    )
