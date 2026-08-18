from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from threading import Event
import time
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import update

from backend.app.features.content.cleanup import (
    ArtifactCleanupCandidate,
    ArtifactCleanupService,
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

    def run_due_once(self, *, limit: int = 10) -> int:
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
        def run_due_once(self, *, limit: int = 10) -> int:
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


def test_worker_sanitizes_fault_and_continues_next_poll() -> None:
    """One failed batch must neither leak its exception text nor kill future cleanup."""
    succeeded = Event()

    class FlakyService:
        def __init__(self) -> None:
            self.calls = 0

        def run_due_once(self, *, limit: int = 10) -> int:
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
