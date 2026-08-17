"""Configuration for the local workbench process."""

from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_RUNTIME_DIR = Path(r"D:\AI_WORKSPACE_RUNTIME\xhs-intelligence-workbench")


class Settings(BaseSettings):
    """Environment-backed settings with local runtime storage prepared on startup."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="XHS_",
        env_ignore_empty=True,
        extra="ignore",
    )

    runtime_dir: Path = DEFAULT_RUNTIME_DIR
    database_path: Path | None = None
    adb_executable: str = "adb"
    browser_executable: str | None = None
    bailian_api_key: str | None = Field(
        default=None,
        validation_alias="BAILIAN_API_KEY",
        repr=False,
    )

    @model_validator(mode="after")
    def prepare_runtime_dir(self) -> "Settings":
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        if self.database_path is None:
            self.database_path = self.runtime_dir / "workbench.sqlite3"
        return self
