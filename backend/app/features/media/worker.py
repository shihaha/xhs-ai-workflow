"""Single app-owned executor for reserved content media runs."""

from __future__ import annotations

from queue import Empty, Queue
from threading import Event, RLock, Thread

from backend.app.features.media.schemas import ContentMediaRunRead
from backend.app.features.media.service import ContentMediaService
from backend.app.features.media.store import MediaRunConflict, MediaRunTransactionUnknown


class MediaWorkerClosed(RuntimeError):
    """Submission is not accepted after application shutdown begins."""


class MediaProviderUnavailable(RuntimeError):
    """The configured capability cannot currently accept work."""


class ContentMediaWorker:
    """Execute one durable media run at a time with a final shutdown fence."""

    def __init__(
        self,
        service: ContentMediaService,
        *,
        poll_seconds: float = 0.1,
        close_timeout: float = 0.25,
    ) -> None:
        self.service = service
        self.poll_seconds = max(0.001, min(poll_seconds, 1.0))
        self.close_timeout = max(0.0, min(close_timeout, 5.0))
        self._queue: Queue[tuple[str, str, int]] = Queue()
        self._lock = RLock()
        self._stop = Event()
        self._stopped = Event()
        self._idle = Event()
        self._idle.set()
        self._thread: Thread | None = None
        self._accepting = True
        self._generation = 0
        self._active_run_id: str | None = None

    @property
    def is_alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if not self._accepting or self._thread is not None:
                return
            self._thread = Thread(
                target=self._run,
                name="content-media",
                daemon=True,
            )
            self._thread.start()
            recoverable = self.service.run_store.list_startup_recoverable()
            for run in recoverable:
                if run.status == "queued":
                    self._enqueue_locked(run)
                else:
                    self._mark_interrupted(run)

    def submit_generation(
        self,
        item_id: str,
        *,
        expected_revision_id: str,
        image_plan_entry_id: str,
    ) -> ContentMediaRunRead:
        if not self.service.image_adapter.configured:
            raise MediaProviderUnavailable("Image generation is not configured.")
        with self._lock:
            self._require_accepting()
            self.start()
            run = self.service.submit_generation(
                item_id,
                expected_revision_id=expected_revision_id,
                image_plan_entry_id=image_plan_entry_id,
            )
            self._enqueue_locked(run)
            return run

    def submit_analysis(
        self,
        item_id: str,
        *,
        expected_revision_id: str,
        material_ids: list[str],
    ) -> ContentMediaRunRead:
        if not self.service.vision_adapter.configured:
            raise MediaProviderUnavailable("Visual analysis is not configured.")
        with self._lock:
            self._require_accepting()
            self.start()
            run = self.service.submit_analysis(
                item_id,
                expected_revision_id=expected_revision_id,
                material_ids=material_ids,
            )
            self._enqueue_locked(run)
            return run

    def close(self) -> bool:
        with self._lock:
            if self._accepting:
                self._accepting = False
                self._generation += 1
            self._stop.set()
            run_ids: set[str] = set()
            if self._active_run_id is not None:
                run_ids.add(self._active_run_id)
            while True:
                try:
                    run_id, _, _ = self._queue.get_nowait()
                except Empty:
                    break
                run_ids.add(run_id)
                self._queue.task_done()
            thread = self._thread
        for run_id in run_ids:
            try:
                self.service.cancel_run(run_id)
            except Exception:
                continue
        if thread is None:
            self._stopped.set()
            self._idle.set()
            return True
        thread.join(timeout=self.close_timeout)
        return not thread.is_alive()

    def wait_idle(self, timeout: float) -> bool:
        return self._idle.wait(max(0.0, timeout))

    def wait_stopped(self, timeout: float) -> bool:
        return self._stopped.wait(max(0.0, timeout))

    def _enqueue_locked(self, run: ContentMediaRunRead) -> None:
        self._idle.clear()
        self._queue.put((run.id, run.capability, self._generation))

    def _require_accepting(self) -> None:
        if not self._accepting:
            raise MediaWorkerClosed("Media worker is closed.")

    def _admitted(self, generation: int) -> bool:
        with self._lock:
            return self._accepting and self._generation == generation

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    run_id, capability, generation = self._queue.get(
                        timeout=self.poll_seconds
                    )
                except Empty:
                    continue
                with self._lock:
                    self._active_run_id = run_id
                try:
                    if not self._admitted(generation):
                        self.service.cancel_run(run_id)
                        continue
                    admission = lambda: self._admitted(generation)
                    if capability == "generate":
                        self.service.run_generation(
                            run_id, admission_check=admission
                        )
                    else:
                        self.service.run_analysis(
                            run_id, admission_check=admission
                        )
                except Exception:
                    # Service/store boundaries retain only sanitized durable facts.
                    pass
                finally:
                    with self._lock:
                        self._active_run_id = None
                    self._queue.task_done()
                    if self._queue.unfinished_tasks == 0:
                        self._idle.set()
        finally:
            self._idle.set()
            self._stopped.set()

    def _mark_interrupted(self, run: ContentMediaRunRead) -> None:
        try:
            self.service.run_store.needs_human(
                run.id,
                expected_version=run.state_version,
                lease_token=run.lease_token,
                error_category="state_changed",
                error_detail="Media run requires operator review.",
            )
        except (MediaRunConflict, MediaRunTransactionUnknown):
            pass
