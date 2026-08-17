import json
from pathlib import Path
import subprocess
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROBE_SCRIPT = PROJECT_ROOT / "tools" / "probe_xhs_adapter.py"


def _initialize_repository(
    repository: Path, files: dict[str, str], *, origin: str | None = None
) -> None:
    repository.mkdir(parents=True)
    for relative_path, contents in files.items():
        (repository / relative_path).write_text(contents, encoding="utf-8")
    for command in (
        ["git", "init"],
        ["git", "add", "."],
        [
            "git",
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
    ):
        subprocess.run(command, cwd=repository, check=True, capture_output=True, text=True)
    if origin is not None:
        subprocess.run(
            ["git", "remote", "add", "origin", origin],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        )


def test_probe_marks_missing_candidate_unavailable(tmp_path: Path) -> None:
    """A missing repository must never be surfaced as a usable collection adapter."""
    completed = subprocess.run(
        [
            sys.executable,
            str(PROBE_SCRIPT),
            "--candidate",
            "xhs-cli",
            "--repo-root",
            str(tmp_path / "no-third-party-repositories"),
        ],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["candidates"][0]["name"] == "xhs-cli"
    assert result["candidates"][0]["status"] == "unavailable"


def test_probe_marks_a_metadata_complete_go_candidate_unverified(tmp_path: Path) -> None:
    """Expecting Node metadata from the Go MCP source would falsely hide an available candidate."""
    repository = tmp_path / "third-party" / "xiaohongshu-mcp"
    _initialize_repository(
        repository,
        {"README.md": "fixture", "go.mod": "module fixture"},
        origin="git@github.com:xpzouying/xiaohongshu-mcp.git",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(PROBE_SCRIPT),
            "--candidate",
            "xiaohongshu-mcp",
            "--repo-root",
            str(repository.parent),
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout)["candidates"][0]["status"] == "unverified"


def test_probe_rejects_an_xhs_cli_tree_without_its_python_project_metadata(
    tmp_path: Path,
) -> None:
    """A directory named xhs-cli with only a README must not be mistaken for the real CLI."""
    repository = tmp_path / "third-party" / "xhs-cli"
    _initialize_repository(repository, {"README.md": "fixture"})

    completed = subprocess.run(
        [
            sys.executable,
            str(PROBE_SCRIPT),
            "--candidate",
            "xhs-cli",
            "--repo-root",
            str(repository.parent),
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout)["candidates"][0]["status"] == "unavailable"


def test_probe_rejects_a_metadata_complete_candidate_without_origin(tmp_path: Path) -> None:
    """A local Git tree without an origin cannot be represented as the official MCP source."""
    repository = tmp_path / "third-party" / "xiaohongshu-mcp"
    _initialize_repository(repository, {"README.md": "fixture", "go.mod": "module fixture"})

    completed = subprocess.run(
        [
            sys.executable,
            str(PROBE_SCRIPT),
            "--candidate",
            "xiaohongshu-mcp",
            "--repo-root",
            str(repository.parent),
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    result = json.loads(completed.stdout)["candidates"][0]
    assert result["status"] == "unavailable"
    assert result["origin"] is None


def test_probe_rejects_a_candidate_with_an_unrelated_origin(tmp_path: Path) -> None:
    """Matching only the directory name lets an unrelated GitHub repository impersonate xhs-cli."""
    repository = tmp_path / "third-party" / "xhs-cli"
    _initialize_repository(
        repository,
        {"README.md": "fixture", "pyproject.toml": "[project]"},
        origin="https://github.com/unrelated/repository.git",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(PROBE_SCRIPT),
            "--candidate",
            "xhs-cli",
            "--repo-root",
            str(repository.parent),
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    result = json.loads(completed.stdout)["candidates"][0]
    assert result["status"] == "unavailable"
    assert result["origin"] == "https://github.com/unrelated/repository.git"


@pytest.mark.parametrize(
    "origin",
    [
        "file://github.com/xpzouying/xiaohongshu-mcp.git",
        "git://github.com/xpzouying/xiaohongshu-mcp.git",
        "ssh://github.com/xpzouying/xiaohongshu-mcp.git",
        "https://user:secret@github.com/xpzouying/xiaohongshu-mcp.git",
        "https://github.com:8443/xpzouying/xiaohongshu-mcp.git",
        "https://github.com/xpzouying/xiaohongshu-mcp.git?ref=main",
        "https://github.com/xpzouying/xiaohongshu-mcp.git#readme",
    ],
)
def test_probe_rejects_nonapproved_origin_forms(
    tmp_path: Path, origin: str
) -> None:
    """Accepting alternate URL schemes or URL decorations lets a non-approved remote impersonate GitHub."""
    repository = tmp_path / "third-party" / "xiaohongshu-mcp"
    _initialize_repository(
        repository,
        {"README.md": "fixture", "go.mod": "module fixture"},
        origin=origin,
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(PROBE_SCRIPT),
            "--candidate",
            "xiaohongshu-mcp",
            "--repo-root",
            str(repository.parent),
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    result = json.loads(completed.stdout)["candidates"][0]
    assert result["status"] == "unavailable"
    assert result["origin"] == origin
