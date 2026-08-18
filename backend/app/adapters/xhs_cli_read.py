"""Strict, read-only adapter for a locally configured ``xhs`` CLI."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.app.adapters.contracts import (
    CollectionItem,
    CollectionRequest,
    CollectionResult,
    MissingCollectionItem,
    RejectedCollectionItem,
)


_XHS_PUBLIC_ORIGIN = "https://www.xiaohongshu.com"
_ALLOWED_COMMANDS = frozenset({"status", "whoami", "search", "read", "user", "user-posts"})
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,500}")
_SENSITIVE_KEY = re.compile(
    r"(?:cookie|token|credential|authorization|password|secret|session|api[_-]?key)",
    re.IGNORECASE,
)
_HUMAN_FAILURES = frozenset({
    "login_required",
    "captcha_required",
    "rate_limited",
    "account_visibility_restricted",
    "response_unusable",
})
_ENVELOPE_MESSAGE_FIELDS = frozenset({"message", "msg", "detail", "reason"})
_ENVELOPE_STATUS_FIELDS = frozenset({"status", "code"})
_SUCCESS_STATUS_VALUES = frozenset({"0", "200", "ok", "success", "succeeded", "true"})


class XhsCliSearchRequest(BaseModel):
    """Only search text is accepted from a collection request."""

    model_config = ConfigDict(extra="forbid", strict=True)

    keyword: str = Field(min_length=1, max_length=500)
    job_id: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("keyword")
    @classmethod
    def reject_unsafe_positional_value(cls, value: str) -> str:
        return _safe_cli_positional(value)


class XhsCliAccountRequest(BaseModel):
    """Only an account identity is accepted from a collection request."""

    model_config = ConfigDict(extra="forbid", strict=True)

    user_id: str = Field(min_length=1, max_length=500)
    job_id: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("user_id")
    @classmethod
    def reject_unsafe_positional_value(cls, value: str) -> str:
        return _safe_cli_positional(value)


class XhsCliReadError(RuntimeError):
    """A persistence-safe external-process failure category."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


Runner = Callable[..., subprocess.CompletedProcess[bytes]]


def decode_bounded_json(
    completed: subprocess.CompletedProcess[bytes], *, max_stdout_bytes: int = 5 * 1024 * 1024
) -> dict[str, Any]:
    """Reject failed, oversized, non-UTF-8 and non-object CLI output without retaining it."""
    stdout = completed.stdout
    stderr = completed.stderr
    if not isinstance(stdout, bytes) or not isinstance(stderr, bytes):
        raise XhsCliReadError("malformed_output")
    if len(stdout) > max_stdout_bytes or len(stderr) > max_stdout_bytes:
        raise XhsCliReadError("output_too_large")
    if completed.returncode != 0:
        raise XhsCliReadError(_failure_category(stdout, stderr))
    try:
        decoded = stdout.decode("utf-8", errors="strict")
        payload = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise XhsCliReadError("malformed_output") from error
    if not isinstance(payload, dict):
        raise XhsCliReadError("malformed_output")
    category = _payload_human_failure_category(payload)
    if category is not None:
        raise XhsCliReadError(category)
    return _redact_credentials(payload)


class XhsCliReadAdapter:
    """Read public account/note observations through a settings-owned executable only."""

    capabilities = frozenset({"search_notes", "fetch_account"})

    def __init__(
        self,
        *,
        executable: Path | str,
        timeout_seconds: float = 20.0,
        runner: Runner = subprocess.run,
        max_stdout_bytes: int = 5 * 1024 * 1024,
    ) -> None:
        executable_text = str(executable)
        if not executable_text.strip():
            raise ValueError("xhs executable must be configured by trusted Settings.")
        if timeout_seconds <= 0:
            raise ValueError("xhs CLI timeout must be positive.")
        if max_stdout_bytes < 1:
            raise ValueError("xhs CLI stdout limit must be positive.")
        self._executable = executable_text
        self._timeout_seconds = timeout_seconds
        self._runner = runner
        self._max_stdout_bytes = max_stdout_bytes

    @classmethod
    def from_settings(cls, settings: Any, *, runner: Runner = subprocess.run) -> "XhsCliReadAdapter":
        """Construct the executable boundary from application Settings, never request input."""
        return cls(
            executable=settings.xhs_cli_executable,
            timeout_seconds=settings.xhs_cli_timeout_seconds,
            runner=runner,
        )

    def search_notes(self, request: CollectionRequest) -> CollectionResult:
        parameters = self._search_parameters(request)
        try:
            payload = self._invoke(["search", parameters.keyword])
        except XhsCliReadError as error:
            return _failed_result(request, error.category)
        return _normalize_notes(payload, request=request, source="search")

    def fetch_account(self, request: CollectionRequest) -> CollectionResult:
        parameters = self._account_parameters(request)
        try:
            profile = self._invoke(["user", parameters.user_id])
            notes = self._invoke(["user-posts", parameters.user_id])
        except XhsCliReadError as error:
            return _failed_result(request, error.category)
        return _normalize_account(
            profile=profile,
            notes=notes,
            request=request,
            requested_user_id=parameters.user_id,
        )

    def _search_parameters(self, request: CollectionRequest) -> XhsCliSearchRequest:
        if request.capability != "search_notes":
            raise ValueError("search_notes requires the search_notes capability.")
        return XhsCliSearchRequest.model_validate(request.parameters)

    def _account_parameters(self, request: CollectionRequest) -> XhsCliAccountRequest:
        if request.capability != "fetch_account":
            raise ValueError("fetch_account requires the fetch_account capability.")
        return XhsCliAccountRequest.model_validate(request.parameters)

    def _invoke(self, command: Sequence[str]) -> dict[str, Any]:
        if not command or command[0] not in _ALLOWED_COMMANDS:
            raise ValueError("xhs command is not in the read-only allowlist.")
        argv = [self._executable, *command, "--json"]
        try:
            completed = self._runner(
                argv,
                shell=False,
                capture_output=True,
                timeout=self._timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise XhsCliReadError("timeout") from error
        except OSError as error:
            raise XhsCliReadError("executable_unavailable") from error
        return decode_bounded_json(completed, max_stdout_bytes=self._max_stdout_bytes)


def _normalize_account(
    *,
    profile: dict[str, Any],
    notes: dict[str, Any],
    request: CollectionRequest,
    requested_user_id: str,
) -> CollectionResult:
    items: list[CollectionItem] = []
    rejected: list[RejectedCollectionItem] = []
    profile_row = _profile_row(profile)
    profile_reference = "xhs_cli:user:profile"
    profile_user_id = _identity(profile_row, "id", "user_id", "userId") if profile_row else None
    requested_identity = _safe_token(requested_user_id)
    if profile_user_id is None:
        rejected.append(
            RejectedCollectionItem(
                reference=profile_reference,
                reason="profile_identity_missing",
                raw_evidence={"response": profile},
            )
        )
    elif requested_identity is None or profile_user_id != requested_identity:
        rejected.append(
            RejectedCollectionItem(
                reference=profile_reference,
                reason="profile_identity_mismatch",
                raw_evidence={"response": profile},
            )
        )
    else:
        items.append(
            CollectionItem(
                id=f"profile:{profile_user_id}",
                kind="profile",
                source_url=f"{_XHS_PUBLIC_ORIGIN}/user/profile/{quote(profile_user_id)}",
                raw_evidence={"response": profile, "profile": profile_row},
                data=_public_data(profile_row, user_id=profile_user_id),
            )
        )
    note_items, note_rejected = _normalize_note_rows(notes, source="user-posts")
    verified_note_items: list[CollectionItem] = []
    for note in note_items:
        note_owner_id = note.data.get("user_id")
        if profile_user_id is not None and requested_identity == profile_user_id:
            if note_owner_id is None:
                note.data["user_id"] = profile_user_id
                verified_note_items.append(note)
            elif note_owner_id == profile_user_id:
                verified_note_items.append(note)
            else:
                note_rejected.append(
                    RejectedCollectionItem(
                        reference=f"xhs_cli:user-posts:{note.id}",
                        reason="note_owner_mismatch",
                        raw_evidence=note.raw_evidence,
                    )
                )
        else:
            verified_note_items.append(note)
    items.extend(verified_note_items)
    rejected.extend(note_rejected)
    return _accounted_result(request=request, items=items, rejected_items=rejected)


def _normalize_notes(
    payload: dict[str, Any], *, request: CollectionRequest, source: str) -> CollectionResult:
    items, rejected = _normalize_note_rows(payload, source=source)
    return _accounted_result(request=request, items=items, rejected_items=rejected)


def _normalize_note_rows(
    payload: dict[str, Any], *, source: str
) -> tuple[list[CollectionItem], list[RejectedCollectionItem]]:
    rows = _note_rows(payload)
    if rows is None:
        return [], [
            RejectedCollectionItem(
                reference=f"xhs_cli:{source}:response",
                reason="notes_not_list",
                raw_evidence={"response": payload},
            )
        ]
    chosen: dict[str, CollectionItem] = {}
    rejected: list[RejectedCollectionItem] = []
    for index, row in enumerate(rows, start=1):
        reference = f"xhs_cli:{source}:note:{index}"
        evidence = {"response": payload, "row": _redact_credentials(row)}
        if not isinstance(row, dict):
            rejected.append(
                RejectedCollectionItem(reference=reference, reason="row_not_object", raw_evidence=evidence)
            )
            continue
        note_id = _identity(row, "id", "note_id", "noteId")
        if note_id is None:
            rejected.append(
                RejectedCollectionItem(reference=reference, reason="note_identity_missing", raw_evidence=evidence)
            )
            continue
        item = CollectionItem(
            id=f"note:{note_id}",
            kind="note",
            source_url=f"{_XHS_PUBLIC_ORIGIN}/explore/{quote(note_id)}",
            raw_evidence=evidence,
            data=_public_data(row, note_id=note_id),
        )
        if item.id in chosen:
            rejected.append(
                RejectedCollectionItem(
                    reference=reference,
                    reason="duplicate_source_url",
                    raw_evidence=evidence,
                )
            )
            continue
        chosen[item.id] = item
    return [chosen[item_id] for item_id in sorted(chosen)], rejected


def _profile_row(payload: dict[str, Any]) -> dict[str, Any] | None:
    for candidate in (payload.get("user"), _nested(payload, "data", "user"), payload.get("data"), payload):
        if isinstance(candidate, dict):
            return _redact_credentials(candidate)
    return None


def _note_rows(payload: dict[str, Any]) -> list[Any] | None:
    for candidate in (
        payload.get("notes"),
        payload.get("items"),
        _nested(payload, "data", "notes"),
        _nested(payload, "data", "items"),
        payload.get("data"),
    ):
        if isinstance(candidate, list):
            return candidate
    return None


def _nested(value: dict[str, Any], *path: str) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _identity(row: dict[str, Any] | None, *keys: str) -> str | None:
    if row is None:
        return None
    for key in keys:
        candidate = _safe_token(row.get(key))
        if candidate is not None:
            return candidate
    return None


def _safe_token(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    return text if _SAFE_TOKEN.fullmatch(text) is not None else None


def _safe_cli_positional(value: str) -> str:
    if not value.strip() or value.startswith("-") or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("xhs CLI positional values cannot be options or control characters.")
    return value


def _public_data(row: dict[str, Any], **identity: str) -> dict[str, Any]:
    allowed = {
        "title", "desc", "description", "nickname", "user_id", "userId", "author_name",
        "authorName", "liked_count", "likedCount", "liked_count", "collect_count", "collectCount",
        "comment_count", "commentCount", "publish_time", "publishTime", "type", "cover",
    }
    data = {key: _redact_credentials(value) for key, value in row.items() if key in allowed}
    data.update(identity)
    return data


def _accounted_result(
    *, request: CollectionRequest, items: list[CollectionItem], rejected_items: list[RejectedCollectionItem]
) -> CollectionResult:
    duplicate_count = sum(item.reason == "duplicate_source_url" for item in rejected_items)
    has_nonduplicate_rejection = len(rejected_items) != duplicate_count
    identity_observed = len(items) + len(rejected_items) - duplicate_count
    expected = request.expected_count
    if expected is None:
        return CollectionResult(
            status="needs_human" if rejected_items else "partial",
            detail="response_unusable" if rejected_items else "expected_count_unknown",
            items=items,
            rejected_items=rejected_items,
            expected_count_known=False,
            expected_count=None,
            succeeded_count=len(items),
            observed_count=len(items) + len(rejected_items),
            missing_items=[],
            overflow_count=0,
            complete=False,
        )
    overflow = max(identity_observed - expected, 0)
    missing = max(expected - identity_observed, 0)
    missing_items = [
        MissingCollectionItem(
            reference=f"xhs_cli:{request.capability}:missing:{index + 1}",
            reason="expected_item_not_observed",
            raw_evidence={"capability": request.capability},
        )
        for index in range(missing)
    ]
    if overflow:
        status, detail, complete = "failed", "observed_count_exceeds_expected", False
    elif missing:
        status = "needs_human" if has_nonduplicate_rejection else "partial"
        detail, complete = (
            "response_unusable" if has_nonduplicate_rejection else "expected_items_missing"
        ), False
    elif has_nonduplicate_rejection:
        status, detail, complete = "needs_human", "response_unusable", False
    else:
        status, detail, complete = "succeeded", None, True
    return CollectionResult(
        status=status,
        detail=detail,
        items=items,
        rejected_items=rejected_items,
        expected_count_known=True,
        expected_count=expected,
        succeeded_count=len(items),
        observed_count=len(items) + len(rejected_items),
        missing_items=missing_items,
        overflow_count=overflow,
        complete=complete,
    )


def _failed_result(request: CollectionRequest, category: str) -> CollectionResult:
    expected = request.expected_count
    missing_items = [
        MissingCollectionItem(
            reference=f"xhs_cli:{request.capability}:missing:{index + 1}",
            reason=category,
            raw_evidence={"category": category},
        )
        for index in range(expected or 0)
    ]
    return CollectionResult(
        status="needs_human" if category in _HUMAN_FAILURES else "failed",
        detail=category,
        items=[],
        rejected_items=[],
        expected_count_known=expected is not None,
        expected_count=expected,
        succeeded_count=0,
        observed_count=0,
        missing_items=missing_items,
        overflow_count=0,
        complete=False,
    )


def _failure_category(stdout: bytes, stderr: bytes) -> str:
    return _failure_category_from_text(
        (stdout + b"\n" + stderr).decode("utf-8", errors="ignore")
    )


def _failure_category_from_text(value: str) -> str:
    text = value.casefold()
    if any(marker in text for marker in ("captcha", "verify", "需要验证", "验证码", "安全验证")):
        return "captcha_required"
    if any(marker in text for marker in ("rate limit", "too many", "429", "请求频繁", "操作频繁")):
        return "rate_limited"
    if any(marker in text for marker in ("login", "sign in", "auth", "请先登录", "登录已过期", "登录过期", "未登录")):
        return "login_required"
    if any(marker in text for marker in ("private", "not visible", "forbidden", "访问受限", "无权访问", "权限不足")):
        return "account_visibility_restricted"
    return "cli_failed"


def _payload_human_failure_category(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    for key, child in value.items():
        normalized_key = str(key).casefold()
        if normalized_key in _ENVELOPE_MESSAGE_FIELDS and isinstance(child, str):
            category = _failure_category_from_text(child)
            if category in _HUMAN_FAILURES:
                return category
        if normalized_key in _ENVELOPE_STATUS_FIELDS and child not in (None, ""):
            category = _structured_status_category(child)
            if category is not None:
                return category
        if normalized_key == "error" and child not in (None, "", {}, []):
            category = _error_category(child)
            return category or "response_unusable"
    nested_data = value.get("data")
    if isinstance(nested_data, dict):
        return _payload_human_failure_category(nested_data)
    return None


def _structured_status_category(value: Any) -> str | None:
    if isinstance(value, bool):
        return "response_unusable"
    if isinstance(value, int):
        return {0: None, 200: None, 401: "login_required", 403: "account_visibility_restricted", 429: "rate_limited"}.get(value, "response_unusable")
    if isinstance(value, str):
        category = _failure_category_from_text(value)
        if category in _HUMAN_FAILURES:
            return category
        return None if value.strip().casefold() in _SUCCESS_STATUS_VALUES else "response_unusable"
    return "response_unusable"


def _error_category(value: Any) -> str | None:
    if isinstance(value, str):
        category = _failure_category_from_text(value)
        return category if category in _HUMAN_FAILURES else "response_unusable"
    if isinstance(value, dict):
        return _payload_human_failure_category(value)
    return "response_unusable"


def _redact_credentials(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[redacted]" if _SENSITIVE_KEY.search(str(key)) else _redact_credentials(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_credentials(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_credentials(item) for item in value]
    return value
