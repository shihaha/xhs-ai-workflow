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
    qianfan_browser_profile_dir: Path | None = None
    qianfan_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    bailian_api_key: str | None = Field(
        default=None,
        validation_alias="BAILIAN_API_KEY",
        repr=False,
    )
    bailian_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    bailian_text_model: str = "deepseek-v4-flash"
    bailian_max_attempts: int = Field(default=3, ge=1, le=10)
    bailian_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    artifact_cleanup_poll_seconds: float = Field(
        default=30.0, ge=1.0, le=3600.0
    )
    artifact_cleanup_batch_size: int = Field(default=10, ge=1, le=100)
    artifact_cleanup_grace_hours: int = Field(default=24, ge=1, le=168)

    @model_validator(mode="after")
    def prepare_runtime_dir(self) -> "Settings":
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        if self.database_path is None:
            self.database_path = self.runtime_dir / "workbench.sqlite3"
        return self
