from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.app.settings import Settings


def test_settings_creates_the_configured_runtime_directory(tmp_path: Path) -> None:
    """Changing runtime storage must not leave later services without a directory."""
    runtime_dir = tmp_path / "runtime"

    settings = Settings(
        runtime_dir=runtime_dir,
        database_path=runtime_dir / "workbench.sqlite3",
    )

    assert settings.runtime_dir == runtime_dir
    assert runtime_dir.is_dir()
    assert settings.database_path == runtime_dir / "workbench.sqlite3"


def test_xhs_cli_settings_are_trusted_and_bounded(tmp_path: Path) -> None:
    """A missing bound would allow a stuck external CLI to hold collection workers forever."""
    settings = Settings(
        runtime_dir=tmp_path / "runtime",
        xhs_cli_executable="C:/trusted-tools/xhs.exe",
        xhs_cli_timeout_seconds=12.5,
    )

    assert settings.xhs_cli_executable == "C:/trusted-tools/xhs.exe"
    assert settings.xhs_cli_timeout_seconds == 12.5
    with pytest.raises(ValidationError):
        Settings(runtime_dir=tmp_path / "invalid", xhs_cli_timeout_seconds=0)
