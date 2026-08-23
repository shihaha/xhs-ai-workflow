"""Local inbox watcher for AgentDock-returned manual ChatGPT results.

The watcher derives candidate identities from durable pending handoffs, never
from arbitrary filenames.  A returned file is only a transport signal; the
handoff service remains the authority that validates identity/hash/schema,
Job/run state, and the latest binding before accepting anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Callable

from sqlalchemy import select

from backend.app.agent_runtime.manual_chatgpt_handoff import (
    AcceptedManualChatGPTResult,
    ManualChatGPTHandoffError,
    ManualChatGPTHandoffRecord,
    ManualChatGPTHandoffService,
)


@dataclass(frozen=True, slots=True)
class InboxRejection:
    handoff_id: str
    detail: str


@dataclass(frozen=True, slots=True)
class InboxPollResult:
    checked: int
    waiting: int
    accepted: tuple[AcceptedManualChatGPTResult, ...]
    rejected: tuple[InboxRejection, ...]


class ManualChatGPTInboxWatcher:
    """Poll fixed AgentDock return paths for durable pending handoffs."""

    def __init__(
        self,
        service: ManualChatGPTHandoffService,
        *,
        poll_seconds: float = 1.0,
        batch_size: int = 50,
        on_result: Callable[[AcceptedManualChatGPTResult], None] | None = None,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if batch_size < 1 or batch_size > 500:
            raise ValueError("batch_size must be between 1 and 500")
        self.service = service
        self.poll_seconds = poll_seconds
        self.batch_size = batch_size
        self.on_result = on_result
        self._stop = Event()
        self._wake = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._last_poll: InboxPollResult | None = None
        self._last_loop_error: str | None = None

    @property
    def last_poll(self) -> InboxPollResult | None:
        with self._lock:
            return self._last_poll

    @property
    def last_loop_error(self) -> str | None:
        with self._lock:
            return self._last_loop_error

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return
        self._stop.clear()
        self._wake.clear()
        self._thread = Thread(
            target=self._run,
            name="manual-chatgpt-inbox",
            daemon=True,
        )
        self._thread.start()

    def wake(self) -> None:
        """Request an immediate poll without waiting for the normal interval."""

        self._wake.set()

    def close(self, *, timeout_seconds: float = 2.0) -> bool:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout_seconds)
        stopped = not thread.is_alive()
        if stopped:
            self._thread = None
        return stopped

    def poll_once(self) -> InboxPollResult:
        pending_ids = self._pending_handoff_ids()
        accepted: list[AcceptedManualChatGPTResult] = []
        rejected: list[InboxRejection] = []
        waiting = 0

        for handoff_id in pending_ids:
            return_path = self._return_path(handoff_id)
            if not return_path.exists():
                waiting += 1
                continue
            # A symlink or non-file is not a harmless "not ready" condition;
            # let the authoritative service reject it and surface diagnostics.
            try:
                result = self.service.accept_return_file(handoff_id)
            except ManualChatGPTHandoffError as exc:
                rejected.append(InboxRejection(handoff_id=handoff_id, detail=str(exc)))
                continue
            accepted.append(result)
            if self.on_result is not None:
                try:
                    self.on_result(result)
                except Exception as exc:  # callback is observational, never authority
                    rejected.append(
                        InboxRejection(
                            handoff_id=handoff_id,
                            detail=f"post_accept_callback_error: {type(exc).__name__}: {exc}",
                        )
                    )

        result = InboxPollResult(
            checked=len(pending_ids),
            waiting=waiting,
            accepted=tuple(accepted),
            rejected=tuple(rejected),
        )
        with self._lock:
            self._last_poll = result
        return result

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
                with self._lock:
                    self._last_loop_error = None
            except Exception as exc:
                # The passive watcher must not kill the workbench process.  An
                # unexpected infrastructure error is retained for health/UI
                # visibility and retried on the next bounded poll.
                with self._lock:
                    self._last_loop_error = f"{type(exc).__name__}: {exc}"
            self._wake.wait(self.poll_seconds)
            self._wake.clear()

    def _pending_handoff_ids(self) -> list[str]:
        with self.service.database.sessions() as session:
            return list(
                session.scalars(
                    select(ManualChatGPTHandoffRecord.id)
                    .where(ManualChatGPTHandoffRecord.status == "pending")
                    .order_by(
                        ManualChatGPTHandoffRecord.created_at,
                        ManualChatGPTHandoffRecord.id,
                    )
                    .limit(self.batch_size)
                )
            )

    def _return_path(self, handoff_id: str) -> Path:
        # IDs originate from validated durable records.  resolve + relative_to
        # still closes accidental path regressions in this transport layer.
        target = (
            self.service.runtime_dir / "external-results" / f"{handoff_id}.json"
        ).resolve()
        try:
            target.relative_to(self.service.runtime_dir)
        except ValueError as exc:
            raise ManualChatGPTHandoffError("AgentDock inbox path escaped runtime_dir.") from exc
        return target
