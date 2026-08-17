"""FastAPI application factory for the local workbench."""

from fastapi import FastAPI

from backend.app.api.health import router as health_router
from backend.app.settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="Xiaohongshu Intelligence Workbench")
    app.state.settings = settings or Settings()
    app.include_router(health_router)
    return app


app = create_app()
