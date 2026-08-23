from __future__ import annotations

from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
import textwrap
from threading import Thread
import time

import pytest


def _read_one_line(stream, queue: Queue[str]) -> None:
    queue.put(stream.readline())


def test_blocked_browser_worker_exits_within_two_seconds_after_ready(tmp_path: Path) -> None:
    """Measure shutdown from a ready worker, not from Python process creation.

    The historical regression test starts its two-second deadline at Popen(), so
    import/SQLite/service setup latency is mixed into the shutdown assertion.
    This handshake keeps the same strict two-second shutdown budget while giving
    startup a separate bounded readiness phase.
    """

    runtime = tmp_path / "child-runtime"
    script = textwrap.dedent(
        f"""
        import sys
        from pathlib import Path
        from threading import Event
        from backend.app.adapters.contracts import CollectionRequest
        from backend.app.db import Database
        from backend.app.features.radar.qianfan_service import QianfanCollectionCreate, QianfanCollectionService
        from backend.app.features.radar.service import RadarService
        from backend.app.services.jobs import JobService
        from backend.tests.radar.test_qianfan_orchestration import _controlled_profile

        runtime = Path({str(runtime)!r})
        database = Database(runtime / 'db.sqlite3')
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
        service.enqueue(QianfanCollectionCreate(expected_count_per_scope=1))
        assert started.wait(1)

        print('worker-ready', flush=True)
        command = sys.stdin.readline().strip()
        assert command == 'close'

        service.close()
        print('lifespan-returned', flush=True)
        """
    )

    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[3],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    ready_lines: Queue[str] = Queue(maxsize=1)
    reader = Thread(
        target=_read_one_line,
        args=(process.stdout, ready_lines),
        name="qianfan-test-ready-reader",
        daemon=True,
    )
    reader.start()

    try:
        ready = ready_lines.get(timeout=10)
    except Empty:
        process.kill()
        process.wait(timeout=2)
        stderr = process.stderr.read()
        pytest.fail(f"Qianfan child did not become ready within 10s; stderr={stderr!r}")

    assert ready.strip() == "worker-ready"

    shutdown_started = time.monotonic()
    process.stdin.write("close\n")
    process.stdin.flush()
    process.stdin.close()

    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)
        remaining_stdout = process.stdout.read()
        stderr = process.stderr.read()
        pytest.fail(
            "blocked Qianfan worker kept Python alive for more than two seconds "
            f"after worker-ready; stdout={remaining_stdout!r}, stderr={stderr!r}"
        )

    shutdown_elapsed = time.monotonic() - shutdown_started
    remaining_stdout = process.stdout.read()
    stderr = process.stderr.read()

    assert process.returncode == 0, stderr
    assert "lifespan-returned" in remaining_stdout
    assert shutdown_elapsed < 2
