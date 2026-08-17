"""FastAPI application factory for the local workbench."""

from fastapi import FastAPI

from backend.app.api.health import router as health_router
from backend.app.api.jobs import router as jobs_router
from backend.app.db import Database
from backend.app.settings import Settings
from backend.app.services.jobs import JobService


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="Xiaohongshu Intelligence Workbench")
    app.state.settings = settings or Settings()
    database_path = app.state.settings.database_path
    assert database_path is not None
    app.state.database = None
    app.state.job_service = None
    if not database_path.is_dir():
        app.state.database = Database(database_path)
        app.state.job_service = JobService(app.state.database)
        app.state.job_service.recover_expired_running()
    app.include_router(health_router)
    app.include_router(jobs_router)
    return app


app = create_app()
