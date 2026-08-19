from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

from backend.app.adapters.contracts import GeneratedImage, ImageGenerationRequest
from backend.app.features.media.service import ContentMediaService
from backend.app.features.media.worker import ContentMediaWorker
from backend.tests.media.test_generation_service import (
    FakeImageAdapter,
    PNG_1X1,
    UnusedVisionAdapter,
    _media_service,
)


class BlockingImageAdapter:
    configured = True
    provider = "controlled_image"
    model = "controlled-image-v1"

    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    def generate_images(self, request: ImageGenerationRequest) -> list[GeneratedImage]:
        self.started.set()
        assert self.release.wait(2)
        return [GeneratedImage(
            data=PNG_1X1,
            mime_type="image/png",
            width=1,
            height=1,
            sha256=hashlib.sha256(PNG_1X1).hexdigest(),
            provider_request_id="blocked-request",
            usage={"images": 1},
            duration_ms=5,
            raw_evidence={
                "attempts": [{"attempt": 1, "category": "response_received"}],
                "prompt_version": request.prompt_version,
            },
        )]


def _blocking_service(tmp_path: Path) -> tuple[ContentMediaService, object, object, BlockingImageAdapter]:
    base, item, original = _media_service(tmp_path)
    adapter = BlockingImageAdapter()
    service = ContentMediaService(
        base.database,
        content_service=base.content_service,
        image_adapter=adapter,
        vision_adapter=UnusedVisionAdapter(),
        runtime_dir=tmp_path,
    )
    return service, item, original, adapter


def test_close_during_provider_call_prevents_late_success(tmp_path: Path) -> None:
    """Removing the post-provider shutdown fence would persist a late success."""

    service, item, original, adapter = _blocking_service(tmp_path)
    worker = ContentMediaWorker(service, poll_seconds=0.01, close_timeout=0.05)
    run = worker.submit_generation(
        item.id,
        expected_revision_id=item.current_revision.id,
        image_plan_entry_id=original.id,
    )
    assert adapter.started.wait(1)

    assert worker.close() is False
    assert service.get_run(run.id).status == "cancelled"
    adapter.release.set()
    assert worker.wait_stopped(1)

    final = service.get_run(run.id)
    assert final.status == "cancelled"
    assert final.output_material_id is None


def test_start_recovers_queued_run_once(tmp_path: Path) -> None:
    """Skipping startup recovery would strand a durable queued media run."""

    service, item, original = _media_service(tmp_path)
    queued = service.submit_generation(
        item.id,
        expected_revision_id=item.current_revision.id,
        image_plan_entry_id=original.id,
    )
    worker = ContentMediaWorker(service, poll_seconds=0.01)

    worker.start()
    assert worker.wait_idle(2)
    assert service.get_run(queued.id).status == "succeeded"
    assert worker.close() is True


def test_start_marks_future_lease_running_run_for_operator_recovery(
    tmp_path: Path,
) -> None:
    """Trusting an old process lease would strand its paid work until far in the future."""

    service, item, original = _media_service(tmp_path)
    queued = service.submit_generation(
        item.id,
        expected_revision_id=item.current_revision.id,
        image_plan_entry_id=original.id,
    )
    running = service.run_store.claim(
        queued.id,
        expected_version=queued.state_version,
        lease_token="00000000-0000-4000-8000-000000000001",
        lease_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    worker = ContentMediaWorker(service, poll_seconds=0.01)

    worker.start()
    assert worker.wait_idle(1)
    recovered = service.get_run(running.id)

    assert recovered.status == "needs_human"
    assert recovered.error_category == "state_changed"
    assert recovered.output_material_id is None
    assert worker.close() is True
