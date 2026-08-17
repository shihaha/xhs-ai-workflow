import json
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROBE_SCRIPT = PROJECT_ROOT / "tools" / "probe_xhs_adapter.py"


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
    repository.mkdir(parents=True)
    (repository / "README.md").write_text("fixture", encoding="utf-8")
    (repository / "go.mod").write_text("module fixture", encoding="utf-8")
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
    repository.mkdir(parents=True)
    (repository / "README.md").write_text("fixture", encoding="utf-8")
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
