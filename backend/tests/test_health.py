from pathlib import Path

import httpx
import pytest

from backend.app.main import create_app
from backend.app.settings import Settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_health_reports_actual_unavailable_external_dependencies(
    tmp_path: Path,
) -> None:
    """A missing tool or credential must make health degraded, never healthy by default."""
    runtime_dir = tmp_path / "runtime"
    settings = Settings(
        runtime_dir=runtime_dir,
        database_path=runtime_dir / "workbench.sqlite3",
        adb_executable="definitely-not-an-adb-executable",
        browser_executable="definitely-not-a-browser-executable",
        bailian_api_key=None,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings)),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "degraded",
        "checks": {
            "database": {
                "healthy": True,
                "path": str(runtime_dir / "workbench.sqlite3"),
            },
            "adb": {
                "healthy": False,
                "executable": "definitely-not-an-adb-executable",
            },
            "browser": {
                "healthy": False,
                "executable": "definitely-not-a-browser-executable",
            },
            "bailian": {"healthy": False},
        },
    }


@pytest.mark.anyio
async def test_health_reports_configured_bailian_without_exposing_its_key(
    tmp_path: Path,
) -> None:
    """A configured credential must be visible as configuration state, but never as a secret."""
    runtime_dir = tmp_path / "runtime"
    settings = Settings(
        runtime_dir=runtime_dir,
        adb_executable="definitely-not-an-adb-executable",
        browser_executable="definitely-not-a-browser-executable",
        bailian_api_key="test-secret",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings)),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/api/v1/health")

    assert response.json()["checks"]["bailian"] == {"healthy": True}
    assert "test-secret" not in response.text


@pytest.mark.anyio
async def test_health_reports_whitespace_bailian_key_as_unconfigured(
    tmp_path: Path,
) -> None:
    """Whitespace is not a usable API credential and must not produce a healthy check."""
    settings = Settings(runtime_dir=tmp_path / "runtime", bailian_api_key="   ")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings)),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/api/v1/health")

    assert response.json()["checks"]["bailian"] == {"healthy": False}
