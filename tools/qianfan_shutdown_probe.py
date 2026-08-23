"""Diagnostic probe for the timing-sensitive Qianfan child-process shutdown test.

This does not change production behavior or the existing test threshold.  It
replays the same blocked-worker setup while emitting phase timings and thread
snapshots so startup/import latency can be distinguished from shutdown latency.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time


CHILD = textwrap.dedent(
    r"""
    import json
    import threading
    import time

    started_at = time.monotonic()

    def mark(name):
        print(json.dumps({
            "mark": name,
            "child_elapsed_seconds": round(time.monotonic() - started_at, 6),
            "threads": [
                {
                    "name": thread.name,
                    "daemon": thread.daemon,
                    "alive": thread.is_alive(),
                }
                for thread in threading.enumerate()
            ],
        }, sort_keys=True), flush=True)

    mark("child_started")

    from pathlib import Path
    from threading import Event
    from backend.app.adapters.contracts import CollectionRequest
    from backend.app.db import Database
    from backend.app.features.radar.qianfan_service import QianfanCollectionCreate, QianfanCollectionService
    from backend.app.features.radar.service import RadarService
    from backend.app.services.jobs import JobService
    from backend.tests.radar.test_qianfan_orchestration import _controlled_profile

    mark("project_imports_done")

    runtime = Path(__RUNTIME__)
    database = Database(runtime / "db.sqlite3")
    jobs = JobService(database, runtime_dir=runtime)
    started = Event()

    class Blocked:
        def collect_scope(self, request: CollectionRequest, **kwargs):
            started.set()
            Event().wait()

    service = QianfanCollectionService(
        job_service=jobs,
        radar_service=RadarService(database),
        runtime_dir=runtime,
        adapter_factory=lambda: Blocked(),
        selector_profile=_controlled_profile(),
    )
    mark("service_created")

    service.enqueue(QianfanCollectionCreate(expected_count_per_scope=1))
    assert started.wait(1)
    mark("worker_started")

    close_started = time.monotonic()
    service.close()
    close_elapsed = time.monotonic() - close_started
    print(json.dumps({
        "mark": "service_closed",
        "child_elapsed_seconds": round(time.monotonic() - started_at, 6),
        "close_elapsed_seconds": round(close_elapsed, 6),
        "threads": [
            {
                "name": thread.name,
                "daemon": thread.daemon,
                "alive": thread.is_alive(),
            }
            for thread in threading.enumerate()
        ],
    }, sort_keys=True), flush=True)
    """
)


def _run_once(root: Path, attempt: int) -> dict[str, object]:
    runtime = root / f"attempt-{attempt}"
    script = CHILD.replace("__RUNTIME__", repr(str(runtime)))
    started = time.monotonic()
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        stdout, stderr = process.communicate()
    wall = time.monotonic() - started
    marks: list[dict[str, object]] = []
    for line in stdout.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "mark" in payload:
            marks.append(payload)
    return {
        "attempt": attempt,
        "returncode": process.returncode,
        "timed_out_at_5s": timed_out,
        "parent_wall_seconds": round(wall, 6),
        "marks": marks,
        "stderr_tail": stderr[-2000:],
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="qianfan-shutdown-probe-") as directory:
        root = Path(directory)
        rows = [_run_once(root, attempt) for attempt in range(1, 11)]

    for row in rows:
        print(json.dumps(row, ensure_ascii=False, sort_keys=True))

    timed_out = [row for row in rows if row["timed_out_at_5s"]]
    if timed_out:
        print(f"probe warning: {len(timed_out)}/10 children still alive after 5 seconds")
        return 1
    print("probe complete: all 10 children exited naturally within 5 seconds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
