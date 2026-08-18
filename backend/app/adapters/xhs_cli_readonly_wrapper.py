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


def _install_readonly_boundary(
    cli_module: ModuleType | Any,
    auth_module: ModuleType | Any,
    cookies: dict[str, str],
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
    raise RuntimeError("command_not_allowed")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
        command = _validated_cli_args(sys.argv[1:])
        cookies = _read_cookies()
        modules = _load_verified_package(_locate_pinned_source_root())
        package = modules["xhs_cli"]
        if getattr(package, "__version__", None) != PINNED_XHS_CLI_VERSION:
            raise RuntimeError("pinned_source_mismatch")
        auth_module = modules["xhs_cli.auth"]
        cli_module = modules["xhs_cli.cli"]
        _install_readonly_boundary(cli_module, auth_module, cookies)
        cli_module.cli.main(args=command, prog_name="xhs", standalone_mode=True)
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
        }:
            category = "readonly_boundary_failed"
        print(category, file=sys.stderr)
        return 73
    except BaseException:
        print("readonly_boundary_failed", file=sys.stderr)
        return 73


if __name__ == "__main__":
    raise SystemExit(main())
