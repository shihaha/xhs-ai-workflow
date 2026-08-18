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
        xhs_cli_python_executable="C:/trusted-tools/python.exe",
        xhs_cli_timeout_seconds=12.5,
    )

    assert settings.xhs_cli_python_executable == "C:/trusted-tools/python.exe"
    assert settings.xhs_cli_timeout_seconds == 12.5
    assert settings.xhs_cli_state_dir == (tmp_path / "runtime" / "xhs-cli-state").resolve()
    assert settings.xhs_cli_state_dir.is_dir()
    with pytest.raises(ValidationError):
        Settings(runtime_dir=tmp_path / "invalid", xhs_cli_timeout_seconds=0)


def test_xhs_cli_state_directory_must_be_isolated_inside_runtime(tmp_path: Path) -> None:
    """Pointing state at a normal user profile would re-enable browser-cookie discovery."""
    runtime_dir = tmp_path / "runtime"
    outside = tmp_path / "normal-user-profile"

    with pytest.raises(ValidationError, match="xhs_cli_state_dir"):
        Settings(runtime_dir=runtime_dir, xhs_cli_state_dir=outside)


def test_xhs_cli_settings_use_explicit_nonduplicated_environment_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_dir = tmp_path / "runtime"
    state_dir = runtime_dir / "prepared-xhs-state"
    monkeypatch.setenv("XHS_CLI_PYTHON_EXECUTABLE", "C:/trusted/python.exe")
    monkeypatch.setenv("XHS_CLI_TIMEOUT_SECONDS", "7.5")
    monkeypatch.setenv("XHS_CLI_STATE_DIR", str(state_dir))
    monkeypatch.setenv("XHS_CLI_MAX_OUTPUT_BYTES", "4096")

    settings = Settings(runtime_dir=runtime_dir)

    assert settings.xhs_cli_python_executable == "C:/trusted/python.exe"
    assert settings.xhs_cli_timeout_seconds == 7.5
    assert settings.xhs_cli_state_dir == state_dir.resolve()
    assert settings.xhs_cli_max_output_bytes == 4096
