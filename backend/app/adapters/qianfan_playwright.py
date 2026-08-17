"""Controlled, read-only Qianfan ranking collection with durable raw evidence."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from backend.app.adapters.contracts import (
    CollectionItem,
    CollectionRequest,
    CollectionResult,
    MissingCollectionItem,
)
from backend.app.models.jobs import JobState
from backend.app.services.jobs import JobService


QIANFAN_RANK_URL = "https://ark.xiaohongshu.com/app-datacenter/market/note-rank"
_RANK_RESPONSE_PATH = "/api/edith/business/data/note/"


class QianfanCaptureOutcome(BaseModel):
    status: Literal["captured", "needs_human"]
    reason: Literal[
        "login_required",
        "layout_changed",
        "navigation_failed",
        "response_not_observed",
        "response_unusable",
    ] | None = None
    raw_evidence: dict[str, Any] = Field(min_length=1)


class QianfanPlaywrightAdapter:
    """Observe ranking responses without platform writes or invented completion."""

    def __init__(
        self,
        page_factory: Callable[[], Any],
        *,
        job_service: JobService | None = None,
        runtime_dir: Path | None = None,
        timeout_seconds: float = 5,
        poll_interval: float = 0.1,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        owns_page: bool = False,
    ) -> None:
        self._page_factory = page_factory
        self._job_service = job_service
        self._runtime_dir = runtime_dir.resolve() if runtime_dir is not None else None
        self._timeout_seconds = max(timeout_seconds, 0)
        self._poll_interval = max(poll_interval, 0)
        self._sleep = sleep
        self._monotonic = monotonic
        self._owns_page = owns_page

    def collect_rankings(self, request: CollectionRequest) -> CollectionResult:
        """Implement the normalized collector contract and link capture evidence to a job."""
        job_id = self._validated_job_id(request)
        outcome = self.capture_visible_page()
        items = (
            _normalized_items(outcome.raw_evidence)
            if outcome.status == "captured"
            else []
        )
        if outcome.status == "captured" and not items:
            outcome = QianfanCaptureOutcome(
                status="needs_human",
                reason=(
                    "response_unusable"
                    if outcome.raw_evidence.get("responses")
                    else "response_not_observed"
                ),
                raw_evidence=outcome.raw_evidence,
            )
        artifact_paths = self._persist_job_evidence(job_id, outcome)
        if outcome.status == "needs_human":
            expected_count = request.expected_count
            missing_count = expected_count if expected_count is not None else 0
            missing_items = [
                MissingCollectionItem(
                    reference=f"qianfan_ranking:{index + 1}",
                    reason=outcome.reason or "needs_human",
                    raw_evidence=outcome.raw_evidence,
                )
                for index in range(missing_count)
            ]
            result = CollectionResult(
                status="needs_human",
                detail=outcome.reason,
                evidence_artifacts=artifact_paths,
                items=[],
                expected_count_known=expected_count is not None,
                expected_count=expected_count,
                succeeded_count=0,
                missing_items=missing_items,
                overflow_count=0,
                complete=False,
            )
            self._finalize_job(job_id, result)
            return result

        expected_count = request.expected_count
        if expected_count is None:
            result = CollectionResult(
                status="partial",
                detail="expected_count_unknown",
                evidence_artifacts=artifact_paths,
                items=items,
                expected_count_known=False,
                expected_count=None,
                succeeded_count=len(items),
                missing_items=[],
                overflow_count=0,
                complete=False,
            )
            self._finalize_job(job_id, result)
            return result
        if len(items) > expected_count:
            result = CollectionResult(
                status="failed",
                detail="observed_count_exceeds_expected",
                evidence_artifacts=artifact_paths,
                items=items,
                expected_count_known=True,
                expected_count=expected_count,
                succeeded_count=len(items),
                missing_items=[],
                overflow_count=len(items) - expected_count,
                complete=False,
            )
            self._finalize_job(job_id, result)
            return result
        missing_count = expected_count - len(items)
        missing_items = [
            MissingCollectionItem(
                reference=f"qianfan_ranking:{len(items) + index + 1}",
                reason="expected_item_not_observed",
                raw_evidence={"capture": outcome.raw_evidence},
            )
            for index in range(missing_count)
        ]
        complete = missing_count == 0
        result = CollectionResult(
            status="succeeded" if complete else "partial",
            detail=None if complete else "expected_items_missing",
            evidence_artifacts=artifact_paths,
            items=items,
            expected_count_known=True,
            expected_count=expected_count,
            succeeded_count=len(items),
            missing_items=missing_items,
            overflow_count=0,
            complete=complete,
        )
        self._finalize_job(job_id, result)
        return result

    def capture_visible_page(self) -> QianfanCaptureOutcome:
        page = self._page_factory()
        responses: list[dict[str, Any]] = []
        capture_errors: list[dict[str, str]] = []
        handler = lambda response: _record_response(response, responses)
        listener_added = False
        outcome: QianfanCaptureOutcome | None = None
        cleanup_errors: list[dict[str, str]] = []
        try:
            if hasattr(page, "on"):
                page.on("response", handler)
                listener_added = True
            outcome = self._observe_page(page, responses, capture_errors)
        finally:
            if listener_added and hasattr(page, "remove_listener"):
                try:
                    page.remove_listener("response", handler)
                except Exception as error:
                    cleanup_errors.append(_capture_error("listener_cleanup", error))
            if self._owns_page and hasattr(page, "close"):
                try:
                    page.close()
                except Exception as error:
                    cleanup_errors.append(_capture_error("page_close", error))
        assert outcome is not None
        if cleanup_errors:
            outcome.raw_evidence["cleanup_errors"] = cleanup_errors
        return outcome

    def _observe_page(
        self,
        page: Any,
        responses: list[dict[str, Any]],
        capture_errors: list[dict[str, str]],
    ) -> QianfanCaptureOutcome:
        try:
            page.goto(QIANFAN_RANK_URL, wait_until="domcontentloaded")
        except Exception as error:
            capture_errors.append(_capture_error("navigation", error))
            return QianfanCaptureOutcome(
                status="needs_human",
                reason="navigation_failed",
                raw_evidence=_raw_page_evidence(
                    page, responses, capture_errors=capture_errors
                ),
            )

        deadline = self._monotonic() + self._timeout_seconds
        table_observed = False
        while True:
            try:
                is_login = "/login" in str(page.url) or (
                    page.locator("input[type='password']").count() > 0
                )
                table_observed = (
                    page.locator(".note-rank table").count() > 0 or table_observed
                )
            except Exception as error:
                capture_errors.append(_capture_error("layout", error))
                return QianfanCaptureOutcome(
                    status="needs_human",
                    reason="layout_changed",
                    raw_evidence=_raw_page_evidence(
                        page, responses, capture_errors=capture_errors
                    ),
                )
            if is_login:
                return QianfanCaptureOutcome(
                    status="needs_human",
                    reason="login_required",
                    raw_evidence=_raw_page_evidence(page, responses),
                )
            if responses:
                return QianfanCaptureOutcome(
                    status="captured",
                    raw_evidence=_raw_page_evidence(page, responses),
                )
            if self._monotonic() >= deadline:
                return QianfanCaptureOutcome(
                    status="needs_human",
                    reason="response_not_observed" if table_observed else "layout_changed",
                    raw_evidence=_raw_page_evidence(page, responses),
                )
            self._sleep(self._poll_interval)

    def _validated_job_id(self, request: CollectionRequest) -> str | None:
        raw_job_id = request.parameters.get("job_id")
        if raw_job_id is None:
            return None
        if self._job_service is None or self._runtime_dir is None:
            raise RuntimeError(
                "job_service and runtime_dir are required when a collection job_id is supplied."
            )
        if not isinstance(raw_job_id, str):
            raise ValueError("collection job_id must be a canonical UUID string.")
        try:
            parsed = UUID(raw_job_id)
        except (ValueError, AttributeError) as error:
            raise ValueError("collection job_id must be a canonical UUID string.") from error
        if parsed.version != 4 or str(parsed) != raw_job_id:
            raise ValueError("collection job_id must be a canonical UUID string.")
        job = self._job_service.get(raw_job_id)
        if job.state is not JobState.running:
            raise ValueError("collection job must be claimed and running.")
        return raw_job_id

    def _persist_job_evidence(
        self, job_id: str | None, outcome: QianfanCaptureOutcome
    ) -> list[str]:
        if not job_id:
            return []
        assert self._job_service is not None
        assert self._runtime_dir is not None
        relative_path = Path("evidence") / "qianfan" / f"{job_id}-{uuid4().hex}.json"
        absolute_path = (self._runtime_dir / relative_path).resolve()
        try:
            absolute_path.relative_to(self._runtime_dir)
        except ValueError as error:
            raise ValueError("Qianfan evidence path escapes runtime storage.") from error
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        absolute_path.write_text(
            json.dumps(outcome.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        artifact = self._job_service.attach_artifact(
            job_id,
            kind="qianfan_raw_capture",
            path=relative_path.as_posix(),
            metadata={
                "source_url": QIANFAN_RANK_URL,
                "capture_status": outcome.status,
                "reason": outcome.reason,
            },
        )
        self._job_service.append_log(
            job_id,
            level="info" if outcome.status == "captured" else "warning",
            message=(
                "Qianfan ranking capture evidence persisted."
                if outcome.status == "captured"
                else f"Qianfan ranking capture needs human: {outcome.reason}."
            ),
        )
        return [artifact.path]

    def _finalize_job(self, job_id: str | None, result: CollectionResult) -> None:
        if job_id is None:
            return
        assert self._job_service is not None
        if result.status == "succeeded":
            self._job_service.transition(
                job_id,
                JobState.succeeded,
                progress_current=result.succeeded_count,
                progress_total=result.expected_count,
                current_stage="qianfan_complete",
            )
        elif result.status == "failed":
            self._job_service.transition(
                job_id,
                JobState.failed,
                current_stage="qianfan_failed",
                error_category=result.detail or "collection_failed",
            )
        else:
            self._job_service.transition(
                job_id,
                JobState.needs_human,
                current_stage="qianfan_needs_human",
                error_category=result.detail or result.status,
            )


def _record_response(response: Any, sink: list[dict[str, Any]]) -> None:
    url = str(response.url)
    if _RANK_RESPONSE_PATH not in url:
        return
    record: dict[str, Any] = {"url": url, "status": int(response.status)}
    raw_text: str | None = None
    try:
        raw_text = response.text()
    except Exception:
        try:
            raw_bytes = response.body()
            raw_text = bytes(raw_bytes).decode("utf-8", errors="replace")
        except Exception:
            raw_text = None
    try:
        record["body"] = response.json()
    except Exception as error:
        record["capture_error"] = type(error).__name__
        record["raw_text"] = raw_text if raw_text is not None else repr(response)
    sink.append(record)


def _raw_page_evidence(
    page: Any,
    responses: list[dict[str, Any]],
    *,
    capture_errors: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    errors = list(capture_errors or [])
    try:
        html = page.content()
    except Exception as error:
        html = None
        errors.append(_capture_error("content", error))
    evidence: dict[str, Any] = {
        "page_url": str(getattr(page, "url", "unknown")),
        "page_html": html,
        "responses": list(responses),
    }
    if errors:
        evidence["capture_errors"] = errors
    return evidence


def _capture_error(stage: str, error: Exception) -> dict[str, str]:
    return {"stage": stage, "type": type(error).__name__, "message": str(error)}


def _normalized_items(raw_evidence: dict[str, Any]) -> list[CollectionItem]:
    chosen: dict[str, CollectionItem] = {}
    for response in raw_evidence.get("responses", []):
        body = response.get("body")
        if not isinstance(body, dict):
            continue
        data = body.get("data")
        raw_items = data.get("dataList", []) if isinstance(data, dict) else []
        if not isinstance(raw_items, list):
            continue
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            item_id = _item_id(raw_item)
            note_id = str(raw_item.get("noteId") or raw_item.get("note_id") or "")
            canonical_note_url = _canonical_note_url(raw_item)
            source_url = (
                f"https://www.xiaohongshu.com/explore/{quote(note_id)}"
                if note_id
                else canonical_note_url or QIANFAN_RANK_URL
            )
            item = CollectionItem(
                id=item_id,
                kind="rank_item",
                source_url=source_url,
                raw_evidence={"response_url": response.get("url"), "item": raw_item},
                data={
                    "rank": raw_item.get("rank"),
                    "note_id": note_id or None,
                    "user_id": raw_item.get("userId") or raw_item.get("user_id"),
                    "author_name": raw_item.get("userNickname"),
                },
            )
            chosen.setdefault(item_id, item)
    return [chosen[item_id] for item_id in sorted(chosen)]


def _item_id(raw_item: dict[str, Any]) -> str:
    stable = raw_item.get("noteId") or raw_item.get("note_id")
    if stable:
        return str(stable)[:500]
    content_url = _canonical_note_url(raw_item)
    if content_url is not None:
        return f"url:{content_url}"[:500]
    canonical = json.dumps(
        {
            "user_id": _first_present(raw_item, "userId", "user_id"),
            "title": _first_present(raw_item, "noteTitle", "title", "note_title"),
            "publish_date": _first_present(
                raw_item,
                "publishTime",
                "publishDate",
                "publish_time",
                "publish_date",
            ),
            "author_name": _first_present(
                raw_item, "userNickname", "authorName", "author_name", "nickname"
            ),
            "content_type": _first_present(
                raw_item, "noteType", "note_type", "contentType", "content_type"
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_note_url(raw_item: dict[str, Any]) -> str | None:
    source_url = _first_present(
        raw_item,
        "noteUrl",
        "note_url",
        "sourceUrl",
        "source_url",
        "url",
    )
    if not source_url:
        return None
    parts = urlsplit(str(source_url))
    path_segments = [segment.lower() for segment in parts.path.split("/") if segment]
    has_note_identity = (
        "explore" in path_segments
        and path_segments.index("explore") + 1 < len(path_segments)
    ) or (
        "item" in path_segments
        and path_segments.index("item") + 1 < len(path_segments)
    )
    if not has_note_identity:
        return None
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", "")
    )


def _first_present(raw_item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = raw_item.get(key)
        if value not in (None, ""):
            return value
    return None
