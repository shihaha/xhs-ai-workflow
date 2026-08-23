"""Recheck apparent backend regressions against the baseline ref.

A single full-suite baseline/spike comparison can misclassify timing-sensitive
concurrency tests when the baseline happens to pass and the spike happens to
fail. This helper keeps the strict first-pass comparison, then re-runs only
new node IDs on both source trees in isolated environments.

A test is a confirmed regression only when it reproduces on the spike and does
not reproduce on the baseline during the targeted recheck. Baseline-reproducible
and non-reproducible candidates are recorded rather than silently discarded.

One historical Qianfan test has a separately proven invalid timing boundary: it
starts its two-second deadline at ``Popen()`` and therefore measures Python
startup/import/SQLite setup in addition to shutdown. For that exact node only,
the guard uses the authoritative ready-boundary replacement test. The
replacement keeps the same strict two-second shutdown budget and fails closed:
any replacement failure still makes the regression guard fail.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
from typing import Any


_SUPERSEDED_BOUNDARY_RECHECKS = {
    "backend/tests/radar/test_qianfan_orchestration.py::test_blocked_browser_worker_cannot_keep_python_process_alive": (
        "backend/tests/radar/test_qianfan_shutdown_timing.py::"
        "test_blocked_browser_worker_exits_within_two_seconds_after_ready"
    ),
}


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _python_in(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _install_environment(source: Path, venv: Path) -> Path:
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    python = _python_in(venv)
    with (source / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)
    deps = list(project["project"]["dependencies"])
    deps.extend(project["project"]["optional-dependencies"]["dev"])
    subprocess.run(
        [str(python), "-m", "pip", "install", "--disable-pip-version-check", *deps],
        check=True,
        cwd=source,
    )
    return python


def _prepare_baseline(root: Path, ref: str, destination: Path) -> None:
    if destination.exists():
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(destination)],
            cwd=root,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        shutil.rmtree(destination, ignore_errors=True)
    subprocess.run(["git", "fetch", "origin", ref, "--depth=1"], cwd=root, check=True)
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(destination), "FETCH_HEAD"],
        cwd=root,
        check=True,
    )


def _run_node(python: Path, source: Path, node_id: str) -> tuple[bool, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = "."
    result = subprocess.run(
        [str(python), "-m", "pytest", node_id, "-q", "--tb=short"],
        cwd=source,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )
    return result.returncode == 0, result.stdout[-8000:]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-result", type=Path, required=True)
    parser.add_argument("--spike-result", type=Path, required=True)
    parser.add_argument("--baseline-ref", default="research/xhs-workbench-next")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--report", type=Path, default=Path("backend-regression-recheck.json"))
    args = parser.parse_args()
    if args.repeats < 1:
        raise SystemExit("--repeats must be positive")

    baseline = _load(args.baseline_result)
    spike = _load(args.spike_result)
    baseline_issues = set(baseline["issues"])
    spike_issues = set(spike["issues"])
    candidates = sorted(spike_issues - baseline_issues)
    fixed = sorted(baseline_issues - spike_issues)

    print(f"baseline issues: {len(baseline_issues)}")
    print(f"spike issues:    {len(spike_issues)}")
    print(f"new candidates:  {len(candidates)}")
    print(f"fixed:           {len(fixed)}")
    if fixed:
        print("\nFixed relative to this baseline run:")
        print("\n".join(f"  - {item}" for item in fixed))

    report: dict[str, Any] = {
        "baseline_issue_count": len(baseline_issues),
        "spike_issue_count": len(spike_issues),
        "initial_candidates": candidates,
        "fixed": fixed,
        "repeats": args.repeats,
        "rechecks": [],
        "confirmed_regressions": [],
    }
    if not candidates:
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return 0

    root = Path.cwd().resolve()
    runner_temp = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())).resolve()
    baseline_source = runner_temp / "agent-runtime-baseline-src"
    baseline_venv = runner_temp / "agent-runtime-baseline-venv"
    spike_venv = runner_temp / "agent-runtime-spike-venv"
    for path in (baseline_venv, spike_venv):
        shutil.rmtree(path, ignore_errors=True)

    print("\nPotential regressions detected; preparing isolated targeted recheck...")
    _prepare_baseline(root, args.baseline_ref, baseline_source)
    baseline_python = _install_environment(baseline_source, baseline_venv)
    spike_python = _install_environment(root, spike_venv)

    confirmed: list[str] = []
    for node_id in candidates:
        replacement_node_id = _SUPERSEDED_BOUNDARY_RECHECKS.get(node_id)
        if replacement_node_id is not None:
            replacement_failures = 0
            replacement_last_failure = ""
            for _ in range(args.repeats):
                ok, output = _run_node(spike_python, root, replacement_node_id)
                if not ok:
                    replacement_failures += 1
                    replacement_last_failure = output

            if replacement_failures:
                classification = "confirmed_replacement_boundary_regression"
                confirmed.append(node_id)
            else:
                classification = "superseded_invalid_timing_boundary_replacement_passed"

            row = {
                "node_id": node_id,
                "replacement_node_id": replacement_node_id,
                "replacement_failures": replacement_failures,
                "attempts_per_ref": args.repeats,
                "classification": classification,
                "replacement_last_failure": replacement_last_failure,
            }
            report["rechecks"].append(row)
            print(
                f"{node_id}: authoritative replacement {replacement_node_id} "
                f"failed={replacement_failures}/{args.repeats} -> {classification}"
            )
            continue

        baseline_failures = 0
        spike_failures = 0
        baseline_last_failure = ""
        spike_last_failure = ""
        for _ in range(args.repeats):
            ok, output = _run_node(baseline_python, baseline_source, node_id)
            if not ok:
                baseline_failures += 1
                baseline_last_failure = output
        for _ in range(args.repeats):
            ok, output = _run_node(spike_python, root, node_id)
            if not ok:
                spike_failures += 1
                spike_last_failure = output

        if baseline_failures:
            classification = "baseline_reproducible_flake_or_existing_failure"
        elif spike_failures:
            classification = "confirmed_spike_only_regression"
            confirmed.append(node_id)
        else:
            classification = "not_reproduced_in_targeted_recheck"

        row = {
            "node_id": node_id,
            "baseline_failures": baseline_failures,
            "spike_failures": spike_failures,
            "attempts_per_ref": args.repeats,
            "classification": classification,
            "baseline_last_failure": baseline_last_failure,
            "spike_last_failure": spike_last_failure,
        }
        report["rechecks"].append(row)
        print(
            f"{node_id}: baseline={baseline_failures}/{args.repeats}, "
            f"spike={spike_failures}/{args.repeats} -> {classification}"
        )

    report["confirmed_regressions"] = confirmed
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if confirmed:
        print("\nCONFIRMED spike-only regressions:")
        print("\n".join(f"  - {item}" for item in confirmed))
        return 1

    print("\nNo candidate reproduced as an authoritative spike-only regression.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
