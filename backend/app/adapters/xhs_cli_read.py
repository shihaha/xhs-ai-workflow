"""Strict, read-only adapter for a locally configured ``xhs`` CLI."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from typing import Any, BinaryIO
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.app.adapters.contracts import (
    CollectionItem,
    CollectionRequest,
    CollectionResult,
    MissingCollectionItem,
    RejectedCollectionItem,
)
from backend.app.features.xhs.ownership import OwnerIdentityError, canonical_owner_id
from backend.app.features.xhs.redaction import redact_credentials


_XHS_PUBLIC_ORIGIN = "https://www.xiaohongshu.com"
_ALLOWED_COMMANDS = frozenset({"status", "whoami", "search", "read", "user", "user-posts"})
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,500}")
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
_REQUIRED_EXTERNAL_COOKIES = frozenset({"a1", "web_session"})
_MAX_STATE_FILE_BYTES = 1024 * 1024
_MAX_STATUS_BYTES = 64 * 1024
_ENV_PASSTHROUGH = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SystemRoot",
    "WINDIR",
    "COMSPEC",
    "SYSTEMDRIVE",
)
JsonPayload = dict[str, Any] | list[Any]


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


def _run_bounded_process(
    argv: Sequence[str],
    *,
    shell: bool,
    cwd: Path | str,
    env: dict[str, str],
    timeout: float,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
) -> subprocess.CompletedProcess[bytes]:
    """Capture both pipes with hard in-flight bounds and always reap the child."""
    if shell:
        raise ValueError("The XHS process boundary never permits a shell.")
    if timeout <= 0 or max_stdout_bytes < 1 or max_stderr_bytes < 1:
        raise ValueError("The XHS process bounds must be positive.")
    process = subprocess.Popen(
        list(argv),
        shell=False,
        cwd=str(cwd),
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None and process.stderr is not None
    overflow = Event()
    stdout = bytearray()
    stderr = bytearray()
    reader_errors: list[BaseException] = []

    def read_bounded(stream: BinaryIO, target: bytearray, limit: int) -> None:
        try:
            while not overflow.is_set():
                chunk = stream.read(64 * 1024)
                if not chunk:
                    return
                remaining = limit - len(target)
                if remaining > 0:
                    target.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    overflow.set()
                    return
        except (OSError, ValueError) as error:
            reader_errors.append(error)

    readers = (
        Thread(
            target=read_bounded,
            args=(process.stdout, stdout, max_stdout_bytes),
            name="xhs-cli-stdout",
            daemon=True,
        ),
        Thread(
            target=read_bounded,
            args=(process.stderr, stderr, max_stderr_bytes),
            name="xhs-cli-stderr",
            daemon=True,
        ),
    )
    for reader in readers:
        reader.start()

    deadline = monotonic() + timeout
    timed_out = False
    cleanup_failed = False
    try:
        while process.poll() is None:
            if overflow.is_set():
                break
            if monotonic() >= deadline:
                timed_out = True
                break
            sleep(0.01)
        if not overflow.is_set() and not timed_out:
            process.wait()
    except OSError:
        cleanup_failed = True
    finally:
        if process.poll() is None and not _terminate_and_reap(process):
            cleanup_failed = True
        for reader in readers:
            reader.join(timeout=1.0)
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        for reader in readers:
            reader.join(timeout=1.0)

    if cleanup_failed or any(reader.is_alive() for reader in readers) or reader_errors:
        raise XhsCliReadError("process_cleanup_failed")
    if overflow.is_set():
        raise XhsCliReadError("output_too_large")
    if timed_out:
        raise XhsCliReadError("timeout")
    return subprocess.CompletedProcess(
        list(argv),
        int(process.returncode or 0),
        stdout=bytes(stdout),
        stderr=bytes(stderr),
    )


def _terminate_and_reap(process: subprocess.Popen[bytes]) -> bool:
    """Terminate, escalate to kill, and synchronously reap one owned child."""
    if process.poll() is not None:
        try:
            process.wait()
            return True
        except OSError:
            return False
    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=0.5)
        return True
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=1.0)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def decode_bounded_json(
    completed: subprocess.CompletedProcess[bytes],
    *,
    max_stdout_bytes: int = 5 * 1024 * 1024,
    max_stderr_bytes: int | None = None,
) -> JsonPayload:
    """Decode a bounded CLI JSON object or the pinned CLI's top-level list."""
    stdout = completed.stdout
    stderr = completed.stderr
    if not isinstance(stdout, bytes) or not isinstance(stderr, bytes):
        raise XhsCliReadError("malformed_output")
    stderr_limit = max_stdout_bytes if max_stderr_bytes is None else max_stderr_bytes
    if len(stdout) > max_stdout_bytes or len(stderr) > stderr_limit:
        raise XhsCliReadError("output_too_large")
    if completed.returncode != 0:
        raise XhsCliReadError(_failure_category(stdout, stderr))
    try:
        decoded = stdout.decode("utf-8", errors="strict")
        payload = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise XhsCliReadError("malformed_output") from error
    if not isinstance(payload, (dict, list)):
        raise XhsCliReadError("malformed_output")
    category = _payload_human_failure_category(payload)
    if category is not None:
        raise XhsCliReadError(category)
    return redact_credentials(payload)


def _isolated_child_environment(state_dir: Path) -> dict[str, str]:
    """Build an allowlisted environment whose browser/profile paths are app-owned."""
    isolated_paths = {
        "APPDATA": state_dir / "appdata" / "roaming",
        "LOCALAPPDATA": state_dir / "appdata" / "local",
        "TEMP": state_dir / "tmp",
        "TMP": state_dir / "tmp",
        "XDG_CONFIG_HOME": state_dir / "xdg" / "config",
        "XDG_CACHE_HOME": state_dir / "xdg" / "cache",
        "XDG_DATA_HOME": state_dir / "xdg" / "data",
    }
    for path in set(isolated_paths.values()):
        path.mkdir(parents=True, exist_ok=True)
    environment = {
        key: value
        for key in _ENV_PASSTHROUGH
        if (value := os.environ.get(key))
    }
    environment.update({
        "HOME": str(state_dir),
        "USERPROFILE": str(state_dir),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "NO_COLOR": "1",
        **{key: str(path) for key, path in isolated_paths.items()},
    })
    return environment


def _has_prepared_external_state(state_dir: Path) -> bool:
    """Require a bounded saved-cookie file before upstream can attempt any auth fallback."""
    cookie_file = state_dir / ".xhs-cli" / "cookies.json"
    try:
        resolved_cookie = cookie_file.resolve(strict=True)
        resolved_cookie.relative_to(state_dir)
        if not resolved_cookie.is_file() or resolved_cookie.stat().st_size > _MAX_STATE_FILE_BYTES:
            return False
        payload = json.loads(resolved_cookie.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or not isinstance(payload.get("cookies"), dict):
        return False
    cookies = payload["cookies"]
    return all(
        isinstance(cookies.get(name), str) and bool(cookies[name].strip())
        for name in _REQUIRED_EXTERNAL_COOKIES
    )


class XhsCliReadAdapter:
    """Read public account/note observations through a settings-owned executable only."""

    capabilities = frozenset({"search_notes", "fetch_account"})

    def __init__(
        self,
        *,
        executable: Path | str,
        state_dir: Path | str,
        timeout_seconds: float = 20.0,
        runner: Runner | None = None,
        max_stdout_bytes: int = 5 * 1024 * 1024,
    ) -> None:
        executable_text = str(executable)
        if not executable_text.strip():
            raise ValueError("xhs executable must be configured by trusted Settings.")
        if timeout_seconds <= 0:
            raise ValueError("xhs CLI timeout must be positive.")
        if max_stdout_bytes < 1:
            raise ValueError("xhs CLI stdout limit must be positive.")
        state_path = Path(state_dir).resolve()
        if state_path == Path.home().resolve():
            raise ValueError("xhs CLI state must not use the normal user profile.")
        self._executable = executable_text
        self._state_dir = state_path
        self._timeout_seconds = timeout_seconds
        self._runner = runner or _run_bounded_process
        self._max_stdout_bytes = max_stdout_bytes
        self._child_env = _isolated_child_environment(state_path)

    @classmethod
    def from_settings(
        cls, settings: Any, *, runner: Runner | None = None
    ) -> "XhsCliReadAdapter":
        """Construct the executable boundary from application Settings, never request input."""
        return cls(
            executable=settings.xhs_cli_executable,
            state_dir=settings.xhs_cli_state_dir,
            timeout_seconds=settings.xhs_cli_timeout_seconds,
            runner=runner,
            max_stdout_bytes=settings.xhs_cli_max_output_bytes,
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
            if not isinstance(profile, dict):
                raise XhsCliReadError("malformed_output")
            notes = self._invoke(["user-posts", parameters.user_id])
        except XhsCliReadError as error:
            return _failed_result(request, error.category)
        return _normalize_account(
            profile=profile,
            notes=notes,
            request=request,
            requested_user_id=parameters.user_id,
        )

    def probe_session_identity(self) -> str:
        """Prove a prepared saved-cookie session without invoking upstream login fallbacks."""
        completed = self._run_command(
            ["status"],
            json_output=False,
            max_output_bytes=min(self._max_stdout_bytes, _MAX_STATUS_BYTES),
        )
        if not isinstance(completed.stdout, bytes) or not isinstance(
            completed.stderr, bytes
        ):
            raise XhsCliReadError("malformed_output")
        status_limit = min(self._max_stdout_bytes, _MAX_STATUS_BYTES)
        if len(completed.stdout) > status_limit or len(completed.stderr) > status_limit:
            raise XhsCliReadError("output_too_large")
        if completed.returncode != 0:
            raise XhsCliReadError(_failure_category(completed.stdout, completed.stderr))
        try:
            status = (completed.stdout + b"\n" + completed.stderr).decode(
                "utf-8", errors="strict"
            ).casefold()
        except UnicodeDecodeError as error:
            raise XhsCliReadError("malformed_output") from error
        if "not logged in" in status or "logged in" not in status:
            raise XhsCliReadError("login_required")

        payload = self._invoke(["whoami"])
        if not isinstance(payload, dict):
            raise XhsCliReadError("response_unusable")
        user_info = payload.get("userInfo")
        if isinstance(user_info, dict) and user_info.get("guest") is True:
            raise XhsCliReadError("login_required")
        profile = _profile_row(payload)
        if profile is None:
            raise XhsCliReadError("response_unusable")
        try:
            identity = canonical_owner_id(profile, include_record_id=True)
        except OwnerIdentityError as error:
            raise XhsCliReadError("response_unusable") from error
        if identity is None:
            raise XhsCliReadError("response_unusable")
        return identity

    def _search_parameters(self, request: CollectionRequest) -> XhsCliSearchRequest:
        if request.capability != "search_notes":
            raise ValueError("search_notes requires the search_notes capability.")
        return XhsCliSearchRequest.model_validate(request.parameters)

    def _account_parameters(self, request: CollectionRequest) -> XhsCliAccountRequest:
        if request.capability != "fetch_account":
            raise ValueError("fetch_account requires the fetch_account capability.")
        return XhsCliAccountRequest.model_validate(request.parameters)

    def _invoke(self, command: Sequence[str]) -> JsonPayload:
        completed = self._run_command(command, json_output=True)
        return decode_bounded_json(
            completed,
            max_stdout_bytes=self._max_stdout_bytes,
            max_stderr_bytes=self._max_stdout_bytes,
        )

    def _run_command(
        self,
        command: Sequence[str],
        *,
        json_output: bool,
        max_output_bytes: int | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        if not command or command[0] not in _ALLOWED_COMMANDS:
            raise ValueError("xhs command is not in the read-only allowlist.")
        if not _has_prepared_external_state(self._state_dir):
            raise XhsCliReadError("login_required")
        argv = [self._executable, *command]
        if json_output:
            argv.append("--json")
        output_limit = (
            self._max_stdout_bytes
            if max_output_bytes is None
            else max_output_bytes
        )
        try:
            return self._runner(
                argv,
                shell=False,
                cwd=self._state_dir,
                env=self._child_env,
                timeout=self._timeout_seconds,
                max_stdout_bytes=output_limit,
                max_stderr_bytes=output_limit,
            )
        except subprocess.TimeoutExpired as error:
            raise XhsCliReadError("timeout") from error
        except OSError as error:
            raise XhsCliReadError("executable_unavailable") from error


def _normalize_account(
    *,
    profile: dict[str, Any],
    notes: JsonPayload,
    request: CollectionRequest,
    requested_user_id: str,
) -> CollectionResult:
    items: list[CollectionItem] = []
    rejected: list[RejectedCollectionItem] = []
    profile_row = _profile_row(profile)
    profile_reference = "xhs_cli:user:profile"
    try:
        profile_user_id = (
            canonical_owner_id(profile_row, include_record_id=True)
            if profile_row else None
        )
    except OwnerIdentityError:
        profile_user_id = None
        rejected.append(
            RejectedCollectionItem(
                reference=profile_reference,
                reason="profile_identity_conflict",
                raw_evidence={"response": profile},
            )
        )
    requested_identity = _safe_token(requested_user_id)
    if profile_user_id is None:
        if not rejected:
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
                data=_profile_public_data(profile_row, user_id=profile_user_id),
            )
        )
    note_items, note_rejected = _normalize_note_rows(notes, source="user-posts")
    verified_note_items: list[CollectionItem] = []
    for note in note_items:
        try:
            note_owner_id = canonical_owner_id(note.data, note.raw_evidence)
        except OwnerIdentityError:
            note_rejected.append(
                RejectedCollectionItem(
                    reference=f"xhs_cli:user-posts:{note.id}",
                    reason="note_owner_mismatch",
                    raw_evidence=note.raw_evidence,
                )
            )
            continue
        if profile_user_id is not None and requested_identity == profile_user_id:
            if note_owner_id is None:
                note.data["user_id"] = profile_user_id
                verified_note_items.append(note)
            elif note_owner_id == profile_user_id:
                note.data.setdefault("user_id", profile_user_id)
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
    payload: JsonPayload, *, request: CollectionRequest, source: str
) -> CollectionResult:
    items, rejected = _normalize_note_rows(payload, source=source)
    return _accounted_result(request=request, items=items, rejected_items=rejected)


def _normalize_note_rows(
    payload: JsonPayload, *, source: str
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
        evidence = {"response": payload, "row": redact_credentials(row)}
        if not isinstance(row, dict):
            rejected.append(
                RejectedCollectionItem(reference=reference, reason="row_not_object", raw_evidence=evidence)
            )
            continue
        clean_row = redact_credentials(row)
        note_card = _note_card(clean_row)
        note_id = _consistent_identity(
            (clean_row, note_card), "id", "note_id", "noteId"
        )
        if note_id is None:
            rejected.append(
                RejectedCollectionItem(reference=reference, reason="note_identity_missing", raw_evidence=evidence)
            )
            continue
        try:
            owner_id = canonical_owner_id(clean_row, note_card)
        except OwnerIdentityError:
            rejected.append(
                RejectedCollectionItem(
                    reference=reference,
                    reason=(
                        "note_owner_conflict"
                        if source == "search"
                        else "note_owner_mismatch"
                    ),
                    raw_evidence=evidence,
                )
            )
            continue
        item = CollectionItem(
            id=f"note:{note_id}",
            kind="note",
            source_url=f"{_XHS_PUBLIC_ORIGIN}/explore/{quote(note_id)}",
            raw_evidence=evidence,
            data=_note_public_data(
                clean_row,
                note_card=note_card,
                note_id=note_id,
                owner_id=owner_id,
            ),
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
    clean = redact_credentials(payload)
    user_page = clean.get("userPageData")
    user_info = clean.get("userInfo")
    if isinstance(user_page, dict):
        basic = user_page.get("basicInfo", user_page.get("basic_info"))
        if isinstance(basic, dict):
            row = _normalized_profile_data(basic, user_info=user_info)
            _merge_profile_interactions(row, user_page.get("interactions"))
            return row or None
    for candidate in (
        clean.get("user"),
        _nested(clean, "data", "user"),
        clean.get("data"),
        clean,
    ):
        if isinstance(candidate, dict):
            row = _normalized_profile_data(candidate, user_info=user_info)
            return row or candidate
    return None


def _note_rows(payload: JsonPayload) -> list[Any] | None:
    if isinstance(payload, list):
        return payload
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


def _note_card(row: dict[str, Any]) -> dict[str, Any]:
    for key in ("noteCard", "note_card"):
        candidate = row.get(key)
        if isinstance(candidate, dict):
            return candidate
    return row


def _consistent_identity(
    sources: Sequence[dict[str, Any]], *keys: str
) -> str | None:
    identities = {
        identity
        for source in sources
        for key in keys
        if (identity := _safe_token(source.get(key))) is not None
    }
    if len(identities) != 1:
        return None
    return next(iter(identities))


def _normalized_profile_data(
    basic: dict[str, Any], *, user_info: Any
) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for source_key, target_key in (
        ("nickname", "nickname"),
        ("desc", "bio"),
        ("description", "bio"),
        ("redId", "red_id"),
        ("red_id", "red_id"),
    ):
        value = basic.get(source_key)
        if isinstance(value, str) and value and target_key not in row:
            row[target_key] = value
    basic_identity = _identity(basic, "userId", "user_id", "id")
    user_info_identity = (
        _identity(user_info, "userId", "user_id", "id")
        if isinstance(user_info, dict)
        else None
    )
    if basic_identity is not None:
        row["userId"] = basic_identity
    if user_info_identity is not None:
        if basic_identity is None:
            row["userId"] = user_info_identity
        elif user_info_identity != basic_identity:
            row["user_id"] = user_info_identity
    for key in ("user_id", "userId", "id"):
        if key in basic and key not in row:
            row[key] = basic[key]
    return row


def _merge_profile_interactions(row: dict[str, Any], interactions: Any) -> None:
    if not isinstance(interactions, list):
        return
    stat_names = {
        "fans": "followers_count",
        "粉丝": "followers_count",
        "follows": "following_count",
        "关注": "following_count",
        "interaction": "liked_count",
        "获赞与收藏": "liked_count",
    }
    for interaction in interactions:
        if not isinstance(interaction, dict):
            continue
        name = interaction.get("name", interaction.get("type"))
        target = stat_names.get(str(name).strip().casefold()) if name is not None else None
        count = _public_count(interaction.get("count", interaction.get("value")))
        if target is not None and count is not None:
            row[target] = count


def _note_public_data(
    row: dict[str, Any],
    *,
    note_card: dict[str, Any],
    note_id: str,
    owner_id: str | None,
) -> dict[str, Any]:
    data = _public_data(row, note_id=note_id)
    field_sources = (
        ("title", ("displayTitle", "display_title", "title")),
        ("description", ("desc", "description", "summary")),
        ("publish_time", ("publishTime", "publish_time", "published_at")),
        ("type", ("type", "noteType", "note_type")),
        ("cover", ("cover",)),
    )
    for target, names in field_sources:
        value = _first_public_value((note_card, row), names)
        if value is not None:
            data[target] = value
    user = note_card.get("user")
    if isinstance(user, dict):
        nickname = _first_public_value((user,), ("nickname", "nick_name"))
        if isinstance(nickname, str):
            data["author_name"] = nickname
    if owner_id is not None:
        data["user_id"] = owner_id
    interact = note_card.get("interactInfo", note_card.get("interact_info"))
    if isinstance(interact, dict):
        for target, names in (
            ("liked_count", ("likedCount", "liked_count")),
            ("collect_count", ("collectedCount", "collectCount", "collected_count", "collect_count")),
            ("comment_count", ("commentCount", "comment_count")),
        ):
            count = _public_count(_first_public_value((interact,), names))
            if count is not None:
                data[target] = count
    return data


def _first_public_value(
    sources: Sequence[dict[str, Any]], names: Sequence[str]
) -> Any:
    for source in sources:
        for name in names:
            value = source.get(name)
            if isinstance(value, (str, int, dict)) and not isinstance(value, bool):
                if not isinstance(value, str) or value:
                    return value
    return None


def _public_count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 else None
    if not isinstance(value, str):
        return None
    normalized = value.strip().replace(",", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([万亿kKmM]?)", normalized)
    if match is None:
        return None
    multiplier = {
        "": 1,
        "万": 10_000,
        "亿": 100_000_000,
        "k": 1_000,
        "m": 1_000_000,
    }[match.group(2).casefold()]
    return int(float(match.group(1)) * multiplier)


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
        "title", "desc", "description", "bio", "nickname", "user_id", "userId",
        "red_id", "redId", "author_name", "followers_count", "following_count",
        "authorName", "liked_count", "likedCount", "liked_count", "collect_count", "collectCount",
        "comment_count", "commentCount", "publish_time", "publishTime", "type", "cover",
    }
    data = {key: redact_credentials(value) for key, value in row.items() if key in allowed}
    data.update(identity)
    return data


def _profile_public_data(row: dict[str, Any], *, user_id: str) -> dict[str, Any]:
    data = _public_data(row, user_id=user_id)
    data.pop("userId", None)
    data.pop("redId", None)
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
