"""Configuration for the local workbench process."""

import os
import sys
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
        populate_by_name=True,
        extra="ignore",
    )

    runtime_dir: Path = DEFAULT_RUNTIME_DIR
    database_path: Path | None = None
    adb_executable: str = "adb"
    browser_executable: str | None = None
    qianfan_browser_profile_dir: Path | None = None
    qianfan_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    xhs_cli_python_executable: str = Field(
        default=sys.executable,
        min_length=1,
        validation_alias="XHS_CLI_PYTHON_EXECUTABLE",
    )
    xhs_cli_timeout_seconds: float = Field(
        default=60.0,
        gt=0,
        le=120,
        validation_alias="XHS_CLI_TIMEOUT_SECONDS",
    )
    xhs_cli_state_dir: Path | None = Field(
        default=None, validation_alias="XHS_CLI_STATE_DIR"
    )
    xhs_cli_max_output_bytes: int = Field(
        default=5 * 1024 * 1024,
        ge=1024,
        le=20 * 1024 * 1024,
        validation_alias="XHS_CLI_MAX_OUTPUT_BYTES",
    )
    bailian_api_key: str | None = Field(
        default=None,
        validation_alias="BAILIAN_API_KEY",
        repr=False,
    )
    bailian_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    bailian_text_model: str = "deepseek-v4-flash"
    bailian_max_attempts: int = Field(default=3, ge=1, le=10)
    bailian_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    bailian_vision_base_url: str = (
        "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    bailian_vision_model: str = "qwen-vl-max"
    bailian_vision_max_attempts: int = Field(default=3, ge=1, le=10)
    bailian_vision_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    bailian_image_base_url: str = "https://dashscope.aliyuncs.com/api/v1"
    bailian_image_model: str = "wan2.6-t2i"
    bailian_image_max_attempts: int = Field(default=3, ge=1, le=10)
    bailian_image_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    bailian_image_poll_deadline_seconds: float = Field(default=300.0, gt=0, le=1800)
    bailian_image_poll_interval_seconds: float = Field(default=3.0, ge=0, le=30)
    bailian_image_max_bytes: int = Field(
        default=20 * 1024 * 1024, ge=1024, le=50 * 1024 * 1024
    )
    bailian_image_max_pixels: int = Field(
        default=16_777_216, ge=1, le=67_108_864
    )
    artifact_cleanup_poll_seconds: float = Field(
        default=30.0, ge=1.0, le=3600.0
    )
    artifact_cleanup_batch_size: int = Field(default=10, ge=1, le=100)
    artifact_cleanup_grace_hours: int = Field(default=24, ge=1, le=168)

    @model_validator(mode="after")
    def prepare_runtime_dir(self) -> "Settings":
        self.runtime_dir = Path(os.path.abspath(self.runtime_dir))
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        if self.database_path is None:
            self.database_path = self.runtime_dir / "workbench.sqlite3"
        state_dir = (
            Path(os.path.abspath(self.xhs_cli_state_dir))
            if self.xhs_cli_state_dir is not None
            else self.runtime_dir / "xhs-cli-state"
        )
        try:
            state_dir.relative_to(self.runtime_dir)
        except ValueError as error:
            raise ValueError(
                "xhs_cli_state_dir must be isolated inside runtime_dir."
            ) from error
        state_dir.mkdir(parents=True, exist_ok=True)
        self.xhs_cli_state_dir = state_dir
        return self
