from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import os
from pathlib import Path
import subprocess
import sys
from threading import Event
import textwrap
import time
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError

from backend.app.db import Database
import backend.app.features.content.cleanup as cleanup_module
import backend.app.features.content.export as export_module
from backend.app.features.content.cleanup import (
    ArtifactCleanupCandidate,
    ArtifactCleanupService,
    ArtifactCleanupShutdownUnsafe,
    ArtifactCleanupWorker,
)
from backend.app.features.content.models import ArtifactCleanupRecord
from backend.app.main import create_app
from backend.app.settings import Settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _ProbeService:
    def __init__(self) -> None:
        self.called = Event()
        self.calls: list[int] = []

    def run_due_once(self, *, limit: int = 10, cancelled=lambda: False) -> int:
        self.calls.append(limit)
        self.called.set()
        return 0


def test_cleanup_settings_have_bounded_defaults_and_validation(tmp_path: Path) -> None:
    """Unbounded polling, batches, or grace windows would break worker safety."""
    settings = Settings(runtime_dir=tmp_path / "runtime")
    assert settings.artifact_cleanup_poll_seconds == 30.0
    assert settings.artifact_cleanup_batch_size == 10
    assert settings.artifact_cleanup_grace_hours == 24

    for field, value in (
        ("artifact_cleanup_poll_seconds", 0),
        ("artifact_cleanup_poll_seconds", 3601),
        ("artifact_cleanup_batch_size", 0),
        ("artifact_cleanup_batch_size", 101),
        ("artifact_cleanup_grace_hours", 0),
        ("artifact_cleanup_grace_hours", 169),
    ):
        with pytest.raises(ValidationError):
            Settings(runtime_dir=tmp_path / field / str(value), **{field: value})


def test_worker_processes_one_configured_batch_then_waits() -> None:
    """Ignoring the configured limit could claim an unbounded cleanup backlog."""
    service = _ProbeService()
    worker = ArtifactCleanupWorker(
        service, poll_seconds=5.0, batch_size=7
    )
    worker.start()
    try:
        assert service.called.wait(timeout=0.5)
        time.sleep(0.05)
        assert service.calls == [7]
    finally:
        worker.close()


def test_worker_shutdown_interrupts_poll_wait_under_one_second() -> None:
    """A normal 30-second poll sleep must not delay application shutdown."""
    service = _ProbeService()
    worker = ArtifactCleanupWorker(
        service, poll_seconds=30.0, batch_size=1
    )
    worker.start()
    assert service.called.wait(timeout=0.5)

    started = time.monotonic()
    worker.close()

    assert time.monotonic() - started < 1.0
    assert worker.is_alive is False


def test_worker_shutdown_is_bounded_when_processing_is_blocked() -> None:
    """A stuck filesystem operation must not hang FastAPI shutdown forever."""
    entered = Event()
    release = Event()

    class BlockedService:
        def run_due_once(self, *, limit: int = 10, cancelled=lambda: False) -> int:
            entered.set()
            release.wait()
            return 0

    worker = ArtifactCleanupWorker(
        BlockedService(), poll_seconds=30.0, batch_size=1
    )
    worker.start()
    assert entered.wait(timeout=0.5)
    started = time.monotonic()
    try:
        worker.close()
        assert time.monotonic() - started < 1.0
    finally:
        release.set()


def test_permanently_blocked_worker_does_not_prevent_process_exit_after_lifespan(
    tmp_path: Path,
) -> None:
    """A cleanup call that never returns must not keep Python alive after shutdown."""
    marker = tmp_path / "atexit-ran.txt"
    runtime = tmp_path / "child-runtime"
    child = textwrap.dedent(
        """
        import asyncio
        import atexit
        import os
        from pathlib import Path
        from threading import Event, enumerate as enumerate_threads

        from backend.app.main import create_app
        from backend.app.settings import Settings

        marker = Path(os.environ["CLEANUP_EXIT_MARKER"])
        runtime = Path(os.environ["CLEANUP_EXIT_RUNTIME"])
        atexit.register(marker.write_text, "closed", encoding="utf-8")
        app = create_app(
            Settings(
                runtime_dir=runtime,
                database_path=runtime / "workbench.sqlite3",
                xhs_cli_state_dir=runtime / "xhs-cli-state",
            )
        )
        entered = Event()
        never_release = Event()

        def blocked_batch(*, limit=10, cancelled=lambda: False):
            entered.set()
            never_release.wait()
            return 0

        app.state.artifact_cleanup_service.run_due_once = blocked_batch

        async def exercise_lifespan():
            async with app.router.lifespan_context(app):
                assert entered.wait(timeout=0.5)
                print("READY", flush=True)

        asyncio.run(exercise_lifespan())
        worker = app.state.artifact_cleanup_worker
        assert worker.close() is False
        assert worker.is_alive is True
        assert not any(
            thread.name == "artifact-cleanup" and not thread.daemon
            for thread in enumerate_threads()
        )
        """
    )
    environment = os.environ.copy()
    environment["CLEANUP_EXIT_MARKER"] = str(marker)
    environment["CLEANUP_EXIT_RUNTIME"] = str(runtime)
    process = subprocess.Popen(
        [sys.executable, "-c", child],
        cwd=Path(__file__).parents[3],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    ready = process.stdout.readline().strip()
    assert ready == "READY"

    started = time.monotonic()
    try:
        stdout, stderr = process.communicate(timeout=1.5)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        pytest.fail(
            "permanently blocked cleanup thread prevented natural process exit; "
            f"stdout={stdout!r}, stderr={stderr!r}"
        )

    assert time.monotonic() - started < 1.5
    assert process.returncode == 0, stderr
    assert marker.read_text(encoding="utf-8") == "closed"


def test_normal_daemon_worker_closes_database_finalizer_exactly_once() -> None:
    """Daemon exit safety must retain normal exact-once resource finalization."""
    service = _ProbeService()
    finalizer_calls: list[str] = []
    worker = ArtifactCleanupWorker(
        service,
        poll_seconds=30,
        batch_size=1,
        on_stopped=lambda: finalizer_calls.append("database_closed"),
    )
    worker.start()
    assert service.called.wait(timeout=0.5)
    try:
        worker_thread = next(
            thread
            for thread in __import__("threading").enumerate()
            if thread.name == "artifact-cleanup"
        )
        assert worker_thread.daemon is True
    finally:
        assert worker.close() is True
    assert worker.wait_stopped(timeout=1)
    assert finalizer_calls == ["database_closed"]
    assert worker.close() is True
    assert finalizer_calls == ["database_closed"]


def test_worker_sanitizes_fault_and_continues_next_poll() -> None:
    """One failed batch must neither leak its exception text nor kill future cleanup."""
    succeeded = Event()

    class FlakyService:
        def __init__(self) -> None:
            self.calls = 0

        def run_due_once(self, *, limit: int = 10, cancelled=lambda: False) -> int:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("secret path and token")
            succeeded.set()
            return 0

    service = FlakyService()
    worker = ArtifactCleanupWorker(service, poll_seconds=0.02, batch_size=2)
    worker.start()
    try:
        assert succeeded.wait(timeout=0.5)
        assert service.calls >= 2
        assert worker.last_error_category == "cleanup_worker_error"
        assert "secret" not in worker.last_error_category
    finally:
        worker.close()


@pytest.mark.anyio
async def test_app_lifespan_worker_processes_due_cleanup(tmp_path: Path) -> None:
    """Starting the app lifespan must run real due work, not a display-only worker."""
    runtime = tmp_path / "runtime"
    app = create_app(
        Settings(
            runtime_dir=runtime,
            database_path=runtime / "workbench.sqlite3",
            artifact_cleanup_poll_seconds=1,
            artifact_cleanup_batch_size=1,
        )
    )
    payload = b"due"
    relative = "orphaned/due.bin"
    target = runtime / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    record = app.state.artifact_cleanup_service.enqueue(
        ArtifactCleanupCandidate(
            owner_type="material",
            owner_id=str(uuid4()),
            relative_path=relative,
            expected_sha256=sha256(payload).hexdigest(),
            expected_size_bytes=len(payload),
            reason="worker_due_test",
            not_before=datetime.now(UTC) - timedelta(seconds=1),
        )
    )

    async with app.router.lifespan_context(app):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            current = app.state.artifact_cleanup_service.get_record(record.id)
            if current is not None and current.state == "quarantined":
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("cleanup worker did not process the due durable record")

    assert target.exists() is False
    assert app.state.artifact_cleanup_worker.is_alive is False


def test_create_app_recovers_expired_leases_and_uses_configured_grace(
    tmp_path: Path,
) -> None:
    """Restart must recover durable claims before a newly configured worker runs."""
    runtime = tmp_path / "runtime"
    settings = Settings(
        runtime_dir=runtime,
        database_path=runtime / "workbench.sqlite3",
        artifact_cleanup_grace_hours=48,
    )
    first_app = create_app(settings)
    service: ArtifactCleanupService = first_app.state.artifact_cleanup_service
    payload = b"expired"
    relative = "orphaned/expired.bin"
    target = runtime / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    record = service.enqueue(
        ArtifactCleanupCandidate(
            owner_type="material",
            owner_id=str(uuid4()),
            relative_path=relative,
            expected_sha256=sha256(payload).hexdigest(),
            expected_size_bytes=len(payload),
            reason="restart_test",
            not_before=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    assert service.claim_due(limit=1) == [record.id]
    with service.database.session() as session:
        session.execute(
            update(ArtifactCleanupRecord)
            .where(ArtifactCleanupRecord.id == record.id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        session.commit()
    first_app.state.database.close()

    restarted = create_app(settings)
    try:
        recovered = restarted.state.artifact_cleanup_service.get_record(record.id)
        assert recovered is not None
        assert recovered.state == "pending"
        assert recovered.attempt_count == 1
        assert restarted.state.artifact_cleanup_service.grace_period == timedelta(
            hours=48
        )
        assert (
            restarted.state.content_service.cleanup_service
            is restarted.state.artifact_cleanup_service
        )
    finally:
        restarted.state.database.close()


class _MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 18, 12, 0, 0)

    def now(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


def _real_cleanup(
    tmp_path: Path,
    *,
    grace_hours: int = 24,
    clock: _MutableClock | None = None,
):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    database = Database(tmp_path / "cleanup.sqlite3", runtime_dir=runtime)
    service = ArtifactCleanupService(
        database,
        runtime_dir=runtime,
        grace_period=timedelta(hours=grace_hours),
        **({"clock": clock.now} if clock is not None else {}),
    )
    payload = b"shutdown-fence"
    relative = "orphaned/shutdown.bin"
    artifact = runtime / relative
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(payload)
    record = service.enqueue(
        ArtifactCleanupCandidate(
            owner_type="material",
            owner_id=str(uuid4()),
            relative_path=relative,
            expected_sha256=sha256(payload).hexdigest(),
            expected_size_bytes=len(payload),
            reason="shutdown_fence_test",
            not_before=(clock.now() if clock is not None else datetime.now(UTC))
            - timedelta(seconds=1),
        )
    )
    return database, runtime, service, artifact, record


def test_blocked_worker_aborts_after_close_and_finalizes_once() -> None:
    """A batch released after bounded close must not mutate and must finalize once."""
    entered = Event()
    release = Event()
    finalized = Event()
    mutations: list[str] = []
    finalizer_calls: list[str] = []

    class BlockedService:
        def run_due_once(self, *, limit: int = 10, cancelled=lambda: False) -> int:
            entered.set()
            release.wait()
            if not cancelled():
                mutations.append("late")
            return 0

    def finalize() -> None:
        finalizer_calls.append("closed")
        finalized.set()

    worker = ArtifactCleanupWorker(
        BlockedService(),
        poll_seconds=30,
        batch_size=1,
        on_stopped=finalize,
    )
    worker.start()
    assert entered.wait(timeout=0.5)

    started = time.monotonic()
    try:
        assert worker.close() is False
        assert time.monotonic() - started < 1
        assert finalizer_calls == []
    finally:
        release.set()
    assert worker.wait_stopped(timeout=1)
    assert mutations == []
    assert finalizer_calls == ["closed"]
    assert finalized.is_set()
    assert worker.close() is True
    assert finalizer_calls == ["closed"]


def test_shutdown_unsafe_still_finalizes_once_and_preserves_primary_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unresolved cleanup fact cannot justify leaking the app-owned database."""
    finalizer_calls: list[str] = []

    class UnsafeService:
        def run_due_once(self, *, limit: int = 10, cancelled=lambda: False) -> int:
            raise ArtifactCleanupShutdownUnsafe("sensitive path must not escape")

    def failing_finalizer() -> None:
        finalizer_calls.append("closed")
        raise RuntimeError("sensitive finalizer details")

    caplog.set_level("ERROR", logger="backend.app.features.content.cleanup")
    worker = ArtifactCleanupWorker(
        UnsafeService(),
        poll_seconds=30,
        batch_size=1,
        on_stopped=failing_finalizer,
    )
    worker.start()
    assert worker.wait_stopped(timeout=1)

    assert finalizer_calls == ["closed"]
    assert worker.last_error_category == "cleanup_shutdown_fact_unresolved"
    assert worker.finalizer_error_category == "cleanup_worker_finalizer_error"
    assert [record.message for record in caplog.records] == [
        "cleanup_shutdown_fact_unresolved",
        "cleanup_worker_finalizer_error",
    ]
    assert all("sensitive" not in record.message for record in caplog.records)
    assert worker.close() is True
    assert finalizer_calls == ["closed"]


def test_stop_before_claim_does_not_claim_or_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop racing the claim boundary must leave the due row pending and bytes put."""
    database, _, service, artifact, record = _real_cleanup(tmp_path)
    entered = Event()
    release = Event()
    real_claim = service._claim_specific

    def blocked_claim(cleanup_id, *, now, cancelled=lambda: False):
        entered.set()
        release.wait()
        return real_claim(cleanup_id, now=now, cancelled=cancelled)

    monkeypatch.setattr(service, "_claim_specific", blocked_claim)
    worker = ArtifactCleanupWorker(service, poll_seconds=30, batch_size=1)
    worker.start()
    assert entered.wait(timeout=0.5)
    try:
        assert worker.close() is False
    finally:
        release.set()
    assert worker.wait_stopped(timeout=1)

    current = service.get_record(record.id)
    assert current is not None and current.state == "pending"
    assert artifact.exists()
    database.close()


def test_stop_after_claim_before_move_retains_claim_and_original_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow identity read returning after stop must not proceed to rename or terminal CAS."""
    database, _, service, artifact, record = _real_cleanup(tmp_path)
    entered = Event()
    release = Event()
    real_verified = service._verified_file

    def blocked_verified(relative_path, cleanup, *, cancelled=lambda: False):
        entered.set()
        release.wait()
        return real_verified(relative_path, cleanup, cancelled=cancelled)

    monkeypatch.setattr(service, "_verified_file", blocked_verified)
    worker = ArtifactCleanupWorker(service, poll_seconds=30, batch_size=1)
    worker.start()
    assert entered.wait(timeout=0.5)
    assert service.get_record(record.id).state == "claimed"  # type: ignore[union-attr]
    try:
        assert worker.close() is False
    finally:
        release.set()
    assert worker.wait_stopped(timeout=1)

    current = service.get_record(record.id)
    assert current is not None and current.state == "claimed"
    assert artifact.exists()
    database.close()


def test_stop_during_pre_delete_retains_quarantine_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delete-handle preparation returning after stop must never arm deletion."""
    database, _, service, _, record = _real_cleanup(tmp_path, grace_hours=0)
    quarantined = service.process_one(record.id)
    assert quarantined.state == "quarantined"
    quarantine_file = service.runtime_dir / str(quarantined.quarantine_path)
    entered = Event()
    release = Event()
    real_open = cleanup_module.open_contained_delete_handle

    def blocked_open(*args, **kwargs):
        entered.set()
        release.wait()
        return real_open(*args, **kwargs)

    monkeypatch.setattr(cleanup_module, "open_contained_delete_handle", blocked_open)
    worker = ArtifactCleanupWorker(service, poll_seconds=30, batch_size=1)
    worker.start()
    assert entered.wait(timeout=0.5)
    try:
        assert worker.close() is False
    finally:
        release.set()
    assert worker.wait_stopped(timeout=1)

    current = service.get_record(record.id)
    assert current is not None and current.state == "claimed"
    assert quarantine_file.exists()
    database.close()


@pytest.mark.anyio
async def test_app_lifespan_defers_database_close_until_blocked_worker_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lifespan must not dispose SQLite while a released cleanup can still touch it."""
    runtime = tmp_path / "runtime"
    app = create_app(
        Settings(runtime_dir=runtime, database_path=runtime / "workbench.sqlite3")
    )
    if app.state.shop_service is not None:
        app.state.shop_service.close()
        app.state.shop_service = None
    entered = Event()
    release = Event()
    close_calls: list[str] = []
    database = app.state.database
    real_close = database.close

    def blocked_batch(*, limit: int = 10, cancelled=lambda: False) -> int:
        entered.set()
        release.wait()
        assert cancelled()
        return 0

    def close_database() -> None:
        close_calls.append("closed")
        real_close()

    monkeypatch.setattr(app.state.artifact_cleanup_service, "run_due_once", blocked_batch)
    monkeypatch.setattr(database, "close", close_database)
    app.state.artifact_cleanup_worker = ArtifactCleanupWorker(
        app.state.artifact_cleanup_service,
        poll_seconds=30,
        batch_size=1,
        on_stopped=database.close,
    )

    started = 0.0
    async with app.router.lifespan_context(app):
        assert entered.wait(timeout=0.5)
        started = time.monotonic()
    try:
        assert time.monotonic() - started < 1
        assert close_calls == []
    finally:
        release.set()
    assert app.state.artifact_cleanup_worker.wait_stopped(timeout=1)
    assert close_calls == ["closed"]
    assert app.state.artifact_cleanup_worker.last_error_category is None
    assert app.state.artifact_cleanup_worker.finalizer_error_category is None


def test_stop_at_internal_rename_boundary_prevents_os_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The final helper-internal authorization fence must run after handle setup."""
    database, runtime, service, artifact, record = _real_cleanup(tmp_path)
    entered = Event()
    release = Event()
    close_calls: list[str] = []
    real_close = database.close

    def boundary_hook() -> None:
        entered.set()
        release.wait()

    def close_database() -> None:
        close_calls.append("closed")
        real_close()

    monkeypatch.setattr(
        export_module,
        "_rename_authorization_boundary_hook",
        boundary_hook,
    )
    worker = ArtifactCleanupWorker(
        service,
        poll_seconds=30,
        batch_size=1,
        on_stopped=close_database,
    )
    worker.start()
    if not entered.wait(timeout=1):
        worker.close()
        release.set()
        worker.wait_stopped(timeout=1)
        pytest.fail("rename authorization boundary was not reached")
    try:
        assert worker.close() is False
        assert close_calls == []
    finally:
        release.set()
    assert worker.wait_stopped(timeout=1)

    quarantine = (
        runtime
        / "artifacts-quarantine"
        / record.id
        / artifact.name
    )
    current = service.get_record(record.id)
    assert current is not None and current.state == "claimed"
    assert artifact.exists()
    assert quarantine.exists() is False
    assert close_calls == ["closed"]
    # Both source and target-directory handles must have been closed on cancellation.
    probe = artifact.with_name("handle-probe.bin")
    artifact.rename(probe)
    probe.rename(artifact)


def test_stop_after_atomic_rename_persists_moved_fact_before_database_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once Windows moved bytes, shutdown must durably record their new identity."""
    database, runtime, service, artifact, record = _real_cleanup(tmp_path)
    entered = Event()
    release = Event()
    close_calls: list[str] = []
    real_close = database.close

    def after_atomic_hook() -> None:
        entered.set()
        release.wait()

    def close_database() -> None:
        close_calls.append("closed")
        real_close()

    monkeypatch.setattr(
        export_module,
        "_rename_after_atomic_hook",
        after_atomic_hook,
    )
    worker = ArtifactCleanupWorker(
        service,
        poll_seconds=30,
        batch_size=1,
        on_stopped=close_database,
    )
    worker.start()
    if not entered.wait(timeout=1):
        worker.close()
        release.set()
        worker.wait_stopped(timeout=1)
        pytest.fail("post-atomic rename boundary was not reached")
    try:
        assert worker.close() is False
        assert close_calls == []
    finally:
        release.set()
    assert worker.wait_stopped(timeout=1)

    quarantine = (
        runtime
        / "artifacts-quarantine"
        / record.id
        / artifact.name
    )
    current = service.get_record(record.id)
    assert current is not None
    assert current.state == "needs_human"
    assert current.quarantine_path == (
        f"artifacts-quarantine/{record.id}/{artifact.name}"
    )
    assert current.last_error_category == "shutdown_after_quarantine_move"
    assert artifact.exists() is False
    assert quarantine.exists()
    assert close_calls == ["closed"]


def test_post_atomic_shutdown_records_fact_even_after_original_lease_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lease expiry cannot erase the already-completed physical move fact."""
    clock = _MutableClock()
    database, runtime, service, artifact, record = _real_cleanup(
        tmp_path, clock=clock
    )
    entered = Event()
    release = Event()
    close_calls: list[str] = []
    real_close = database.close

    def after_atomic_hook() -> None:
        entered.set()
        release.wait()

    def close_database() -> None:
        close_calls.append("closed")
        real_close()

    monkeypatch.setattr(export_module, "_rename_after_atomic_hook", after_atomic_hook)
    worker = ArtifactCleanupWorker(
        service,
        poll_seconds=30,
        batch_size=1,
        on_stopped=close_database,
    )
    worker.start()
    assert entered.wait(timeout=1)
    try:
        assert worker.close() is False
        clock.advance(timedelta(minutes=10))
    finally:
        release.set()
    assert worker.wait_stopped(timeout=1)

    current = service.get_record(record.id)
    assert current is not None
    assert current.state == "needs_human"
    assert current.last_error_category == "shutdown_after_quarantine_move"
    assert current.quarantine_path == (
        f"artifacts-quarantine/{record.id}/{artifact.name}"
    )
    assert (runtime / str(current.quarantine_path)).exists()
    assert close_calls == ["closed"]


def test_post_atomic_shutdown_overrides_concurrent_expired_lease_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pending recovery row must yield to the already-moved physical identity."""
    clock = _MutableClock()
    database, _, service, _, record = _real_cleanup(tmp_path, clock=clock)
    entered = Event()
    release = Event()

    def after_atomic_hook() -> None:
        entered.set()
        release.wait()

    monkeypatch.setattr(export_module, "_rename_after_atomic_hook", after_atomic_hook)
    worker = ArtifactCleanupWorker(service, poll_seconds=30, batch_size=1)
    worker.start()
    assert entered.wait(timeout=1)
    try:
        assert worker.close() is False
        clock.advance(timedelta(minutes=10))
        assert service.recover_expired_leases() == 1
        assert service.get_record(record.id).state == "pending"  # type: ignore[union-attr]
    finally:
        release.set()
    assert worker.wait_stopped(timeout=1)

    current = service.get_record(record.id)
    assert current is not None and current.state == "needs_human"
    assert current.last_error_category == "shutdown_after_quarantine_move"
    database.close()


def test_post_atomic_shutdown_overrides_concurrent_new_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A newer lease cannot authorize work against bytes already moved by the old lease."""
    clock = _MutableClock()
    database, runtime, service, _, record = _real_cleanup(tmp_path, clock=clock)
    entered = Event()
    release = Event()

    def after_atomic_hook() -> None:
        entered.set()
        release.wait()

    monkeypatch.setattr(export_module, "_rename_after_atomic_hook", after_atomic_hook)
    worker = ArtifactCleanupWorker(service, poll_seconds=30, batch_size=1)
    worker.start()
    assert entered.wait(timeout=1)
    try:
        assert worker.close() is False
        clock.advance(timedelta(minutes=10))
        assert service.recover_expired_leases() == 1
        second = ArtifactCleanupService(
            database, runtime_dir=runtime, clock=clock.now
        )
        assert second.claim_due(limit=1) == [record.id]
    finally:
        release.set()
    assert worker.wait_stopped(timeout=1)

    current = service.get_record(record.id)
    assert current is not None and current.state == "needs_human"
    assert current.lease_token is None
    assert current.last_error_category == "shutdown_after_quarantine_move"
    database.close()


def _persist_moved_fact(
    service: ArtifactCleanupService,
    cleanup_id: str,
    quarantine_path: str,
    identity: tuple[int, int, int, int],
    *,
    file_id_offset: int = 0,
) -> None:
    with service.database.session() as session:
        session.execute(
            update(ArtifactCleanupRecord)
            .where(ArtifactCleanupRecord.id == cleanup_id)
            .values(
                state="needs_human",
                quarantine_path=quarantine_path,
                quarantine_volume_id=identity[0],
                quarantine_file_id=identity[1] + file_id_offset,
                quarantine_size_bytes=identity[2],
                quarantine_mtime_ns=identity[3],
                lease_token=None,
                lease_expires_at=None,
                last_error_category="external_shutdown_reconcile",
            )
        )
        session.commit()


def test_post_atomic_shutdown_accepts_an_existing_identical_durable_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An identical concurrent acknowledgement is success, not a conflict."""
    database, runtime, service, artifact, record = _real_cleanup(tmp_path)
    entered = Event()
    release = Event()
    qpath = f"artifacts-quarantine/{record.id}/{artifact.name}"

    def after_atomic_hook() -> None:
        entered.set()
        release.wait()

    monkeypatch.setattr(export_module, "_rename_after_atomic_hook", after_atomic_hook)
    worker = ArtifactCleanupWorker(service, poll_seconds=30, batch_size=1)
    worker.start()
    assert entered.wait(timeout=1)
    try:
        assert worker.close() is False
        inspection = export_module.inspect_contained_artifact(runtime, qpath)
        assert inspection.identity is not None
        _persist_moved_fact(service, record.id, qpath, inspection.identity)
    finally:
        release.set()
    assert worker.wait_stopped(timeout=1)
    current = service.get_record(record.id)
    assert current is not None and current.quarantine_path == qpath
    database.close()


def test_post_atomic_shutdown_conflict_closes_database_after_worker_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A durable conflict remains observable without leaking SQLite ownership."""
    database, runtime, service, artifact, record = _real_cleanup(tmp_path)
    entered = Event()
    release = Event()
    close_calls: list[str] = []
    real_close = database.close
    qpath = f"artifacts-quarantine/{record.id}/{artifact.name}"

    def after_atomic_hook() -> None:
        entered.set()
        release.wait()

    def close_database() -> None:
        close_calls.append("closed")
        real_close()

    monkeypatch.setattr(export_module, "_rename_after_atomic_hook", after_atomic_hook)
    worker = ArtifactCleanupWorker(
        service,
        poll_seconds=30,
        batch_size=1,
        on_stopped=close_database,
    )
    worker.start()
    assert entered.wait(timeout=1)
    try:
        assert worker.close() is False
        inspection = export_module.inspect_contained_artifact(runtime, qpath)
        assert inspection.identity is not None
        _persist_moved_fact(
            service,
            record.id,
            qpath,
            inspection.identity,
            file_id_offset=1,
        )
    finally:
        release.set()
    assert worker.wait_stopped(timeout=1)

    assert close_calls == ["closed"]
    assert worker.last_error_category == "cleanup_shutdown_fact_unresolved"
    with database.session() as session:
        current = session.get(ArtifactCleanupRecord, record.id)
        assert current is not None
        assert current.state == "needs_human"
        assert current.last_error_category == "external_shutdown_reconcile"
    assert worker.close() is True
    assert close_calls == ["closed"]


def test_post_atomic_shutdown_read_fault_closes_database_after_worker_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unverifiable move stays observable while SQLite is finalized exactly once."""
    database, _, service, _, record = _real_cleanup(tmp_path)
    entered = Event()
    release = Event()
    close_calls: list[str] = []
    real_close = database.close

    def after_atomic_hook() -> None:
        entered.set()
        release.wait()

    def close_database() -> None:
        close_calls.append("closed")
        real_close()

    monkeypatch.setattr(export_module, "_rename_after_atomic_hook", after_atomic_hook)
    worker = ArtifactCleanupWorker(
        service,
        poll_seconds=30,
        batch_size=1,
        on_stopped=close_database,
    )
    worker.start()
    assert entered.wait(timeout=1)
    try:
        assert worker.close() is False

        def unavailable_read(_cleanup_id: str):
            raise SQLAlchemyError("database read unavailable")

        monkeypatch.setattr(service, "get_record", unavailable_read)
    finally:
        release.set()
    assert worker.wait_stopped(timeout=1)

    assert close_calls == ["closed"]
    assert worker.last_error_category == "cleanup_shutdown_fact_unresolved"
    with database.session() as session:
        current = session.get(ArtifactCleanupRecord, record.id)
        assert current is not None
        assert current.state == "needs_human"
        assert current.last_error_category == "shutdown_after_quarantine_move"
    assert worker.close() is True
    assert close_calls == ["closed"]
