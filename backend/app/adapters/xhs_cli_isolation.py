"""Handle-pinned state and private runtime preparation for xhs-cli reads."""

from __future__ import annotations

import json
import os
import re
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any


MAX_STATE_FILE_BYTES = 1024 * 1024
REQUIRED_EXTERNAL_COOKIES = frozenset({"a1", "web_session"})
_COOKIE_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")


class PreparedStateError(RuntimeError):
    """A safe external-state failure category."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class PreparedXhsExecution(AbstractContextManager["PreparedXhsExecution"]):
    """Cookie bytes plus directory handles held for the entire child lifetime."""

    def __init__(self, *, cookie_bytes: bytes, runtime_dir: Path, handles: list[Any]) -> None:
        self.cookie_bytes = cookie_bytes
        self.runtime_dir = runtime_dir
        self._handles = handles

    def __exit__(self, *_args: object) -> None:
        while self._handles:
            handle = self._handles.pop()
            try:
                if os.name == "nt":
                    import ctypes
                    from ctypes import wintypes

                    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
                    kernel32.CloseHandle(wintypes.HANDLE(handle))
                else:
                    os.close(handle)
            except OSError:
                pass


def prepare_xhs_execution(*, runtime_dir: Path, state_dir: Path) -> PreparedXhsExecution:
    """Pin trusted paths and read prepared credentials through the pinned handle."""
    runtime = Path(os.path.abspath(runtime_dir))
    state = Path(os.path.abspath(state_dir))
    try:
        state.relative_to(runtime)
    except ValueError as error:
        raise PreparedStateError("external_state_untrusted") from error

    relative_state = state.relative_to(runtime)
    trusted_paths = [runtime]
    trusted_cursor = runtime
    for component in relative_state.parts:
        trusted_cursor /= component
        trusted_paths.append(trusted_cursor)
    config = state / ".xhs-cli"
    trusted_paths.append(config)
    cookie_file = config / "cookies.json"
    try:
        os.lstat(cookie_file)
    except FileNotFoundError:
        raise PreparedStateError("login_required")
    except OSError as error:
        raise PreparedStateError("external_state_untrusted") from error

    private = state / "private-runtime"
    private_paths = [
        private,
        private / "appdata",
        private / "appdata" / "roaming",
        private / "appdata" / "local",
        private / "tmp",
        private / "xdg",
        private / "xdg" / "config",
        private / "xdg" / "cache",
        private / "xdg" / "data",
    ]
    if os.name == "nt":
        return _prepare_windows(trusted_paths, private_paths, cookie_file, private)
    return _prepare_posix(trusted_paths, private_paths, cookie_file, private)


def isolated_environment(private: Path) -> dict[str, str]:
    """Build a small child environment containing only app-owned profile paths."""
    passthrough = (
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "SystemRoot",
        "WINDIR",
        "COMSPEC",
        "SYSTEMDRIVE",
    )
    environment = {
        key: value for key in passthrough if (value := os.environ.get(key))
    }
    environment.update({
        "HOME": str(private),
        "USERPROFILE": str(private),
        "APPDATA": str(private / "appdata" / "roaming"),
        "LOCALAPPDATA": str(private / "appdata" / "local"),
        "TEMP": str(private / "tmp"),
        "TMP": str(private / "tmp"),
        "XDG_CONFIG_HOME": str(private / "xdg" / "config"),
        "XDG_CACHE_HOME": str(private / "xdg" / "cache"),
        "XDG_DATA_HOME": str(private / "xdg" / "data"),
        "NO_COLOR": "1",
    })
    return environment


def _validated_cookie_bytes(encoded: bytes) -> bytes:
    try:
        payload = json.loads(encoded.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PreparedStateError("login_required") from error
    cookies = payload.get("cookies") if isinstance(payload, dict) else None
    if not isinstance(cookies, dict):
        raise PreparedStateError("login_required")
    clean: dict[str, str] = {}
    for name, value in cookies.items():
        if (
            not isinstance(name, str)
            or _COOKIE_NAME.fullmatch(name) is None
            or not isinstance(value, str)
            or not value
            or any(ord(char) < 0x21 or char in ";," or ord(char) == 0x7F for char in value)
        ):
            raise PreparedStateError("external_state_untrusted")
        clean[name] = value
    if not all(clean.get(name, "").strip() for name in REQUIRED_EXTERNAL_COOKIES):
        raise PreparedStateError("login_required")
    return json.dumps(
        {"cookies": clean}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _prepare_windows(
    trusted_paths: list[Path],
    private_paths: list[Path],
    cookie_file: Path,
    private: Path,
) -> PreparedXhsExecution:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(BY_HANDLE_FILE_INFORMATION)
    ]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD
    ]
    kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    ]
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    invalid_handle = wintypes.HANDLE(-1).value
    handles: list[int] = []

    def close_all() -> None:
        while handles:
            kernel32.CloseHandle(wintypes.HANDLE(handles.pop()))

    def open_path(path: Path, *, directory: bool) -> tuple[int, BY_HANDLE_FILE_INFORMATION]:
        # BACKUP_SEMANTICS opens directories; OPEN_REPARSE_POINT prevents traversal.
        flags = (0x02000000 if directory else 0) | 0x00200000
        handle = kernel32.CreateFileW(
            str(path),
            0x80000000,
            0x00000001,
            None,
            3,
            flags,
            None,
        )
        if handle in (None, 0, invalid_handle):
            raise OSError(ctypes.get_last_error(), "CreateFileW")
        handle_value = int(handle)
        handles.append(handle_value)
        info = BY_HANDLE_FILE_INFORMATION()
        if not kernel32.GetFileInformationByHandle(
            wintypes.HANDLE(handle_value), ctypes.byref(info)
        ):
            raise OSError(ctypes.get_last_error(), "GetFileInformationByHandle")
        if info.dwFileAttributes & 0x00000400:
            raise PreparedStateError("external_state_untrusted")
        is_directory = bool(info.dwFileAttributes & 0x00000010)
        if is_directory != directory:
            raise PreparedStateError("external_state_untrusted")
        buffer = ctypes.create_unicode_buffer(32768)
        length = kernel32.GetFinalPathNameByHandleW(
            wintypes.HANDLE(handle_value), buffer, len(buffer), 0
        )
        if not length or length >= len(buffer):
            raise OSError(ctypes.get_last_error(), "GetFinalPathNameByHandleW")
        actual = buffer.value.removeprefix("\\\\?\\")
        expected = str(Path(os.path.abspath(path)))
        if os.path.normcase(actual) != os.path.normcase(expected):
            raise PreparedStateError("external_state_untrusted")
        return handle_value, info

    try:
        for directory in trusted_paths:
            open_path(directory, directory=True)
        cookie_handle, before = open_path(cookie_file, directory=False)
        size = (int(before.nFileSizeHigh) << 32) | int(before.nFileSizeLow)
        if before.nNumberOfLinks != 1 or size < 1 or size > MAX_STATE_FILE_BYTES:
            raise PreparedStateError("external_state_untrusted")
        buffer = ctypes.create_string_buffer(size)
        read = wintypes.DWORD()
        if not kernel32.ReadFile(
            wintypes.HANDLE(cookie_handle), buffer, size, ctypes.byref(read), None
        ) or read.value != size:
            raise OSError(ctypes.get_last_error(), "ReadFile")
        after = BY_HANDLE_FILE_INFORMATION()
        if not kernel32.GetFileInformationByHandle(
            wintypes.HANDLE(cookie_handle), ctypes.byref(after)
        ):
            raise OSError(ctypes.get_last_error(), "GetFileInformationByHandle")
        identity_before = (
            before.dwVolumeSerialNumber, before.nFileIndexHigh, before.nFileIndexLow,
            before.nFileSizeHigh, before.nFileSizeLow, before.nNumberOfLinks,
        )
        identity_after = (
            after.dwVolumeSerialNumber, after.nFileIndexHigh, after.nFileIndexLow,
            after.nFileSizeHigh, after.nFileSizeLow, after.nNumberOfLinks,
        )
        if identity_before != identity_after:
            raise PreparedStateError("external_state_untrusted")
        encoded = _validated_cookie_bytes(buffer.raw[:size])
        for directory in private_paths:
            try:
                directory.mkdir(exist_ok=True)
            except OSError as error:
                raise PreparedStateError("external_state_untrusted") from error
            open_path(directory, directory=True)
        return PreparedXhsExecution(
            cookie_bytes=encoded, runtime_dir=private, handles=handles
        )
    except PreparedStateError:
        close_all()
        raise
    except OSError as error:
        close_all()
        raise PreparedStateError("external_state_untrusted") from error


def _prepare_posix(
    trusted_paths: list[Path],
    private_paths: list[Path],
    cookie_file: Path,
    private: Path,
) -> PreparedXhsExecution:
    handles: list[int] = []
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    try:
        for directory in trusted_paths:
            handle = os.open(directory, os.O_RDONLY | nofollow | directory_flag)
            handles.append(handle)
            stat = os.fstat(handle)
            if not os.path.isdir(directory) or stat.st_nlink < 1:
                raise PreparedStateError("external_state_untrusted")
        cookie_handle = os.open(cookie_file, os.O_RDONLY | nofollow)
        handles.append(cookie_handle)
        before = os.fstat(cookie_handle)
        if before.st_nlink != 1 or before.st_size < 1 or before.st_size > MAX_STATE_FILE_BYTES:
            raise PreparedStateError("external_state_untrusted")
        encoded = b""
        while len(encoded) < before.st_size:
            chunk = os.read(cookie_handle, before.st_size - len(encoded))
            if not chunk:
                break
            encoded += chunk
        after = os.fstat(cookie_handle)
        if (
            len(encoded) != before.st_size
            or (before.st_dev, before.st_ino, before.st_size, before.st_nlink)
            != (after.st_dev, after.st_ino, after.st_size, after.st_nlink)
        ):
            raise PreparedStateError("external_state_untrusted")
        for directory in private_paths:
            directory.mkdir(exist_ok=True)
            handle = os.open(directory, os.O_RDONLY | nofollow | directory_flag)
            handles.append(handle)
            if not os.path.isdir(directory):
                raise PreparedStateError("external_state_untrusted")
        return PreparedXhsExecution(
            cookie_bytes=_validated_cookie_bytes(encoded),
            runtime_dir=private,
            handles=handles,
        )
    except PreparedStateError:
        while handles:
            os.close(handles.pop())
        raise
    except OSError as error:
        while handles:
            os.close(handles.pop())
        raise PreparedStateError("external_state_untrusted") from error
