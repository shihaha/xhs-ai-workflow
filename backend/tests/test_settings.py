from pathlib import Path

import pytest
from pydantic import ValidationError

import backend.app.adapters.xhs_cli_readonly_wrapper as wrapper
from backend.app.adapters.xhs_cli_read import XhsCliReadAdapter
from backend.app.settings import Settings


def test_settings_creates_the_configured_runtime_directory(tmp_path: Path) -> None:
    """Changing runtime storage must not leave later services without a directory."""
    runtime_dir = tmp_path / "runtime"

    settings = Settings(
        runtime_dir=runtime_dir,
        database_path=runtime_dir / "workbench.sqlite3",
        _env_file=None,
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
        _env_file=None,
    )

    assert settings.xhs_cli_python_executable == "C:/trusted-tools/python.exe"
    assert settings.xhs_cli_timeout_seconds == 12.5
    assert settings.xhs_cli_state_dir == (tmp_path / "runtime" / "xhs-cli-state").resolve()
    assert settings.xhs_cli_state_dir.is_dir()
    with pytest.raises(ValidationError):
        Settings(
            runtime_dir=tmp_path / "invalid",
            xhs_cli_timeout_seconds=0,
            _env_file=None,
        )


def test_default_xhs_cli_budget_exceeds_the_initial_read_and_bounded_scroll_window(
    tmp_path: Path,
) -> None:
    """The child process must survive the trusted initial read plus all fixed scroll waits."""
    runtime_dir = tmp_path / "runtime"
    settings = Settings(
        runtime_dir=runtime_dir,
        xhs_cli_state_dir=runtime_dir / "xhs-cli-state",
        _env_file=None,
    )
    adapter = XhsCliReadAdapter.from_settings(settings)
    user_posts_goto_budget_seconds = 20.0
    user_posts_start_wait_budget_seconds = 3.0
    user_posts_data_wait_budget_seconds = 15.0
    bounded_scroll_wait_seconds = (
        wrapper._USER_POSTS_MAX_SCROLL_ATTEMPTS
        * wrapper._USER_POSTS_SCROLL_WAIT_MS
        / 1000
    )
    startup_and_parse_margin_seconds = 10.0
    worst_case_budget_seconds = (
        user_posts_goto_budget_seconds
        + user_posts_start_wait_budget_seconds
        + user_posts_data_wait_budget_seconds
        + bounded_scroll_wait_seconds
    )

    assert settings.xhs_cli_timeout_seconds == 120.0
    assert adapter._timeout_seconds == settings.xhs_cli_timeout_seconds
    assert adapter._timeout_seconds >= (
        worst_case_budget_seconds + startup_and_parse_margin_seconds
    )
    assert adapter._timeout_seconds <= 120.0


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


@pytest.mark.parametrize(
    "endpoint",
    (
        "http://192.0.2.10:9223",
        "https://127.0.0.1:9223",
        "http://user:pass@127.0.0.1:9223",
        "http://127.0.0.1:9223/json/version",
    ),
)
def test_xhs_cdp_endpoint_is_an_explicit_local_browser_boundary(
    tmp_path: Path, endpoint: str
) -> None:
    """A remote or ambiguous endpoint could expose the trusted browser session."""
    with pytest.raises(ValidationError, match="xhs_cdp_endpoint"):
        Settings(
            runtime_dir=tmp_path / "runtime",
            xhs_cdp_endpoint=endpoint,
            _env_file=None,
        )


def test_bailian_text_vision_and_image_settings_are_independent_and_bounded(
    tmp_path: Path,
) -> None:
    """Changing one media capability must not silently redirect the other two."""
    settings = Settings(
        runtime_dir=tmp_path / "runtime",
        bailian_text_model="text-model",
        bailian_vision_model="vision-model",
        bailian_image_model="image-model",
        bailian_base_url="https://text.example/v1",
        bailian_vision_base_url="https://vision.example/v1",
        bailian_image_base_url="https://image.example/api/v1",
        bailian_vision_timeout_seconds=11,
        bailian_image_timeout_seconds=22,
        _env_file=None,
    )

    assert settings.bailian_text_model == "text-model"
    assert settings.bailian_vision_model == "vision-model"
    assert settings.bailian_image_model == "image-model"
    assert settings.bailian_base_url == "https://text.example/v1"
    assert settings.bailian_vision_base_url == "https://vision.example/v1"
    assert settings.bailian_image_base_url == "https://image.example/api/v1"
    assert settings.bailian_vision_timeout_seconds == 11
    assert settings.bailian_image_timeout_seconds == 22

    with pytest.raises(ValidationError):
        Settings(
            runtime_dir=tmp_path / "invalid",
            bailian_image_max_bytes=0,
            _env_file=None,
        )
