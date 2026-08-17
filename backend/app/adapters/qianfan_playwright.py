"""Controlled, read-only Qianfan ranking collection with durable raw evidence."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
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
    RejectedCollectionItem,
)
from backend.app.models.jobs import JobState
from backend.app.services.jobs import JobService


QIANFAN_RANK_URL = "https://ark.xiaohongshu.com/app-datacenter/market/note-rank"
_RANK_RESPONSE_PATH = "/api/edith/business/data/note/"
_XHS_PUBLIC_ORIGIN = "https://www.xiaohongshu.com"


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


@dataclass(frozen=True)
class _NormalizedRankings:
    items: list[CollectionItem]
    rejected_items: list[RejectedCollectionItem]
    valid_response_observed: bool


class _RankItemUnusable(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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
        stage = "capture"
        try:
            outcome = self.capture_visible_page()
            stage = "evidence_persistence"
            artifact_paths = self._persist_job_evidence(job_id, outcome)

            if outcome.status == "needs_human":
                stage = "result_validation"
                result = _needs_human_result(
                    request=request,
                    detail=outcome.reason or "needs_human",
                    artifact_paths=artifact_paths,
                    raw_evidence=outcome.raw_evidence,
                )
            else:
                stage = "normalization"
                normalized = _normalized_items(outcome.raw_evidence)
                stage = "result_validation"
                if not normalized.valid_response_observed:
                    result = _needs_human_result(
                        request=request,
                        detail=(
                            "response_unusable"
                            if outcome.raw_evidence.get("responses")
                            else "response_not_observed"
                        ),
                        artifact_paths=artifact_paths,
                        raw_evidence=outcome.raw_evidence,
                    )
                else:
                    result = _accounted_result(
                        request=request,
                        items=normalized.items,
                        rejected_items=normalized.rejected_items,
                        artifact_paths=artifact_paths,
                        raw_evidence=outcome.raw_evidence,
                    )

            stage = "job_finalization"
            self._finalize_job(job_id, result)
            return result
        except Exception as error:
            self._fail_running_job(job_id, category=f"{stage}_failed", error=error)
            raise

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

    def _fail_running_job(
        self, job_id: str | None, *, category: str, error: Exception
    ) -> None:
        if job_id is None:
            return
        assert self._job_service is not None
        try:
            self._job_service.append_log(
                job_id,
                level="error",
                message=(
                    f"Qianfan collection raised during {category}: "
                    f"{type(error).__name__}: {error}"
                ),
            )
        except Exception as log_error:
            error.add_note(
                "Could not append Qianfan failure log: "
                f"{type(log_error).__name__}: {log_error}"
            )
        try:
            if self._job_service.get(job_id).state is JobState.running:
                self._job_service.transition(
                    job_id,
                    JobState.failed,
                    current_stage="qianfan_failed",
                    error_category=category,
                )
        except Exception as transition_error:
            error.add_note(
                "Could not transition Qianfan job after failure: "
                f"{type(transition_error).__name__}: {transition_error}"
            )


def _needs_human_result(
    *,
    request: CollectionRequest,
    detail: str,
    artifact_paths: list[str],
    raw_evidence: dict[str, Any],
) -> CollectionResult:
    expected_count = request.expected_count
    missing_count = expected_count if expected_count is not None else 0
    missing_items = [
        MissingCollectionItem(
            reference=f"qianfan_ranking:{index + 1}",
            reason=detail,
            raw_evidence=raw_evidence,
        )
        for index in range(missing_count)
    ]
    return CollectionResult(
        status="needs_human",
        detail=detail,
        evidence_artifacts=artifact_paths,
        items=[],
        rejected_items=[],
        expected_count_known=expected_count is not None,
        expected_count=expected_count,
        succeeded_count=0,
        observed_count=0,
        missing_items=missing_items,
        overflow_count=0,
        complete=False,
    )


def _accounted_result(
    *,
    request: CollectionRequest,
    items: list[CollectionItem],
    rejected_items: list[RejectedCollectionItem],
    artifact_paths: list[str],
    raw_evidence: dict[str, Any],
) -> CollectionResult:
    expected_count = request.expected_count
    observed_count = len(items) + len(rejected_items)
    if expected_count is None:
        if not items and not rejected_items:
            return _needs_human_result(
                request=request,
                detail="expected_count_unknown",
                artifact_paths=artifact_paths,
                raw_evidence=raw_evidence,
            )
        return CollectionResult(
            status="needs_human" if rejected_items else "partial",
            detail="response_unusable" if rejected_items else "expected_count_unknown",
            evidence_artifacts=artifact_paths,
            items=items,
            rejected_items=rejected_items,
            expected_count_known=False,
            expected_count=None,
            succeeded_count=len(items),
            observed_count=observed_count,
            missing_items=[],
            overflow_count=0,
            complete=False,
        )
    if observed_count > expected_count:
        return CollectionResult(
            status="failed",
            detail="observed_count_exceeds_expected",
            evidence_artifacts=artifact_paths,
            items=items,
            rejected_items=rejected_items,
            expected_count_known=True,
            expected_count=expected_count,
            succeeded_count=len(items),
            observed_count=observed_count,
            missing_items=[],
            overflow_count=observed_count - expected_count,
            complete=False,
        )
    missing_count = expected_count - observed_count
    missing_items = [
        MissingCollectionItem(
            reference=f"qianfan_ranking:{observed_count + index + 1}",
            reason="expected_item_not_observed",
            raw_evidence={"capture": raw_evidence},
        )
        for index in range(missing_count)
    ]
    complete = missing_count == 0 and not rejected_items
    if complete:
        status = "succeeded"
        detail = None
    elif rejected_items:
        status = "needs_human"
        detail = "response_unusable"
    else:
        status = "partial"
        detail = "expected_items_missing"
    return CollectionResult(
        status=status,
        detail=detail,
        evidence_artifacts=artifact_paths,
        items=items,
        rejected_items=rejected_items,
        expected_count_known=True,
        expected_count=expected_count,
        succeeded_count=len(items),
        observed_count=observed_count,
        missing_items=missing_items,
        overflow_count=0,
        complete=complete,
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


def _normalized_items(raw_evidence: dict[str, Any]) -> _NormalizedRankings:
    chosen: dict[str, CollectionItem] = {}
    rejected_items: list[RejectedCollectionItem] = []
    valid_response_observed = False
    for response_index, response in enumerate(raw_evidence.get("responses", [])):
        if not isinstance(response, dict):
            continue
        body = response.get("body")
        if not isinstance(body, dict):
            continue
        if not _successful_response(response, body):
            continue
        data = body.get("data")
        if not isinstance(data, dict) or "dataList" not in data:
            continue
        raw_items = data.get("dataList")
        if not isinstance(raw_items, list):
            continue
        valid_response_observed = True
        for item_index, raw_item in enumerate(raw_items):
            reference = f"qianfan_response:{response_index + 1}:item:{item_index + 1}"
            item_evidence = {"response_url": response.get("url"), "item": raw_item}
            if not isinstance(raw_item, dict):
                rejected_items.append(
                    RejectedCollectionItem(
                        reference=reference,
                        reason="row_not_object",
                        raw_evidence=item_evidence,
                    )
                )
                continue
            try:
                item_id, note_id, canonical_note_url = _item_identity(raw_item)
            except _RankItemUnusable as error:
                rejected_items.append(
                    RejectedCollectionItem(
                        reference=reference,
                        reason=error.reason,
                        raw_evidence=item_evidence,
                    )
                )
                continue
            source_url = (
                f"https://www.xiaohongshu.com/explore/{quote(note_id)}"
                if note_id
                else canonical_note_url or QIANFAN_RANK_URL
            )
            item = CollectionItem(
                id=item_id,
                kind="rank_item",
                source_url=source_url,
                raw_evidence=item_evidence,
                data={
                    "rank": raw_item.get("rank"),
                    "note_id": note_id or None,
                    "user_id": raw_item.get("userId") or raw_item.get("user_id"),
                    "author_name": raw_item.get("userNickname"),
                },
            )
            if item_id in chosen:
                rejected_items.append(
                    RejectedCollectionItem(
                        reference=reference,
                        reason="duplicate_identity",
                        raw_evidence=item_evidence,
                    )
                )
                continue
            chosen[item_id] = item
    return _NormalizedRankings(
        items=[chosen[item_id] for item_id in sorted(chosen)],
        rejected_items=rejected_items,
        valid_response_observed=valid_response_observed,
    )


def _item_identity(raw_item: dict[str, Any]) -> tuple[str, str | None, str | None]:
    raw_note_id = _first_present(raw_item, "noteId", "note_id")
    if raw_note_id is not None:
        note_id = _safe_identity_token(raw_note_id)
        if note_id is None:
            raise _RankItemUnusable("malformed_note_id")
        return note_id[:500], note_id, None
    content_url = _canonical_note_url(raw_item)
    if content_url is not None:
        return f"url:{content_url}"[:500], None, content_url
    if _first_present(
        raw_item,
        "noteUrl",
        "note_url",
        "sourceUrl",
        "source_url",
        "url",
    ) is not None:
        raise _RankItemUnusable("malformed_note_url")
    user_id = _identity_text(_first_present(raw_item, "userId", "user_id"))
    title = _identity_text(
        _first_present(raw_item, "noteTitle", "title", "note_title")
    )
    if user_id is None or title is None:
        raise _RankItemUnusable("identity_insufficient")
    publish_date = _identity_text(
        _first_present(
            raw_item,
            "publishTime",
            "publishDate",
            "publish_time",
            "publish_date",
        )
    )
    if publish_date is None:
        raise _RankItemUnusable("identity_insufficient")
    raw_content_type = _first_present(
        raw_item,
        "contentType",
        "content_type",
        "noteType",
        "note_type",
        "mediaType",
        "media_type",
        "type",
    )
    if not isinstance(raw_content_type, str) or not raw_content_type.strip():
        raise _RankItemUnusable("identity_insufficient")
    content_type = raw_content_type.strip()
    canonical = json.dumps(
        {
            "user_id": user_id,
            "title": title,
            "publish_date": publish_date,
            "content_type": content_type.casefold(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return sha256(canonical.encode("utf-8")).hexdigest(), None, None


def _canonical_note_url(raw_item: dict[str, Any]) -> str | None:
    source_url = _first_present(
        raw_item,
        "noteUrl",
        "note_url",
        "sourceUrl",
        "source_url",
        "url",
    )
    if not isinstance(source_url, str):
        return None
    candidate = source_url.strip()
    if (
        not candidate
        or "\\" in candidate
        or any(character.isspace() for character in candidate)
    ):
        return None
    if re.search(r"%(?![0-9A-Fa-f]{2})", candidate):
        return None
    if candidate.startswith("/"):
        if candidate.startswith("//"):
            return None
        candidate = f"{_XHS_PUBLIC_ORIGIN}{candidate}"
    try:
        parts = urlsplit(candidate)
        hostname = parts.hostname
        port = parts.port
    except (TypeError, ValueError, UnicodeError):
        return None
    if parts.scheme.lower() != "https" or hostname is None:
        return None
    if parts.username is not None or parts.password is not None or port is not None:
        return None
    if parts.fragment:
        return None
    normalized_hostname = hostname.lower().rstrip(".")
    if normalized_hostname != "xiaohongshu.com" and not normalized_hostname.endswith(
        ".xiaohongshu.com"
    ):
        return None
    canonical_path = _canonical_note_path(parts.path)
    if canonical_path is None:
        return None
    return urlunsplit(
        ("https", "www.xiaohongshu.com", canonical_path, "", "")
    )


def _canonical_note_path(path: str) -> str | None:
    for pattern in (
        r"/explore/([A-Za-z0-9_-]{1,500})/?",
        r"/item/([A-Za-z0-9_-]{1,500})/?",
        r"/discovery/item/([A-Za-z0-9_-]{1,500})/?",
    ):
        match = re.fullmatch(pattern, path)
        if match is not None:
            return path.rstrip("/")
    return None


def _successful_response(response: dict[str, Any], body: dict[str, Any]) -> bool:
    http_status = response.get("status")
    if isinstance(http_status, bool) or not isinstance(http_status, int):
        return False
    if not 200 <= http_status < 300:
        return False
    for key in ("success", "code", "status"):
        if key in body and not _successful_business_marker(key, body[key]):
            return False
    return True


def _successful_business_marker(key: str, marker: Any) -> bool:
    if key == "success":
        if isinstance(marker, bool):
            return marker
        if isinstance(marker, str):
            return marker.strip().casefold() in {
                "true",
                "success",
                "succeeded",
                "ok",
            }
        return False
    if isinstance(marker, bool):
        return marker
    if isinstance(marker, int):
        return marker in {0, 200}
    if isinstance(marker, str):
        return marker.strip().casefold() in {"0", "200", "success", "succeeded", "ok"}
    return False


def _identity_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _safe_identity_token(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    normalized = str(value).strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{1,500}", normalized) is None:
        return None
    return normalized


def _first_present(raw_item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = raw_item.get(key)
        if value not in (None, ""):
            return value
    return None
