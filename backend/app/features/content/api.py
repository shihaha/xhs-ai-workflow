"""HTTP routes for products, content review and deterministic packages."""

from fastapi import APIRouter, HTTPException, Request, status

from backend.app.features.content.schemas import (
    ArtifactCleanupRead,
    ContentItemCreate, ContentItemRead, ContentPackageRead, MaterialCreate, MaterialRead,
    ExportCreate, ProductCreate, ProductRead, RegenerateCreate, ReviewCreate,
)
from backend.app.features.content.cleanup import ArtifactCleanupService
from backend.app.features.content.service import (
    ContentModelFailure, ContentModelUnavailable, ContentNotFound, ContentService,
    ContentStateError, ContentValidationError,
)


router = APIRouter(prefix="/api/v1", tags=["content"])


def _service(request: Request) -> ContentService:
    service: ContentService | None = request.app.state.content_service
    if service is None:
        raise HTTPException(status_code=503, detail="SQLite database is unavailable.")
    return service


def _cleanup_service(request: Request) -> ArtifactCleanupService:
    service: ArtifactCleanupService | None = (
        request.app.state.artifact_cleanup_service
    )
    if service is None:
        raise HTTPException(status_code=503, detail="SQLite database is unavailable.")
    return service


def _translate(error: Exception) -> HTTPException:
    if isinstance(error, ContentModelUnavailable):
        return HTTPException(status_code=503, detail=str(error))
    if isinstance(error, ContentModelFailure):
        return HTTPException(status_code=502, detail=str(error))
    if isinstance(error, ContentNotFound):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, ContentStateError):
        return HTTPException(status_code=409, detail=str(error))
    return HTTPException(status_code=422, detail=str(error))


@router.post("/products", response_model=ProductRead, status_code=status.HTTP_201_CREATED)
def create_product(payload: ProductCreate, request: Request) -> ProductRead:
    try:
        return _service(request).create_product(payload)
    except (ContentNotFound, ContentValidationError, ContentStateError, ContentModelUnavailable, ContentModelFailure) as error:
        raise _translate(error) from error


@router.get("/products", response_model=list[ProductRead])
def list_products(request: Request) -> list[ProductRead]:
    return _service(request).list_products()


@router.get("/products/{product_id}", response_model=ProductRead)
def get_product(product_id: str, request: Request) -> ProductRead:
    try:
        return _service(request).get_product(product_id)
    except (ContentNotFound, ContentStateError) as error:
        raise _translate(error) from error


@router.post("/products/{product_id}/materials", response_model=MaterialRead, status_code=201)
def add_material(product_id: str, payload: MaterialCreate, request: Request) -> MaterialRead:
    try:
        return _service(request).add_material(product_id, payload)
    except (ContentNotFound, ContentValidationError, ContentStateError) as error:
        raise _translate(error) from error


@router.post("/content-items", response_model=ContentItemRead, status_code=201)
def create_content_item(payload: ContentItemCreate, request: Request) -> ContentItemRead:
    service = _service(request)
    try:
        return service.create_content_item(payload)
    except (ContentNotFound, ContentValidationError, ContentStateError, ContentModelUnavailable, ContentModelFailure) as error:
        raise _translate(error) from error


@router.get("/content-items", response_model=list[ContentItemRead])
def list_content_items(request: Request) -> list[ContentItemRead]:
    return _service(request).list_content_items()


@router.get("/content-items/{item_id}", response_model=ContentItemRead)
def get_content_item(item_id: str, request: Request) -> ContentItemRead:
    try:
        return _service(request).get_content_item(item_id)
    except ContentNotFound as error:
        raise _translate(error) from error


@router.post("/content-items/{item_id}/reviews", response_model=ContentItemRead)
def review(item_id: str, payload: ReviewCreate, request: Request) -> ContentItemRead:
    try:
        return _service(request).review(item_id, payload)
    except (ContentNotFound, ContentValidationError, ContentStateError) as error:
        raise _translate(error) from error


@router.post("/content-items/{item_id}/regenerate", response_model=ContentItemRead)
def regenerate(item_id: str, payload: RegenerateCreate, request: Request) -> ContentItemRead:
    try:
        return _service(request).regenerate(item_id, payload)
    except (ContentNotFound, ContentValidationError, ContentStateError, ContentModelUnavailable, ContentModelFailure) as error:
        raise _translate(error) from error


@router.post("/content-items/{item_id}/export", response_model=ContentPackageRead, status_code=201)
def export(item_id: str, payload: ExportCreate, request: Request) -> ContentPackageRead:
    try:
        return _service(request).export_package(item_id, payload)
    except (ContentNotFound, ContentValidationError, ContentStateError) as error:
        raise _translate(error) from error


@router.get("/content-packages", response_model=list[ContentPackageRead])
def list_packages(request: Request) -> list[ContentPackageRead]:
    return _service(request).list_packages()


@router.get("/content-packages/{package_id}", response_model=ContentPackageRead)
def get_package(package_id: str, request: Request) -> ContentPackageRead:
    try:
        return _service(request).get_package(package_id)
    except (ContentNotFound, ContentStateError) as error:
        raise _translate(error) from error


@router.get("/artifact-cleanups", response_model=list[ArtifactCleanupRead])
def list_artifact_cleanups(request: Request) -> list[ArtifactCleanupRead]:
    return _cleanup_service(request).list_records()


@router.get("/artifact-cleanups/{cleanup_id}", response_model=ArtifactCleanupRead)
def get_artifact_cleanup(cleanup_id: str, request: Request) -> ArtifactCleanupRead:
    record = _cleanup_service(request).get_record(cleanup_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail="Artifact cleanup record does not exist.",
        )
    return record
