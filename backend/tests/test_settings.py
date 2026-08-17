from pathlib import Path

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
