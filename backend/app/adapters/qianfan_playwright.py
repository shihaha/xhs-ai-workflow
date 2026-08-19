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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.app.adapters.contracts import (
    CollectionItem,
    CollectionRequest,
    CollectionResult,
    MissingCollectionItem,
    RejectedCollectionItem,
)
from backend.app.models.jobs import JobState
from backend.app.services.jobs import JobService
from backend.app.features.radar.models import BoardName, DimensionName


QIANFAN_RANK_URL = "https://ark.xiaohongshu.com/app-datacenter/market/note-rank"
_RANK_RESPONSE_PATH = "/api/edith/business/data/note/"
_CONTENT_RANK_RESPONSE_PATH = "/api/edith/business/data/note/rank/v2/list"
_ACCOUNT_RANK_RESPONSE_PATH = "/api/edith/business/data/note/user/rank/v2/list"
_XHS_PUBLIC_ORIGIN = "https://www.xiaohongshu.com"
_QIANFAN_BOARDS = ("阅读榜", "引流榜", "热卖榜", "成交榜")
_QIANFAN_DIMENSIONS = ("优秀内容", "优秀账号")


class QianfanSelectorProfile(BaseModel):
    """Versioned, code-owned selector and response-scope verification contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1, max_length=100)
    supported: bool
    ready_selector: str | None = None
    login_selector: str | None = None
    captcha_selector: str | None = None
    board_selectors: tuple[tuple[str, str], ...] = ()
    dimension_selectors: tuple[tuple[str, str], ...] = ()
    active_board_selectors: tuple[tuple[str, str], ...] = ()
    active_dimension_selectors: tuple[tuple[str, str], ...] = ()
    response_endpoint_paths: tuple[tuple[str, str], ...] = (
        ("优秀内容", _CONTENT_RANK_RESPONSE_PATH),
        ("优秀账号", _ACCOUNT_RANK_RESPONSE_PATH),
    )
    board_request_mapping: tuple[tuple[str, int], ...] = (
        ("阅读榜", 1),
        ("引流榜", 2),
        ("热卖榜", 3),
        ("成交榜", 4),
    )
    request_note_type: int = 0
    canonical_page_no: int = 1
    canonical_page_size: int = 10
    # Retained only so controlled historical fixtures can be constructed. Scope
    # verification for supported profiles is request-bound, never body-bound.
    response_board_path: tuple[str, ...] = ()
    response_dimension_path: tuple[str, ...] = ()

    @field_validator(
        "board_selectors",
        "dimension_selectors",
        "active_board_selectors",
        "active_dimension_selectors",
        "response_endpoint_paths",
        "board_request_mapping",
        mode="before",
    )
    @classmethod
    def freeze_selector_map(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return tuple(sorted(value.items()))
        return value

    @model_validator(mode="after")
    def validate_supported_profile(self) -> "QianfanSelectorProfile":
        if not self.supported:
            return self
        required_boards = set(_QIANFAN_BOARDS)
        required_dimensions = set(_QIANFAN_DIMENSIONS)
        if (
            not self.ready_selector
            or not self.login_selector
            or not self.captcha_selector
            or {key for key, _ in self.board_selectors} != required_boards
            or {key for key, _ in self.active_board_selectors} != required_boards
            or {key for key, _ in self.dimension_selectors} != required_dimensions
            or {key for key, _ in self.active_dimension_selectors}
            != required_dimensions
            or {key for key, _ in self.response_endpoint_paths}
            != required_dimensions
            or {key for key, _ in self.board_request_mapping} != required_boards
            or any(not isinstance(path, str) or not path.startswith("/") for _, path in self.response_endpoint_paths)
            or any(isinstance(sort_by, bool) or not isinstance(sort_by, int) for _, sort_by in self.board_request_mapping)
            or isinstance(self.request_note_type, bool)
            or not isinstance(self.request_note_type, int)
            or isinstance(self.canonical_page_no, bool)
            or not isinstance(self.canonical_page_no, int)
            or self.canonical_page_no < 1
            or isinstance(self.canonical_page_size, bool)
            or not isinstance(self.canonical_page_size, int)
            or self.canonical_page_size < 1
        ):
            raise ValueError("Supported Qianfan selector profiles must verify every fixed scope.")
        return self


DEFAULT_QIANFAN_SELECTOR_PROFILE = QianfanSelectorProfile(
    version="qianfan-note-rank-live-v1",
    supported=True,
    ready_selector="div.d-tabs-header.d-clickable",
    login_selector='text="登录"',
    captcha_selector='text="验证码"',
    board_selectors=tuple(
        (board, f'div.d-tabs-header.d-clickable:has-text("{board}")')
        for board in _QIANFAN_BOARDS
    ),
    dimension_selectors=tuple(
        (dimension, f'button.d-button:has-text("{dimension}")')
        for dimension in _QIANFAN_DIMENSIONS
    ),
    active_board_selectors=tuple(
        (board, f'div.d-tabs-header.d-clickable.active:has-text("{board}")')
        for board in _QIANFAN_BOARDS
    ),
    active_dimension_selectors=tuple(
        (dimension, f'button.d-button.active:has-text("{dimension}")')
        for dimension in _QIANFAN_DIMENSIONS
    ),
)


class _OwnedPersistentPage:
    def __init__(self, page: Any, context: Any, playwright: Any) -> None:
        self._page = page
        self._context = context
        self._playwright = playwright

    def __getattr__(self, name: str) -> Any:
        return getattr(self._page, name)

    def close(self) -> None:
        try:
            self._context.close()
        finally:
            self._playwright.stop()


def persistent_qianfan_page_factory(
    *, browser_executable: str | None, user_data_dir: Path | None
) -> Callable[[], Any]:
    """Build a settings-owned persistent Playwright page factory."""

    def open_page() -> Any:
        if not browser_executable or not Path(browser_executable).is_file():
            raise RuntimeError("Qianfan browser executable is not configured or unavailable.")
        if user_data_dir is None or not user_data_dir.is_dir():
            raise RuntimeError("Qianfan persistent browser profile is not configured or unavailable.")
        from playwright.sync_api import sync_playwright

        playwright = sync_playwright().start()
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                executable_path=browser_executable,
                headless=False,
            )
            page = context.pages[0] if context.pages else context.new_page()
            return _OwnedPersistentPage(page, context, playwright)
        except Exception:
            playwright.stop()
            raise

    return open_page


class QianfanCaptureOutcome(BaseModel):
    status: Literal["captured", "needs_human"]
    reason: Literal[
        "login_required",
        "layout_changed",
        "navigation_failed",
        "response_not_observed",
        "response_unusable",
        "captcha_required",
        "scope_unverified",
        "selector_profile_unverified",
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


class QianfanCollectionCancelled(RuntimeError):
    """Raised before persistence when service shutdown revoked adapter admission."""


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
        finalize_job: bool = True,
    ) -> None:
        self._page_factory = page_factory
        self._job_service = job_service
        self._runtime_dir = runtime_dir.resolve() if runtime_dir is not None else None
        self._timeout_seconds = max(timeout_seconds, 0)
        self._poll_interval = max(poll_interval, 0)
        self._sleep = sleep
        self._monotonic = monotonic
        self._owns_page = owns_page
        self._finalize_job_enabled = finalize_job

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
            if self._finalize_job_enabled:
                self._finalize_job(job_id, result)
            return result
        except Exception as error:
            if self._finalize_job_enabled:
                self._fail_running_job(job_id, category=f"{stage}_failed", error=error)
            raise

    def collect_scope(
        self,
        request: CollectionRequest,
        *,
        board: BoardName,
        dimension: DimensionName,
        selector_profile: QianfanSelectorProfile = DEFAULT_QIANFAN_SELECTOR_PROFILE,
    ) -> CollectionResult:
        """Collect one fixed scope only after UI and response scope are both proven."""
        job_id = self._validated_job_id(request)
        is_cancelled = request.parameters.get("is_cancelled")
        stage = "profile_validation"
        try:
            if not selector_profile.supported:
                outcome = QianfanCaptureOutcome(
                    status="needs_human",
                    reason="selector_profile_unverified",
                    raw_evidence={
                        "selector_profile_version": selector_profile.version,
                        "requested_scope": {"board": board, "dimension": dimension},
                        "profile_supported": False,
                    },
                )
            else:
                stage = "scope_capture"
                outcome = self.capture_scope_page(
                    board=board,
                    dimension=dimension,
                    selector_profile=selector_profile,
                )
            stage = "evidence_persistence"
            if callable(is_cancelled) and is_cancelled():
                raise QianfanCollectionCancelled("Qianfan collection was cancelled.")
            artifact_paths = self._persist_job_evidence(
                job_id,
                outcome,
                board=board,
                dimension=dimension,
                selector_profile_version=selector_profile.version,
                is_cancelled=is_cancelled if callable(is_cancelled) else None,
            )
            if outcome.status == "needs_human":
                result = _needs_human_result(
                    request=request,
                    detail=outcome.reason or "needs_human",
                    artifact_paths=artifact_paths,
                    raw_evidence=outcome.raw_evidence,
                )
            else:
                scoped_evidence = dict(outcome.raw_evidence)
                scoped_evidence["responses"] = list(
                    outcome.raw_evidence.get("verified_responses", [])
                )
                stage = "normalization"
                normalized = _normalized_items(scoped_evidence)
                stage = "result_validation"
                if not normalized.valid_response_observed:
                    result = _needs_human_result(
                        request=request,
                        detail="response_unusable",
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
            if self._finalize_job_enabled:
                stage = "job_finalization"
                self._finalize_job(job_id, result)
            return result
        except Exception as error:
            if self._finalize_job_enabled:
                self._fail_running_job(job_id, category=f"{stage}_failed", error=error)
            raise

    def capture_scope_page(
        self,
        *,
        board: BoardName,
        dimension: DimensionName,
        selector_profile: QianfanSelectorProfile,
    ) -> QianfanCaptureOutcome:
        if not selector_profile.supported:
            raise ValueError("Unsupported selector profiles cannot open a browser page.")
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
            outcome = self._observe_scope_page(
                page,
                responses,
                capture_errors,
                board=board,
                dimension=dimension,
                selector_profile=selector_profile,
            )
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

    def _observe_scope_page(
        self,
        page: Any,
        responses: list[dict[str, Any]],
        capture_errors: list[dict[str, str]],
        *,
        board: BoardName,
        dimension: DimensionName,
        selector_profile: QianfanSelectorProfile,
    ) -> QianfanCaptureOutcome:
        base_evidence = {
            "selector_profile_version": selector_profile.version,
            "requested_scope": {"board": board, "dimension": dimension},
            "profile_supported": True,
        }
        try:
            page.goto(QIANFAN_RANK_URL, wait_until="domcontentloaded")
        except Exception as error:
            capture_errors.append(_capture_error("navigation", error))
            return QianfanCaptureOutcome(
                status="needs_human",
                reason="navigation_failed",
                raw_evidence={
                    **_raw_page_evidence(page, responses, capture_errors=capture_errors),
                    **base_evidence,
                },
            )
        deadline = self._monotonic() + self._timeout_seconds
        while True:
            try:
                if page.locator(str(selector_profile.login_selector)).count() > 0:
                    reason = "login_required"
                elif page.locator(str(selector_profile.captcha_selector)).count() > 0:
                    reason = "captcha_required"
                elif page.locator(str(selector_profile.ready_selector)).count() > 0:
                    break
                else:
                    reason = None
            except Exception as error:
                capture_errors.append(_capture_error("scope_readiness", error))
                return QianfanCaptureOutcome(
                    status="needs_human",
                    reason="layout_changed",
                    raw_evidence={
                        **_raw_page_evidence(page, responses, capture_errors=capture_errors),
                        **base_evidence,
                    },
                )
            if reason is not None:
                return QianfanCaptureOutcome(
                    status="needs_human",
                    reason=reason,
                    raw_evidence={**_raw_page_evidence(page, responses), **base_evidence},
                )
            if self._monotonic() >= deadline:
                return QianfanCaptureOutcome(
                    status="needs_human",
                    reason="layout_changed",
                    raw_evidence={**_raw_page_evidence(page, responses), **base_evidence},
                )
            self._sleep(self._poll_interval)
        try:
            page.locator(dict(selector_profile.board_selectors)[board]).click()
            page.locator(dict(selector_profile.dimension_selectors)[dimension]).click()
        except Exception as error:
            capture_errors.append(_capture_error("scope_selection", error))
            return QianfanCaptureOutcome(
                status="needs_human",
                reason="layout_changed",
                raw_evidence={
                    **_raw_page_evidence(page, responses, capture_errors=capture_errors),
                    **base_evidence,
                },
            )
        while True:
            try:
                active = (
                    page.locator(dict(selector_profile.active_board_selectors)[board]).count()
                    > 0
                    and page.locator(
                        dict(selector_profile.active_dimension_selectors)[dimension]
                    ).count()
                    > 0
                )
            except Exception as error:
                capture_errors.append(_capture_error("active_scope", error))
                active = False
            verified_responses = [
                response
                for response in responses
                if _response_matches_scope(
                    response,
                    board=board,
                    dimension=dimension,
                    selector_profile=selector_profile,
                )
            ]
            if active and verified_responses:
                return QianfanCaptureOutcome(
                    status="captured",
                    raw_evidence={
                        **_raw_page_evidence(page, responses, capture_errors=capture_errors),
                        **base_evidence,
                        "verified_scope": {"board": board, "dimension": dimension},
                        "verified_responses": verified_responses,
                    },
                )
            if self._monotonic() >= deadline:
                return QianfanCaptureOutcome(
                    status="needs_human",
                    reason="scope_unverified",
                    raw_evidence={
                        **_raw_page_evidence(page, responses, capture_errors=capture_errors),
                        **base_evidence,
                        "ui_scope_active": active,
                    },
                )
            self._sleep(self._poll_interval)

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
        self,
        job_id: str | None,
        outcome: QianfanCaptureOutcome,
        *,
        board: BoardName | None = None,
        dimension: DimensionName | None = None,
        selector_profile_version: str | None = None,
        is_cancelled: Callable[[], bool] | None = None,
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
        if is_cancelled is not None and is_cancelled():
            raise QianfanCollectionCancelled("Qianfan collection was cancelled.")
        encoded = json.dumps(
            outcome.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        absolute_path.write_bytes(encoded)
        if is_cancelled is not None and is_cancelled():
            raise QianfanCollectionCancelled("Qianfan collection was cancelled.")
        digest = sha256(encoded).hexdigest()
        metadata: dict[str, Any] = {
            "source_url": QIANFAN_RANK_URL,
            "capture_status": outcome.status,
            "reason": outcome.reason,
            "sha256": digest,
        }
        if board is not None and dimension is not None:
            metadata.update(
                {
                    "board": board,
                    "dimension": dimension,
                    "selector_profile_version": selector_profile_version,
                }
            )
        artifact = self._job_service.attach_artifact(
            job_id,
            kind="qianfan_raw_capture",
            path=relative_path.as_posix(),
            metadata=metadata,
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
    record: dict[str, Any] = {
        "url": _without_query_or_fragment(url),
        "status": int(response.status),
    }
    request_scope = _record_request_scope(getattr(response, "request", None))
    if request_scope is not None:
        record["request"] = request_scope
    try:
        record["body"] = _redact_credential_like_fields(response.json())
    except Exception as error:
        record["capture_error"] = type(error).__name__
        body_length = _response_body_length(response)
        if body_length is not None:
            record["body_length"] = body_length
    sink.append(_redact_credential_like_fields(record))


def _response_body_length(response: Any) -> int | None:
    try:
        return len(response.text())
    except Exception:
        try:
            return len(bytes(response.body()))
        except Exception:
            return None


def _record_request_scope(request: Any) -> dict[str, Any] | None:
    if request is None:
        return None
    method = getattr(request, "method", None)
    request_url = getattr(request, "url", None)
    if not isinstance(method, str) or not isinstance(request_url, str):
        return None
    scope: dict[str, Any] = {"method": method, "path": _url_path(request_url)}
    try:
        raw_post_data = request.post_data_json()
    except Exception:
        raw_post_data = None
    if isinstance(raw_post_data, dict):
        for key in ("sortBy", "noteType", "pageNo", "pageSize"):
            value = raw_post_data.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                scope[key] = value
    return scope


def _url_path(url: str) -> str:
    try:
        return urlsplit(url).path
    except (TypeError, ValueError, UnicodeError):
        return ""


def _without_query_or_fragment(url: str) -> str:
    try:
        parsed = urlsplit(url)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    except (TypeError, ValueError, UnicodeError):
        return ""


def _redact_credential_like_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _redact_credential_like_fields(child)
            for key, child in value.items()
            if not _is_credential_like_key(key)
        }
    if isinstance(value, list):
        return [_redact_credential_like_fields(child) for child in value]
    if isinstance(value, tuple):
        return [_redact_credential_like_fields(child) for child in value]
    return value


def _is_credential_like_key(key: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
    return normalized in {
        "xsectoken",
        "token",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "cookie",
        "cookies",
        "authorization",
        "setcookie",
        "password",
        "passwd",
        "secret",
        "credential",
        "credentials",
        "session",
        "sessionid",
    }


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
    return _redact_credential_like_fields(evidence)


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
        account_response = _is_account_ranking_response(response)
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
                item = (
                    _normalized_account_item(raw_item, item_evidence)
                    if account_response
                    else _normalized_content_item(raw_item, item_evidence)
                )
            except _RankItemUnusable as error:
                rejected_items.append(
                    RejectedCollectionItem(
                        reference=reference,
                        reason=error.reason,
                        raw_evidence=item_evidence,
                    )
                )
                continue
            if item.id in chosen:
                rejected_items.append(
                    RejectedCollectionItem(
                        reference=reference,
                        reason="duplicate_identity",
                        raw_evidence=item_evidence,
                    )
                )
                continue
            chosen[item.id] = item
    return _NormalizedRankings(
        items=[chosen[item_id] for item_id in sorted(chosen)],
        rejected_items=rejected_items,
        valid_response_observed=valid_response_observed,
    )


def _is_account_ranking_response(response: dict[str, Any]) -> bool:
    request = response.get("request")
    return isinstance(request, dict) and request.get("path") == _ACCOUNT_RANK_RESPONSE_PATH


def _normalized_content_item(
    raw_item: dict[str, Any], item_evidence: dict[str, Any]
) -> CollectionItem:
    item_id, note_id, canonical_note_url = _item_identity(raw_item)
    source_url = (
        f"https://www.xiaohongshu.com/explore/{quote(note_id)}"
        if note_id
        else canonical_note_url or QIANFAN_RANK_URL
    )
    return CollectionItem(
        id=item_id,
        kind="rank_item",
        source_url=source_url,
        raw_evidence=_redact_credential_like_fields(item_evidence),
        data={
            "rank": raw_item.get("rank"),
            "title": _first_present(raw_item, "noteTitle", "title", "note_title"),
            "author_name": _first_present(
                raw_item, "userNickname", "authorName", "author_name"
            ),
            "publish_date": _first_present(
                raw_item,
                "publishTime",
                "publishDate",
                "publish_time",
                "publish_date",
            ),
            "read_range": _first_present(
                raw_item,
                "readNumRange",
                "noteReadNumRange",
                "readRange",
                "read_range",
            ),
            "click_rate_range": _first_present(
                raw_item,
                "clickRateRange",
                "noteClickRateRange",
                "click_rate_range",
            ),
            "pay_rate_range": _first_present(
                raw_item,
                "payRateRange",
                "notePayRateRange",
                "pay_rate_range",
            ),
            "gmv_range": _first_present(raw_item, "gmvRange", "noteGmvRange", "gmv_range"),
            "note_id": note_id or None,
            "user_id": raw_item.get("userId") or raw_item.get("user_id"),
        },
    )


def _normalized_account_item(
    raw_item: dict[str, Any], item_evidence: dict[str, Any]
) -> CollectionItem:
    user_id = _safe_identity_token(_first_present(raw_item, "userId", "user_id"))
    if user_id is None:
        raise _RankItemUnusable("identity_insufficient")
    return CollectionItem(
        id=f"account:{user_id}",
        kind="rank_item",
        source_url=f"https://www.xiaohongshu.com/user/profile/{quote(user_id)}",
        raw_evidence=_redact_credential_like_fields(item_evidence),
        data={
            "rank": raw_item.get("rank"),
            "author_name": _first_present(raw_item, "userNickname", "authorName", "author_name"),
            "user_id": user_id,
        },
    )


def _response_matches_scope(
    response: dict[str, Any],
    *,
    board: BoardName,
    dimension: DimensionName,
    selector_profile: QianfanSelectorProfile,
) -> bool:
    body = response.get("body")
    request = response.get("request")
    if (
        not isinstance(body, dict)
        or not _successful_response(response, body)
        or not isinstance(request, dict)
    ):
        return False
    return request == {
        "method": "POST",
        "path": dict(selector_profile.response_endpoint_paths)[dimension],
        "sortBy": dict(selector_profile.board_request_mapping)[board],
        "noteType": selector_profile.request_note_type,
        "pageNo": selector_profile.canonical_page_no,
        "pageSize": selector_profile.canonical_page_size,
    }


def _nested_value(value: Any, path: tuple[str, ...]) -> Any:
    current = value
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


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
