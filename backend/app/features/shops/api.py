"""HTTP boundary for Android devices and evidence-backed shop collection."""

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from backend.app.adapters.contracts import DeviceHealth
from backend.app.features.shops.service import (
    ShopCollectionCreate,
    ShopCollectionRead,
    ShopCollectionService,
)


router = APIRouter(prefix="/api/v1", tags=["shops"])


def _adapter(request: Request) -> Any:
    adapter = request.app.state.android_adapter
    if adapter is None:
        raise HTTPException(status_code=503, detail="Android adapter is unavailable.")
    return adapter


def _service(request: Request) -> ShopCollectionService:
    service: ShopCollectionService | None = request.app.state.shop_service
    if service is None:
        raise HTTPException(status_code=503, detail="SQLite database is unavailable.")
    return service


@router.get("/devices", response_model=list[DeviceHealth])
def list_devices(request: Request) -> list[DeviceHealth]:
    """Return the current selected-device fact, including unavailable state."""
    return [_adapter(request).health()]


@router.post(
    "/shop-collections",
    response_model=ShopCollectionRead,
    status_code=status.HTTP_201_CREATED,
)
def collect_shop(
    payload: ShopCollectionCreate, request: Request
) -> ShopCollectionRead:
    return _service(request).collect(payload)
