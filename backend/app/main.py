"""FastAPI application factory for the local workbench."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from backend.app.api.health import router as health_router
from backend.app.api.jobs import router as jobs_router
from backend.app.db import Database
from backend.app.settings import Settings
from backend.app.services.jobs import JobService


@asynccontextmanager
async def _lifespan(app: FastAPI):
    try:
        yield
    finally:
        database: Database | None = app.state.database
        if database is not None:
            database.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="Xiaohongshu Intelligence Workbench", lifespan=_lifespan)
    app.state.settings = settings or Settings()
    database_path = app.state.settings.database_path
    assert database_path is not None
    app.state.database = None
    app.state.job_service = None
    app.state.database_error = None
    try:
        if database_path.is_dir():
            raise OSError("Configured database path is a directory.")
        app.state.database = Database(database_path)
        app.state.job_service = JobService(
            app.state.database, runtime_dir=app.state.settings.runtime_dir
        )
        app.state.job_service.recover_expired_running()
    except (OSError, SQLAlchemyError):
        app.state.database = None
        app.state.job_service = None
        app.state.database_error = "SQLite database is unavailable."
    app.include_router(health_router)
    app.include_router(jobs_router)

    @app.exception_handler(SQLAlchemyError)
    async def database_failure(request: Request, _: SQLAlchemyError) -> JSONResponse:
        request.app.state.database_error = "SQLite database is unavailable."
        return JSONResponse(status_code=503, content={"detail": "SQLite database is unavailable."})

    return app


app = create_app()
