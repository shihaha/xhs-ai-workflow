"""Read-only local source-repository probes for candidate Xiaohongshu adapters."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class Candidate:
    name: str
    repository_url: str
    directory_name: str
    expected_files: tuple[str, ...]


CANDIDATES = (
    Candidate(
        name="xiaohongshu-mcp",
        repository_url="https://github.com/xpzouying/xiaohongshu-mcp.git",
        directory_name="xiaohongshu-mcp",
        expected_files=("README.md", "go.mod"),
    ),
    Candidate(
        name="xhs-cli",
        repository_url="https://github.com/jackwener/xhs-cli.git",
        directory_name="xhs-cli",
        expected_files=("README.md", "pyproject.toml"),
    ),
    Candidate(
        name="MediaCrawler",
        repository_url="https://github.com/NanmiCoder/MediaCrawler.git",
        directory_name="MediaCrawler",
        expected_files=("README.md", "pyproject.toml"),
    ),
)


def _run(command: list[str], *, cwd: Path) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return {
        "command": subprocess.list2cmdline(command),
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def probe(candidate: Candidate, repository_root: Path) -> dict[str, Any]:
    """Inspect only a locally cloned source tree; never start an authenticated client."""
    repository = repository_root / candidate.directory_name
    result: dict[str, Any] = {
        "name": candidate.name,
        "repository_url": candidate.repository_url,
        "repository_path": str(repository),
        "login_dependent": True,
        "status": "unavailable",
        "origin": None,
        "checks": [],
    }
    if not repository.is_dir():
        result["reason"] = "Candidate repository is not present locally."
        return result

    if shutil.which("git") is None:
        result["reason"] = "Git executable is unavailable; source revision could not be verified."
        return result

    revision = _run(["git", "rev-parse", "--short", "HEAD"], cwd=repository)
    result["checks"].append(revision)
    origin_check = _run(["git", "remote", "get-url", "origin"], cwd=repository)
    result["checks"].append(origin_check)
    if origin_check["returncode"] == 0:
        result["origin"] = origin_check["stdout"]
    expected_files = {
        expected_file: (repository / expected_file).is_file()
        for expected_file in candidate.expected_files
    }
    result["checks"].append({"expected_files": expected_files})
    if revision["returncode"] != 0:
        result["reason"] = "Repository is present but its Git revision is not readable."
        return result
    if origin_check["returncode"] != 0:
        result["reason"] = "Repository has no readable origin remote."
        return result
    if _normalized_github_repository(origin_check["stdout"]) != _normalized_github_repository(
        candidate.repository_url
    ):
        result["reason"] = "Repository origin does not match the official candidate source."
        return result
    if not all(expected_files.values()):
        result["reason"] = "Repository is missing expected project metadata."
        return result

    result["status"] = "unverified"
    result["reason"] = (
        "Source metadata probe passed. Login-dependent collection was not run, so this "
        "candidate is not live-verified."
    )
    return result


def _normalized_github_repository(origin: str) -> str | None:
    """Return a comparable GitHub owner/repository identity for HTTPS and SSH remotes."""
    value = origin.strip()
    if value.startswith("git@github.com:"):
        path = value.removeprefix("git@github.com:")
    else:
        parsed = urlparse(value)
        try:
            port = parsed.port
        except ValueError:
            return None
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.hostname.casefold() != "github.com"
            or parsed.username is not None
            or parsed.password is not None
            or port not in (None, 443)
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            return None
        path = parsed.path.lstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if len(parts) != 2 or not all(parts):
        return None
    return "/".join(part.casefold() for part in parts)


def _selected_candidates(name: str) -> tuple[Candidate, ...]:
    if name == "all":
        return CANDIDATES
    normalized = name.casefold()
    for candidate in CANDIDATES:
        if candidate.name.casefold() == normalized:
            return (candidate,)
    raise ValueError(name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", default="all", help="Candidate name or 'all'.")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(r"D:\AI_WORKSPACE_RUNTIME\xhs-intelligence-workbench\third_party"),
        help="Directory containing locally cloned candidate repositories.",
    )
    arguments = parser.parse_args()
    try:
        candidates = _selected_candidates(arguments.candidate)
    except ValueError:
        parser.error(f"Unknown candidate: {arguments.candidate}")
    print(
        json.dumps(
            {
                "probe": "xhs-adapter-source-repository",
                "candidates": [probe(candidate, arguments.repo_root) for candidate in candidates],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
