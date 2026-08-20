"""Read-only launcher for the audited xhs-cli revision.

Credentials arrive only on stdin.  This module verifies the source revision,
replaces every persisted/browser authentication entry point, disables token
cache writes, and then exposes only the five collection commands used by the
workbench.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from types import CodeType, ModuleType
from typing import Any


PINNED_XHS_CLI_VERSION = "0.1.4"
PINNED_SOURCE_SHA256 = {
    "__init__.py": "af047caa0a1dfacaa900e7fbcc7011fd219b7325052045d077d011cd405c30a2",
    "auth.py": "771c8fa87f5776261735c3bac4c827d4d0b9b419d0d935588c7cba8a64dca6a2",
    "cli.py": "f40dd3fd431e72ec0afc412705bf00b4aadc244738db528204635df9d0801fb7",
    "client.py": "7c87b97568ff512a2b0f45fc9b74687a3f627b034b768fdd0bffbf60da9147bf",
    "exceptions.py": "6de0dca064444f6cb55c8f6186eec57d210be5bd97d763e039f7807e0d2859be",
}
_PINNED_EXECUTION_ORDER = (
    "__init__.py",
    "exceptions.py",
    "auth.py",
    "client.py",
    "cli.py",
)
_MAX_SOURCE_BYTES = 2 * 1024 * 1024
_MAX_STDIN_BYTES = 1024 * 1024
_COOKIE_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_REQUIRED_COOKIES = frozenset({"a1", "web_session"})
_USER_POSTS_SCROLL_DELTA = 1200
_USER_POSTS_SCROLL_WAIT_MS = 1000
_USER_POSTS_MAX_SCROLL_ATTEMPTS = 60
_USER_POSTS_STABLE_ATTEMPTS = 2
_ENGINEERING_LATEST_SAMPLE_LIMIT = 10
_USER_POSTS_SNAPSHOT_JS = """
() => {
    const state = window.__INITIAL_STATE__;
    if (!state || !state.user || !state.user.notes) return {slots: [], natural_end: false};
    const unwrap = (value, depth = 0) => {
        if (depth > 6 || value === null || value === undefined) return value;
        if (typeof value !== 'object') return value;
        if ('_value' in value && 'dep' in value) return unwrap(value._value, depth + 1);
        if ('value' in value && 'dep' in value) return unwrap(value.value, depth + 1);
        if (Array.isArray(value)) return value.map(item => unwrap(item, depth + 1));
        const result = {};
        for (const key of Object.keys(value)) {
            if (key === 'dep' || key.startsWith('__')) continue;
            try { result[key] = unwrap(value[key], depth + 1); } catch (_error) {}
        }
        return result;
    };
    const notes = unwrap(state.user.notes);
    let slots = [];
    if (Array.isArray(notes)) slots = notes;
    if (notes && typeof notes === 'object') {
        for (const key of ['value', '_value', 'data', 'list']) {
            if (Array.isArray(notes[key])) { slots = notes[key]; break; }
        }
    }
    const terminalByState = Boolean(notes && typeof notes === 'object' &&
        (notes.hasMore === false || notes.has_more === false || notes.isEnd === true));
    const documentElement = document.documentElement;
    const terminalByViewport = Boolean(documentElement &&
        window.scrollY + window.innerHeight >= documentElement.scrollHeight - 1);
    return {slots, natural_end: terminalByState || terminalByViewport};
}
"""


def _locate_pinned_source_root() -> Path:
    """Find one filesystem package without consulting Python import loaders."""
    candidates: list[Path] = []
    for entry in sys.path:
        if not entry:
            continue
        candidate = Path(entry) / "xhs_cli"
        try:
            if candidate.is_dir():
                candidates.append(candidate)
        except OSError:
            continue
    if not candidates:
        raise RuntimeError("pinned_package_unavailable")
    if len(candidates) != 1:
        raise RuntimeError("pinned_source_mismatch")
    return candidates[0]


def _module_name(filename: str) -> str:
    return "xhs_cli" if filename == "__init__.py" else f"xhs_cli.{filename[:-3]}"


def _verified_source_bytes(source_root: Path) -> dict[str, bytes]:
    """Read each allowed source once; the returned bytes are the execution input."""
    try:
        source_names = {
            entry.name
            for entry in source_root.iterdir()
            if entry.is_file() and entry.suffix == ".py"
        }
    except OSError as error:
        raise RuntimeError("pinned_source_mismatch") from error
    if source_names != set(PINNED_SOURCE_SHA256):
        raise RuntimeError("pinned_source_mismatch")

    sources: dict[str, bytes] = {}
    for filename in _PINNED_EXECUTION_ORDER:
        try:
            with (source_root / filename).open("rb") as stream:
                encoded = stream.read(_MAX_SOURCE_BYTES + 1)
        except OSError as error:
            raise RuntimeError("pinned_source_mismatch") from error
        if len(encoded) > _MAX_SOURCE_BYTES:
            raise RuntimeError("pinned_source_mismatch")
        normalized = encoded.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        if hashlib.sha256(normalized).hexdigest() != PINNED_SOURCE_SHA256[filename]:
            raise RuntimeError("pinned_source_mismatch")
        sources[filename] = normalized
    return sources


def _load_verified_package(source_root: Path) -> dict[str, ModuleType]:
    """Compile and execute only already-hashed bytes, never source paths or pyc."""
    sources = _verified_source_bytes(source_root)
    expected_names = {_module_name(filename) for filename in _PINNED_EXECUTION_ORDER}
    if any(
        name == "xhs_cli" or name.startswith("xhs_cli.")
        for name in sys.modules
    ):
        raise RuntimeError("pinned_module_conflict")

    code: dict[str, CodeType] = {}
    for filename in _PINNED_EXECUTION_ORDER:
        module_name = _module_name(filename)
        origin = f"verified-memory:{module_name}"
        code[module_name] = compile(
            sources[filename], origin, "exec", dont_inherit=True
        )

    modules: dict[str, ModuleType] = {}
    try:
        for filename in _PINNED_EXECUTION_ORDER:
            module_name = _module_name(filename)
            module = ModuleType(module_name)
            module.__file__ = f"verified-memory:{module_name}"
            module.__loader__ = None
            module.__package__ = "xhs_cli" if module_name != "xhs_cli" else "xhs_cli"
            module.__spec__ = None
            if module_name == "xhs_cli":
                module.__path__ = ()
            modules[module_name] = module
            sys.modules[module_name] = module
        for filename in _PINNED_EXECUTION_ORDER:
            module_name = _module_name(filename)
            exec(code[module_name], modules[module_name].__dict__)
        actual_names = {
            name
            for name in sys.modules
            if name == "xhs_cli" or name.startswith("xhs_cli.")
        }
        if actual_names != expected_names or any(
            sys.modules.get(name) is not module
            or module.__file__ != f"verified-memory:{name}"
            for name, module in modules.items()
        ):
            raise RuntimeError("pinned_module_conflict")
        return modules
    except BaseException:
        for name, module in modules.items():
            if sys.modules.get(name) is module:
                del sys.modules[name]
        raise


def _read_cookies() -> dict[str, str]:
    encoded = sys.stdin.buffer.read(_MAX_STDIN_BYTES + 1)
    if not encoded or len(encoded) > _MAX_STDIN_BYTES:
        raise RuntimeError("prepared_state_invalid")
    try:
        payload = json.loads(encoded.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("prepared_state_invalid") from error
    cookies = payload.get("cookies") if isinstance(payload, dict) else None
    if not isinstance(cookies, dict):
        raise RuntimeError("prepared_state_invalid")
    clean: dict[str, str] = {}
    for name, value in cookies.items():
        if (
            not isinstance(name, str)
            or _COOKIE_NAME.fullmatch(name) is None
            or not isinstance(value, str)
            or not value
            or any(ord(char) < 0x21 or char in ";," or ord(char) == 0x7F for char in value)
        ):
            raise RuntimeError("prepared_state_invalid")
        clean[name] = value
    if not all(clean.get(name, "").strip() for name in _REQUIRED_COOKIES):
        raise RuntimeError("prepared_state_invalid")
    return clean


def _user_post_rows(value: object) -> list[dict[str, Any]]:
    """Flatten the pinned CLI's page slots without accepting a caller shape."""
    rows: list[dict[str, Any]] = []
    if isinstance(value, dict):
        value = value.get("slots")
    if not isinstance(value, list):
        return rows
    for item in value:
        if isinstance(item, dict):
            rows.append(item)
        elif isinstance(item, list):
            rows.extend(_user_post_rows(item))
    return rows


def _natural_end_proven(value: object) -> bool:
    return isinstance(value, dict) and value.get("natural_end") is True


def _user_post_identity(row: dict[str, Any]) -> str | None:
    for key in ("id", "noteId", "note_id"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _largest_observed_user_posts(
    client: Any, user_id: str, original_get_user_posts: Any
) -> list[dict[str, Any]]:
    """Make a bounded, fixed read-only follow-up after the pinned initial read."""
    observed: dict[str, dict[str, Any]] = {}

    def add_rows(value: object) -> bool:
        grew = False
        for row in _user_post_rows(value):
            note_id = _user_post_identity(row)
            if note_id is not None and note_id not in observed:
                observed[note_id] = row
                grew = True
        return grew

    add_rows(original_get_user_posts(client, user_id))
    page = getattr(client, "_page", None)
    if page is None:
        return list(observed.values())
    stable_attempts = 0
    for attempt in range(_USER_POSTS_MAX_SCROLL_ATTEMPTS):
        try:
            page.mouse.wheel(0, _USER_POSTS_SCROLL_DELTA)
            page.wait_for_timeout(_USER_POSTS_SCROLL_WAIT_MS)
            grew = add_rows(page.evaluate(_USER_POSTS_SNAPSHOT_JS))
        except Exception:
            break
        if grew:
            stable_attempts = 0
            if attempt == _USER_POSTS_MAX_SCROLL_ATTEMPTS - 1:
                raise RuntimeError("bounded_collection_limit")
        else:
            stable_attempts += 1
            if stable_attempts >= _USER_POSTS_STABLE_ATTEMPTS:
                break
    return list(observed.values())


def _engineering_latest_sample_user_posts(
    client: Any, user_id: str, original_get_user_posts: Any
) -> dict[str, object]:
    """Apply the engineering-only latest-ten account sampling rule without full scrolling."""
    observed: dict[str, dict[str, Any]] = {}
    available_count_observed = 0

    def add_rows(value: object) -> bool:
        nonlocal available_count_observed
        visible: dict[str, dict[str, Any]] = {}
        for row in _user_post_rows(value):
            note_id = _user_post_identity(row)
            if note_id is not None:
                visible.setdefault(note_id, row)
        available_count_observed = max(available_count_observed, len(visible))
        grew = False
        for note_id, row in visible.items():
            if note_id not in observed and len(observed) < _ENGINEERING_LATEST_SAMPLE_LIMIT:
                observed[note_id] = row
                grew = True
        return grew

    def sampled(completeness: str) -> dict[str, object]:
        return {
            "collection_scope": "latest",
            "sample_limit": _ENGINEERING_LATEST_SAMPLE_LIMIT,
            "notes": list(observed.values()),
            "available_count_observed": available_count_observed,
            "completeness": completeness,
        }

    add_rows(original_get_user_posts(client, user_id))
    if len(observed) == _ENGINEERING_LATEST_SAMPLE_LIMIT:
        return sampled("bounded_sample")
    page = getattr(client, "_page", None)
    if page is None:
        raise RuntimeError("bounded_sample_incomplete")
    stable_attempts = 0
    for _attempt in range(_USER_POSTS_MAX_SCROLL_ATTEMPTS):
        try:
            page.mouse.wheel(0, _USER_POSTS_SCROLL_DELTA)
            page.wait_for_timeout(_USER_POSTS_SCROLL_WAIT_MS)
            snapshot = page.evaluate(_USER_POSTS_SNAPSHOT_JS)
        except Exception as error:
            raise RuntimeError("bounded_sample_incomplete") from error
        grew = add_rows(snapshot)
        if len(observed) == _ENGINEERING_LATEST_SAMPLE_LIMIT:
            return sampled("bounded_sample")
        if _natural_end_proven(snapshot):
            return sampled("sample_exhausted")
        stable_attempts = 0 if grew else stable_attempts + 1
        if stable_attempts >= _USER_POSTS_STABLE_ATTEMPTS:
            raise RuntimeError("bounded_sample_incomplete")
    raise RuntimeError("bounded_sample_incomplete")


def _install_readonly_boundary(
    cli_module: ModuleType | Any,
    auth_module: ModuleType | Any,
    cookies: dict[str, str],
    *,
    client_module: ModuleType | Any | None = None,
    command: list[str] | None = None,
    latest_sample: bool = False,
) -> None:
    """Replace every audited browser/persistence hook before command dispatch."""
    cookie_string = "; ".join(f"{name}={value}" for name, value in sorted(cookies.items()))

    def supplied_cookie() -> str:
        return cookie_string

    def disabled(*_args: object, **_kwargs: object) -> Any:
        raise RuntimeError("readonly boundary disabled")

    def no_cache(*_args: object, **_kwargs: object) -> None:
        return None

    for module in (auth_module, cli_module):
        module.get_cookie_string = supplied_cookie
        module.get_saved_cookie_string = supplied_cookie
        module.save_token_cache = no_cache
    auth_module._load_saved_cookies = supplied_cookie
    auth_module._extract_browser_cookies = disabled
    auth_module.qrcode_login = disabled
    auth_module._browser_assisted_qrcode_login = disabled
    auth_module.save_cookies = disabled
    auth_module.clear_cookies = disabled
    auth_module.load_xsec_token = disabled
    cli_module.qrcode_login = disabled
    cli_module.clear_cookies = disabled
    cli_module.load_xsec_token = disabled
    cli_module._cache_note_tokens = no_cache
    if client_module is not None and command == ["whoami", "--json"]:
        client_module.XhsClient.get_user_info = lambda _self, _user_id: {}
    if client_module is not None and command is not None and command[0] == "user-posts":
        original_get_user_posts = client_module.XhsClient.get_user_posts
        client_module.XhsClient.get_user_posts = (
            lambda self, user_id: _engineering_latest_sample_user_posts(
                self, user_id, original_get_user_posts
            )
        ) if latest_sample or "--latest-10" in command else (
            lambda self, user_id: _largest_observed_user_posts(
                self, user_id, original_get_user_posts
            )
        )


def _validated_cli_args(argv: list[str]) -> list[str]:
    if argv == ["status"] or argv == ["whoami", "--json"]:
        return argv
    if len(argv) == 3 and argv[0] in {"search", "user", "user-posts"} and argv[2] == "--json":
        positional = argv[1]
        if (
            positional
            and len(positional) <= 500
            and not positional.startswith("-")
            and not any(ord(char) < 32 or ord(char) == 127 for char in positional)
        ):
            return argv
    if len(argv) == 4 and argv[0] == "user-posts" and argv[2:] == ["--latest-10", "--json"]:
        if argv[1] and len(argv[1]) <= 500 and not argv[1].startswith("-") and not any(ord(char) < 32 or ord(char) == 127 for char in argv[1]):
            return argv
    raise RuntimeError("command_not_allowed")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
        command = _validated_cli_args(sys.argv[1:])
        latest_sample = command[:1] == ["user-posts"] and "--latest-10" in command
        cli_command = [part for part in command if part != "--latest-10"]
        cookies = _read_cookies()
        modules = _load_verified_package(_locate_pinned_source_root())
        package = modules["xhs_cli"]
        if getattr(package, "__version__", None) != PINNED_XHS_CLI_VERSION:
            raise RuntimeError("pinned_source_mismatch")
        auth_module = modules["xhs_cli.auth"]
        client_module = modules["xhs_cli.client"]
        cli_module = modules["xhs_cli.cli"]
        _install_readonly_boundary(
            cli_module,
            auth_module,
            cookies,
            client_module=client_module,
            command=cli_command,
            latest_sample=latest_sample,
        )
        cli_module.cli.main(args=cli_command, prog_name="xhs", standalone_mode=True)
        return 0
    except SystemExit as error:
        return int(error.code or 0)
    except RuntimeError as error:
        category = str(error)
        if category not in {
            "command_not_allowed",
            "pinned_package_unavailable",
            "pinned_source_mismatch",
            "pinned_module_conflict",
            "prepared_state_invalid",
            "readonly boundary disabled",
            "bounded_collection_limit",
            "bounded_sample_incomplete",
        }:
            category = "readonly_boundary_failed"
        print(category, file=sys.stderr)
        return 73
    except BaseException:
        print("readonly_boundary_failed", file=sys.stderr)
        return 73


if __name__ == "__main__":
    raise SystemExit(main())
