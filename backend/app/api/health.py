"""Truthful local dependency health reporting."""

import os
from pathlib import Path
from shutil import which

from fastapi import APIRouter, Request

from backend.app.settings import Settings


router = APIRouter(prefix="/api/v1", tags=["health"])


def _is_available_executable(executable: str | None) -> bool:
    return executable is not None and which(executable) is not None


def _is_writable_database_path(database_path: Path) -> bool:
    if database_path.is_dir() or not database_path.parent.is_dir():
        return False
    if not os.access(database_path.parent, os.W_OK):
        return False
    return not database_path.exists() or os.access(database_path, os.W_OK)


@router.get("/health")
def health(request: Request) -> dict[str, object]:
    """Report current local prerequisites without probing external services."""
    settings: Settings = request.app.state.settings
    database_path = settings.database_path
    assert database_path is not None
    database_error = request.app.state.database_error

    checks = {
        "database": {
            "healthy": database_error is None and _is_writable_database_path(database_path),
            "path": str(database_path),
        },
        "adb": {
            "healthy": _is_available_executable(settings.adb_executable),
            "executable": settings.adb_executable,
        },
        "browser": {
            "healthy": _is_available_executable(settings.browser_executable),
            "executable": settings.browser_executable,
        },
        "bailian": {
            "healthy": bool(
                settings.bailian_api_key and settings.bailian_api_key.strip()
            )
        },
    }
    is_healthy = all(check["healthy"] for check in checks.values())
    return {"status": "healthy" if is_healthy else "degraded", "checks": checks}
