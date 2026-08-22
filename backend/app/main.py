"""FastAPI application factory for the local workbench."""

from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from backend.app.api.health import router as health_router
from backend.app.api.jobs import router as jobs_router
from backend.app.adapters.android_device import AndroidDeviceAdapter
from backend.app.adapters.registry import build_default_registry
from backend.app.adapters.bailian import BailianModelAdapter
from backend.app.adapters.bailian_media import (
    BailianImageGenerationAdapter,
    BailianVisionAdapter,
)
from backend.app.adapters.qianfan_playwright import (
    QianfanPlaywrightAdapter,
    persistent_qianfan_page_factory,
)
from backend.app.db import Database
from backend.app.features.analysis.api import router as analysis_router
from backend.app.features.analysis.service import AnalysisService
from backend.app.features.content.api import router as content_router
from backend.app.features.content.cleanup import (
    ArtifactCleanupService,
    ArtifactCleanupWorker,
)
from backend.app.features.content.service import ContentService
from backend.app.features.content_research.api import router as content_research_router
from backend.app.features.content_research.benchmark_api import router as benchmark_research_router
from backend.app.features.content_research.benchmark_service import BenchmarkResearchService
from backend.app.features.content_research.service import ContentResearchService
from backend.app.features.media.api import router as media_router
from backend.app.features.media.service import ContentMediaService
from backend.app.features.media.worker import ContentMediaWorker
from backend.app.features.radar.api import router as radar_router
from backend.app.features.radar.service import RadarService
from backend.app.features.radar.qianfan_service import QianfanCollectionService
from backend.app.features.shops.api import router as shops_router
from backend.app.features.shops.service import (
    ANDROID_SHOP_JOB_TYPES,
    ShopCollectionService,
)
from backend.app.features.xhs.api import router as xhs_router
from backend.app.features.xhs.service import XhsCollectionService
from backend.app.settings import Settings
from backend.app.services.jobs import JobService


@asynccontextmanager
async def _lifespan(app: FastAPI):
    cleanup_worker: ArtifactCleanupWorker | None = app.state.artifact_cleanup_worker
    media_worker: ContentMediaWorker | None = getattr(
        app.state, "content_media_worker", None
    )
    try:
        if media_worker is not None:
            media_worker.start()
        if cleanup_worker is not None:
            cleanup_worker.start()
        yield
    finally:
        media_safe = True
        if media_worker is not None:
            media_safe = media_worker.close()
        xhs_safe = True
        xhs_service: XhsCollectionService | None = app.state.xhs_collection_service
        if xhs_service is not None:
            xhs_safe = xhs_service.close()
        qianfan_service: QianfanCollectionService | None = (
            app.state.qianfan_collection_service
        )
        if qianfan_service is not None:
            qianfan_service.close()
        shop_service: ShopCollectionService | None = app.state.shop_service
        if shop_service is not None:
            shop_service.close()
        database: Database | None = app.state.database
        if cleanup_worker is not None:
            cleanup_worker.close()
        elif database is not None:
            database.close()
        if not xhs_safe:
            raise RuntimeError("XHS collection process tree did not stop safely.")
        if not media_safe:
            raise RuntimeError("Content media worker did not stop safely.")


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="Xiaohongshu Intelligence Workbench", lifespan=_lifespan)
    app.state.settings = settings or Settings()
    database_path = app.state.settings.database_path
    assert database_path is not None
    app.state.database = None
    app.state.job_service = None
    app.state.radar_service = None
    app.state.adapter_registry = None
    app.state.xhs_collection_service = None
    app.state.qianfan_collection_service = None
    app.state.android_adapter = None
    app.state.shop_service = None
    app.state.analysis_service = None
    app.state.content_service = None
    app.state.content_research_service = None
    app.state.benchmark_research_service = None
    app.state.artifact_cleanup_service = None
    app.state.artifact_cleanup_worker = None
    app.state.bailian_vision_adapter = BailianVisionAdapter(
        api_key=app.state.settings.bailian_api_key,
        base_url=app.state.settings.bailian_vision_base_url,
        model=app.state.settings.bailian_vision_model,
        max_attempts=app.state.settings.bailian_vision_max_attempts,
        timeout_seconds=app.state.settings.bailian_vision_timeout_seconds,
    )
    app.state.bailian_image_adapter = BailianImageGenerationAdapter(
        api_key=app.state.settings.bailian_api_key,
        base_url=app.state.settings.bailian_image_base_url,
        model=app.state.settings.bailian_image_model,
        max_attempts=app.state.settings.bailian_image_max_attempts,
        timeout_seconds=app.state.settings.bailian_image_timeout_seconds,
        poll_deadline_seconds=app.state.settings.bailian_image_poll_deadline_seconds,
        poll_interval_seconds=app.state.settings.bailian_image_poll_interval_seconds,
        max_image_bytes=app.state.settings.bailian_image_max_bytes,
        max_image_pixels=app.state.settings.bailian_image_max_pixels,
    )
    app.state.content_media_service = None
    app.state.content_media_worker = None
    app.state.bailian_adapter = BailianModelAdapter(
        api_key=app.state.settings.bailian_api_key,
        base_url=app.state.settings.bailian_base_url,
        model=app.state.settings.bailian_text_model,
        max_attempts=app.state.settings.bailian_max_attempts,
        timeout_seconds=app.state.settings.bailian_timeout_seconds,
    )
    app.state.database_error = None
    try:
        if database_path.is_dir():
            raise OSError("Configured database path is a directory.")
        app.state.database = Database(
            database_path, runtime_dir=app.state.settings.runtime_dir
        )
        app.state.job_service = JobService(
            app.state.database, runtime_dir=app.state.settings.runtime_dir
        )
        app.state.adapter_registry = build_default_registry(app.state.settings)
        app.state.xhs_collection_service = XhsCollectionService(
            database=app.state.database,
            job_service=app.state.job_service,
            adapter=app.state.adapter_registry.resolve("fetch_account"),
            runtime_dir=app.state.settings.runtime_dir,
            max_artifact_bytes=app.state.settings.xhs_cli_max_output_bytes,
        )
        app.state.radar_service = RadarService(
            app.state.database, job_service=app.state.job_service
        )
        page_factory = persistent_qianfan_page_factory(
            browser_executable=app.state.settings.browser_executable,
            user_data_dir=app.state.settings.qianfan_browser_profile_dir,
        )
        app.state.qianfan_collection_service = QianfanCollectionService(
            job_service=app.state.job_service,
            radar_service=app.state.radar_service,
            runtime_dir=app.state.settings.runtime_dir,
            adapter_factory=lambda: QianfanPlaywrightAdapter(
                page_factory=page_factory,
                job_service=app.state.job_service,
                runtime_dir=app.state.settings.runtime_dir,
                timeout_seconds=app.state.settings.qianfan_timeout_seconds,
                owns_page=True,
                finalize_job=False,
            ),
        )
        app.state.analysis_service = AnalysisService(
            app.state.database,
            app.state.bailian_adapter,
            runtime_dir=app.state.settings.runtime_dir,
        )
        app.state.content_research_service = ContentResearchService(
            app.state.database,
            app.state.bailian_adapter,
        )
        app.state.benchmark_research_service = BenchmarkResearchService(
            app.state.database,
            app.state.xhs_collection_service,
        )
        app.state.artifact_cleanup_service = ArtifactCleanupService(
            app.state.database,
            runtime_dir=app.state.settings.runtime_dir,
            grace_period=timedelta(
                hours=app.state.settings.artifact_cleanup_grace_hours
            ),
        )
        app.state.artifact_cleanup_service.recover_expired_leases()
        app.state.content_service = ContentService(
            app.state.database,
            app.state.bailian_adapter,
            runtime_dir=app.state.settings.runtime_dir,
            cleanup_service=app.state.artifact_cleanup_service,
        )
        app.state.content_media_service = ContentMediaService(
            app.state.database,
            content_service=app.state.content_service,
            image_adapter=app.state.bailian_image_adapter,
            vision_adapter=app.state.bailian_vision_adapter,
            runtime_dir=app.state.settings.runtime_dir,
            cleanup_service=app.state.artifact_cleanup_service,
        )
        app.state.content_media_worker = ContentMediaWorker(
            app.state.content_media_service
        )
        app.state.artifact_cleanup_worker = ArtifactCleanupWorker(
            app.state.artifact_cleanup_service,
            poll_seconds=app.state.settings.artifact_cleanup_poll_seconds,
            batch_size=app.state.settings.artifact_cleanup_batch_size,
            on_stopped=app.state.database.close,
        )
        app.state.job_service.recover_expired_running(
            worker_job_types=ANDROID_SHOP_JOB_TYPES
        )
    except (OSError, SQLAlchemyError):
        if app.state.qianfan_collection_service is not None:
            app.state.qianfan_collection_service.close()
        app.state.database = None
        app.state.job_service = None
        app.state.adapter_registry = None
        app.state.xhs_collection_service = None
        app.state.radar_service = None
        app.state.qianfan_collection_service = None
        app.state.analysis_service = None
        app.state.content_service = None
        app.state.content_research_service = None
        app.state.benchmark_research_service = None
        app.state.artifact_cleanup_service = None
        app.state.artifact_cleanup_worker = None
        app.state.content_media_service = None
        app.state.content_media_worker = None
        app.state.database_error = "SQLite database is unavailable."
    app.state.android_adapter = AndroidDeviceAdapter(
        runtime_dir=app.state.settings.runtime_dir,
        job_service=app.state.job_service,
        adb_executable=app.state.settings.adb_executable,
    )
    if app.state.job_service is not None:
        app.state.shop_service = ShopCollectionService(
            job_service=app.state.job_service,
            device_adapter=app.state.android_adapter,
            scope_model_adapter=app.state.bailian_adapter,
        )
    app.include_router(health_router)
    app.include_router(jobs_router)
    app.include_router(radar_router)
    app.include_router(shops_router)
    app.include_router(analysis_router)
    app.include_router(content_research_router)
    app.include_router(benchmark_research_router)
    app.include_router(content_router)
    app.include_router(media_router)
    app.include_router(xhs_router)

    @app.exception_handler(SQLAlchemyError)
    async def database_failure(request: Request, _: SQLAlchemyError) -> JSONResponse:
        request.app.state.database_error = "SQLite database is unavailable."
        return JSONResponse(status_code=503, content={"detail": "SQLite database is unavailable."})

    return app


app = create_app()
