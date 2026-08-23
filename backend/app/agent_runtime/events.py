"""Framework-neutral lifecycle events for Agent Runtime observability.

Durable state remains in SQLite. Event sinks are projections for logs, metrics,
streaming UIs, and later AG-UI mapping; a sink failure must never change the
business/runtime outcome.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RuntimeEventType(str, Enum):
    run_started = "run_started"
    before_model = "before_model"
    after_model = "after_model"
    before_tool_validate = "before_tool_validate"
    after_tool_validate = "after_tool_validate"
    permission_decision = "permission_decision"
    before_tool = "before_tool"
    after_tool = "after_tool"
    checkpoint_committed = "checkpoint_committed"
    human_action_required = "human_action_required"
    run_finished = "run_finished"
    run_error = "run_error"


class RuntimeEvent(BaseModel):
    event_type: RuntimeEventType
    run_id: str
    step_index: int | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)


class EventSink(Protocol):
    def emit(self, event: RuntimeEvent) -> None: ...


class NullEventSink:
    def emit(self, event: RuntimeEvent) -> None:  # noqa: ARG002
        return None


@dataclass(slots=True)
class RecordingEventSink:
    """Deterministic in-memory sink for tests and integration spikes."""

    events: list[RuntimeEvent] = field(default_factory=list)

    def emit(self, event: RuntimeEvent) -> None:
        self.events.append(event)


@dataclass(slots=True)
class SafeEventSink:
    """Prevent observability failures from corrupting runtime execution."""

    inner: EventSink

    def emit(self, event: RuntimeEvent) -> None:
        try:
            self.inner.emit(event)
        except Exception:  # pragma: no cover - logging path only
            logger.exception("Agent Runtime event sink failed for %s", event.event_type.value)
