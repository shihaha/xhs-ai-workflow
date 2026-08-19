"""Narrow permanent-delete boundary for private XHS staging files only."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from backend.app.features.content.export import (
    _delete_open_file,
    inspect_contained_artifact,
    remove_contained_regular,
)


_STAGE_NAME = re.compile(
    r"(?:"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"-[0-9a-f]{32}(?:-failure)?"
    r"|rollback-[0-9a-f]{32}"
    r")\.stage"
)
_ACTIVE_STAGE_NAME = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"-[0-9a-f]{32}(?:-failure)?\.stage"
)
_FINAL_NAME = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"(?:-failure)?\.json"
)

# Narrow fault-injection hook: tests raise only after the exact new file handle
# has been retained by the store.
_stage_creation_after_open_hook: Callable[[int], None] | None = None
_SQLITE_MAX_INTEGER = (1 << 63) - 1


def sqlite_file_device_identity(value: int) -> int:
    """Keep Windows unsigned volume identities stable inside SQLite INTEGER."""

    normalized = int(value)
    if normalized < 0:
        raise ValueError("file device identity must be non-negative")
    if normalized <= _SQLITE_MAX_INTEGER:
        return normalized
    digest = hashlib.sha256(str(normalized).encode("ascii")).digest()
    return int.from_bytes(digest[:8], "big") & _SQLITE_MAX_INTEGER


class UnsafeXhsArtifactStore(OSError):
    """A runtime artifact parent or file identity was not trustworthy."""


@dataclass(frozen=True)
class XhsArtifactIdentity:
    file_dev: int
    file_ino: int
    size_bytes: int
    file_mtime_ns: int

    @classmethod
    def from_stat(cls, metadata: os.stat_result) -> "XhsArtifactIdentity":
        return cls(
            sqlite_file_device_identity(metadata.st_dev),
            int(metadata.st_ino),
            int(metadata.st_size),
            int(metadata.st_mtime_ns),
        )


@dataclass(frozen=True)
class XhsArtifactInspection:
    status: Literal["trusted", "missing", "ambiguous"]
    identity: XhsArtifactIdentity | None = None


class TrustedXhsArtifactStore(AbstractContextManager["TrustedXhsArtifactStore"]):
    """Pin the private XHS parents and perform every mutation relative to them."""

    def __init__(self, runtime_root: Path, *, max_bytes: int) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.runtime_root = Path(os.path.abspath(runtime_root))
        self.max_bytes = max_bytes
        backend_type = _WindowsArtifactStore if os.name == "nt" else _PosixArtifactStore
        try:
            self._backend = backend_type(self.runtime_root, max_bytes=max_bytes)
        except UnsafeXhsArtifactStore:
            raise
        except (OSError, TypeError, ValueError) as error:
            raise UnsafeXhsArtifactStore(
                "XHS artifact root could not be pinned safely."
            ) from error

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self._backend.close()

    def create_stage(self, name: str, payload: bytes) -> XhsArtifactIdentity:
        _require_stage_name(name)
        if len(payload) > self.max_bytes:
            raise UnsafeXhsArtifactStore("XHS stage payload exceeds its hard cap.")
        return self._backend.create_stage(name, payload)

    def inspect_stage(self, name: str) -> XhsArtifactInspection:
        _require_stage_name(name)
        return self._backend.inspect("stage", name)

    def inspect_final(self, name: str) -> XhsArtifactInspection:
        _require_final_name(name)
        return self._backend.inspect("final", name)

    def read_stage(self, name: str, identity: XhsArtifactIdentity) -> bytes:
        _require_stage_name(name)
        return self._backend.read("stage", name, identity)

    def read_final(self, name: str, identity: XhsArtifactIdentity) -> bytes:
        _require_final_name(name)
        return self._backend.read("final", name, identity)

    def promote(
        self,
        stage_name: str,
        final_name: str,
        identity: XhsArtifactIdentity,
    ) -> XhsArtifactIdentity:
        _require_stage_name(stage_name)
        _require_final_name(final_name)
        return self._backend.promote(stage_name, final_name, identity)

    def recover_promote(
        self,
        stage_name: str,
        final_name: str,
        identity: XhsArtifactIdentity,
    ) -> XhsArtifactIdentity:
        """Promote an existing exact stage after reopening all trusted parents."""
        _require_stage_name(stage_name)
        _require_final_name(final_name)
        return self._backend.recover_promote(stage_name, final_name, identity)

    def demote_and_discard(
        self,
        final_name: str,
        stage_name: str,
        identity: XhsArtifactIdentity,
    ) -> bool:
        _require_final_name(final_name)
        _require_stage_name(stage_name)
        return self._backend.demote_and_discard(final_name, stage_name, identity)

    def discard_stage(self, name: str, identity: XhsArtifactIdentity) -> bool:
        _require_stage_name(name)
        return self._backend.discard_stage(name, identity)

    def discard_owned_stage(self, name: str) -> bool:
        """Delete only the still-held file created by this store instance."""
        _require_stage_name(name)
        return self._backend.discard_owned_stage(name)


def _require_stage_name(name: str) -> None:
    if _ACTIVE_STAGE_NAME.fullmatch(name) is None:
        raise UnsafeXhsArtifactStore("Invalid XHS staging name.")


def _require_final_name(name: str) -> None:
    if _FINAL_NAME.fullmatch(name) is None:
        raise UnsafeXhsArtifactStore("Invalid XHS evidence name.")


def _trusted_identity(
    metadata: os.stat_result,
    *,
    max_bytes: int,
) -> XhsArtifactIdentity:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or int(getattr(metadata, "st_nlink", 1)) != 1
        or int(getattr(metadata, "st_file_attributes", 0)) & 0x00000400
        or not 0 <= metadata.st_size <= max_bytes
    ):
        raise UnsafeXhsArtifactStore("XHS artifact is not a bounded regular file.")
    return XhsArtifactIdentity.from_stat(metadata)


def _rename_windows_fd_relative(
    descriptor: int,
    directory_handle: int,
    target_name: str,
) -> bool:
    """Rename an open file relative to one already-pinned directory handle."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    encoded_length = len(target_name.encode("utf-16-le"))

    class StatusValue(ctypes.Union):
        _fields_ = [("status", wintypes.LONG), ("pointer", wintypes.LPVOID)]

    class IoStatusBlock(ctypes.Structure):
        _anonymous_ = ("value",)
        _fields_ = [("value", StatusValue), ("information", ctypes.c_size_t)]

    class FileRenameInfo(ctypes.Structure):
        _fields_ = [
            ("replace_if_exists", ctypes.c_ubyte),
            ("root_directory", wintypes.HANDLE),
            ("file_name_length", wintypes.DWORD),
            ("file_name", wintypes.WCHAR * (len(target_name) + 1)),
        ]

    information = FileRenameInfo(False, directory_handle, encoded_length, target_name)
    io_status = IoStatusBlock()
    set_information = ctypes.WinDLL("ntdll", use_last_error=True).NtSetInformationFile
    set_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(IoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        ctypes.c_int,
    )
    set_information.restype = wintypes.LONG
    return set_information(
        msvcrt.get_osfhandle(descriptor),
        ctypes.byref(io_status),
        ctypes.byref(information),
        FileRenameInfo.file_name.offset + encoded_length,
        10,
    ) == 0


def _rename_posix_noreplace(
    source_parent: int,
    source_name: str,
    target_parent: int,
    target_name: str,
) -> None:
    """Use Linux renameat2 without replacement; unsupported hosts fail closed."""

    import ctypes
    import errno

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise UnsafeXhsArtifactStore("Atomic no-replace rename is unavailable.")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_parent,
        os.fsencode(source_name),
        target_parent,
        os.fsencode(target_name),
        1,  # RENAME_NOREPLACE
    )
    if result == 0:
        return
    code = ctypes.get_errno()
    if code == errno.EEXIST:
        raise FileExistsError(target_name)
    if code in {errno.ENOSYS, errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL)}:
        raise UnsafeXhsArtifactStore("Atomic no-replace rename is unavailable.")
    raise OSError(code, os.strerror(code), source_name)


class _PosixArtifactStore:
    def __init__(self, runtime_root: Path, *, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self._handles: list[int] = []
        self._stage_handles: dict[str, int] = {}
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory = getattr(os, "O_DIRECTORY", 0)
        try:
            runtime = os.open(runtime_root, os.O_RDONLY | nofollow | directory)
            self._handles.append(runtime)
            parent = runtime
            for component in ("evidence", "xhs", ".staging"):
                try:
                    os.mkdir(component, mode=0o700, dir_fd=parent)
                except FileExistsError:
                    pass
                opened = os.open(
                    component,
                    os.O_RDONLY | nofollow | directory,
                    dir_fd=parent,
                )
                self._handles.append(opened)
                if not stat.S_ISDIR(os.fstat(opened).st_mode):
                    raise UnsafeXhsArtifactStore("XHS artifact parent is not a directory.")
                if component == "xhs":
                    self._final_parent = opened
                parent = opened
            self._stage_parent = parent
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        for descriptor in list(self._stage_handles.values()):
            try:
                os.close(descriptor)
            except OSError:
                pass
        self._stage_handles.clear()
        while self._handles:
            try:
                os.close(self._handles.pop())
            except OSError:
                pass

    def _parent(self, area: str) -> int:
        return self._stage_parent if area == "stage" else self._final_parent

    def create_stage(self, name: str, payload: bytes) -> XhsArtifactIdentity:
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=self._stage_parent)
            self._stage_handles[name] = descriptor
            descriptor = None
            held = self._stage_handles[name]
            if _stage_creation_after_open_hook is not None:
                _stage_creation_after_open_hook(held)
            view = memoryview(payload)
            offset = 0
            while offset < len(view):
                written = os.write(held, view[offset:])
                if written <= 0:
                    raise OSError("short XHS staging write")
                offset += written
            os.fsync(held)
            identity = _trusted_identity(os.fstat(held), max_bytes=self.max_bytes)
            if identity.size_bytes != len(payload):
                raise UnsafeXhsArtifactStore("XHS staging write changed identity.")
            named = self._open("stage", name)
            try:
                if _trusted_identity(os.fstat(named), max_bytes=self.max_bytes) != identity:
                    raise UnsafeXhsArtifactStore("XHS staging name changed while writing.")
            finally:
                os.close(named)
            os.fsync(self._stage_parent)
            return identity
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _open(self, area: str, name: str) -> int:
        return os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=self._parent(area),
        )

    def inspect(self, area: str, name: str) -> XhsArtifactInspection:
        descriptor: int | None = None
        try:
            descriptor = self._open(area, name)
            return XhsArtifactInspection(
                "trusted",
                _trusted_identity(os.fstat(descriptor), max_bytes=self.max_bytes),
            )
        except FileNotFoundError:
            return XhsArtifactInspection("missing")
        except (OSError, ValueError, TypeError):
            return XhsArtifactInspection("ambiguous")
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def read(self, area: str, name: str, identity: XhsArtifactIdentity) -> bytes:
        descriptor = self._open(area, name)
        try:
            if _trusted_identity(os.fstat(descriptor), max_bytes=self.max_bytes) != identity:
                raise UnsafeXhsArtifactStore("XHS artifact identity changed.")
            payload = b""
            while len(payload) <= self.max_bytes:
                chunk = os.read(descriptor, min(65536, self.max_bytes + 1 - len(payload)))
                if not chunk:
                    break
                payload += chunk
            if len(payload) > self.max_bytes or _trusted_identity(os.fstat(descriptor), max_bytes=self.max_bytes) != identity:
                raise UnsafeXhsArtifactStore("XHS artifact changed while reading.")
            return payload
        finally:
            os.close(descriptor)

    def promote(self, stage_name: str, final_name: str, identity: XhsArtifactIdentity) -> XhsArtifactIdentity:
        held = self._stage_handles.get(stage_name)
        if held is None or _trusted_identity(os.fstat(held), max_bytes=self.max_bytes) != identity:
            raise UnsafeXhsArtifactStore("XHS staging handle identity changed.")
        named = self.inspect("stage", stage_name)
        if named.status != "trusted" or named.identity != identity:
            raise UnsafeXhsArtifactStore("XHS staging name identity changed.")
        _rename_posix_noreplace(
            self._stage_parent,
            stage_name,
            self._final_parent,
            final_name,
        )
        os.fsync(self._stage_parent)
        os.fsync(self._final_parent)
        os.close(self._stage_handles.pop(stage_name))
        promoted = self.inspect("final", final_name)
        if promoted.status != "trusted" or promoted.identity != identity:
            raise UnsafeXhsArtifactStore("XHS promotion identity changed.")
        return identity

    def recover_promote(self, stage_name: str, final_name: str, identity: XhsArtifactIdentity) -> XhsArtifactIdentity:
        descriptor = self._open("stage", stage_name)
        try:
            if _trusted_identity(os.fstat(descriptor), max_bytes=self.max_bytes) != identity:
                raise UnsafeXhsArtifactStore("Recovered XHS staging identity changed.")
            _rename_posix_noreplace(
                self._stage_parent,
                stage_name,
                self._final_parent,
                final_name,
            )
            os.fsync(self._stage_parent)
            os.fsync(self._final_parent)
        finally:
            os.close(descriptor)
        promoted = self.inspect("final", final_name)
        if promoted.status != "trusted" or promoted.identity != identity:
            raise UnsafeXhsArtifactStore("Recovered XHS promotion identity changed.")
        return identity

    def demote_and_discard(self, final_name: str, stage_name: str, identity: XhsArtifactIdentity) -> bool:
        final = self.inspect("final", final_name)
        stage = self.inspect("stage", stage_name)
        if final.status == "missing":
            return stage.status == "missing" or self.discard_stage(stage_name, identity)
        if final.status != "trusted" or final.identity != identity or stage.status != "missing":
            return False
        _rename_posix_noreplace(
            self._final_parent,
            final_name,
            self._stage_parent,
            stage_name,
        )
        os.fsync(self._final_parent)
        os.fsync(self._stage_parent)
        return self.discard_stage(stage_name, identity)

    def discard_stage(self, name: str, identity: XhsArtifactIdentity) -> bool:
        inspection = self.inspect("stage", name)
        if inspection.status == "missing":
            return True
        # POSIX pathname unlink cannot prove that the directory entry still
        # names the inspected open identity at the deletion instant.
        return False

    def discard_owned_stage(self, name: str) -> bool:
        # Keep the exact handle and stage for manual recovery on POSIX hosts.
        return False


class _WindowsArtifactStore:
    def __init__(self, runtime_root: Path, *, max_bytes: int) -> None:
        import ctypes
        from ctypes import wintypes

        self.max_bytes = max_bytes
        self._raw_handles: list[int] = []
        self._stage_handles: dict[str, int] = {}
        self._ctypes = ctypes
        self._wintypes = wintypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._ntdll = ctypes.WinDLL("ntdll", use_last_error=True)

        class FileInformation(ctypes.Structure):
            _fields_ = [
                ("attributes", wintypes.DWORD),
                ("creation", wintypes.FILETIME),
                ("access", wintypes.FILETIME),
                ("write", wintypes.FILETIME),
                ("volume", wintypes.DWORD),
                ("size_high", wintypes.DWORD),
                ("size_low", wintypes.DWORD),
                ("links", wintypes.DWORD),
                ("index_high", wintypes.DWORD),
                ("index_low", wintypes.DWORD),
            ]

        class UnicodeString(ctypes.Structure):
            _fields_ = [
                ("Length", wintypes.USHORT),
                ("MaximumLength", wintypes.USHORT),
                ("Buffer", wintypes.LPWSTR),
            ]

        class ObjectAttributes(ctypes.Structure):
            _fields_ = [
                ("Length", wintypes.ULONG),
                ("RootDirectory", wintypes.HANDLE),
                ("ObjectName", ctypes.POINTER(UnicodeString)),
                ("Attributes", wintypes.ULONG),
                ("SecurityDescriptor", wintypes.LPVOID),
                ("SecurityQualityOfService", wintypes.LPVOID),
            ]

        class StatusValue(ctypes.Union):
            _fields_ = [("Status", wintypes.LONG), ("Pointer", wintypes.LPVOID)]

        class IoStatusBlock(ctypes.Structure):
            _anonymous_ = ("value",)
            _fields_ = [("value", StatusValue), ("Information", ctypes.c_size_t)]

        self._FileInformation = FileInformation
        self._UnicodeString = UnicodeString
        self._ObjectAttributes = ObjectAttributes
        self._IoStatusBlock = IoStatusBlock
        self._configure_functions()
        try:
            runtime = self._open_absolute_directory(runtime_root)
            parent = runtime
            cursor = runtime_root
            for component in ("evidence", "xhs", ".staging"):
                cursor /= component
                opened = self._open_relative_directory(parent, component, cursor, create=True)
                if component == "xhs":
                    self._final_parent = opened
                parent = opened
            self._stage_parent = parent
        except BaseException:
            self.close()
            raise

    def _configure_functions(self) -> None:
        ctypes = self._ctypes
        wintypes = self._wintypes
        self._kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        self._kernel32.CreateFileW.restype = wintypes.HANDLE
        self._kernel32.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(self._FileInformation),
        ]
        self._kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        self._kernel32.GetFinalPathNameByHandleW.argtypes = [
            wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        ]
        self._kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._ntdll.NtCreateFile.argtypes = [
            ctypes.POINTER(wintypes.HANDLE), wintypes.ULONG,
            ctypes.POINTER(self._ObjectAttributes), ctypes.POINTER(self._IoStatusBlock),
            ctypes.POINTER(ctypes.c_longlong), wintypes.ULONG, wintypes.ULONG,
            wintypes.ULONG, wintypes.ULONG, wintypes.LPVOID, wintypes.ULONG,
        ]
        self._ntdll.NtCreateFile.restype = wintypes.LONG

    def close(self) -> None:
        for descriptor in list(self._stage_handles.values()):
            try:
                os.close(descriptor)
            except OSError:
                pass
        self._stage_handles.clear()
        while getattr(self, "_raw_handles", []):
            self._kernel32.CloseHandle(
                self._wintypes.HANDLE(self._raw_handles.pop())
            )

    def _validate_directory(self, handle: int, expected: Path) -> None:
        info = self._FileInformation()
        if not self._kernel32.GetFileInformationByHandle(
            self._wintypes.HANDLE(handle), self._ctypes.byref(info)
        ):
            raise OSError(self._ctypes.get_last_error(), "directory identity")
        if not info.attributes & 0x10 or info.attributes & 0x400:
            raise UnsafeXhsArtifactStore("XHS artifact parent is linked or not a directory.")
        buffer = self._ctypes.create_unicode_buffer(32768)
        length = self._kernel32.GetFinalPathNameByHandleW(
            self._wintypes.HANDLE(handle), buffer, len(buffer), 0
        )
        if not length or length >= len(buffer):
            raise OSError(self._ctypes.get_last_error(), "directory final path")
        actual = buffer.value
        if actual.startswith("\\\\?\\UNC\\"):
            actual = "\\\\" + actual[8:]
        elif actual.startswith("\\\\?\\"):
            actual = actual[4:]
        if os.path.normcase(actual) != os.path.normcase(str(Path(os.path.abspath(expected)))):
            raise UnsafeXhsArtifactStore("XHS artifact parent identity changed.")

    def _open_absolute_directory(self, path: Path) -> int:
        desired = 0x00100000 | 0x00000001 | 0x00000002 | 0x00000004 | 0x00000020 | 0x80
        handle = self._kernel32.CreateFileW(
            str(path), desired, 0x7, None, 3, 0x02000000 | 0x00200000, None
        )
        invalid = self._wintypes.HANDLE(-1).value
        if handle in (None, 0, invalid):
            raise OSError(self._ctypes.get_last_error(), "open XHS runtime")
        value = int(handle)
        self._raw_handles.append(value)
        self._validate_directory(value, path)
        return value

    def _relative_native_handle(
        self,
        parent: int,
        name: str,
        *,
        directory: bool,
        create: bool,
        writable: bool = False,
    ) -> int:
        ctypes = self._ctypes
        wintypes = self._wintypes
        name_buffer = ctypes.create_unicode_buffer(name)
        encoded_length = len(name.encode("utf-16-le"))
        unicode_name = self._UnicodeString(
            encoded_length,
            encoded_length + ctypes.sizeof(ctypes.c_wchar),
            ctypes.cast(name_buffer, wintypes.LPWSTR),
        )
        attributes = self._ObjectAttributes(
            ctypes.sizeof(self._ObjectAttributes), wintypes.HANDLE(parent),
            ctypes.pointer(unicode_name), 0x40, None, None,
        )
        io_status = self._IoStatusBlock()
        opened = wintypes.HANDLE()
        if directory:
            desired = 0x00100000 | 0x1 | 0x2 | 0x4 | 0x20 | 0x80
            options = 0x00200000 | 0x20 | 0x1
            disposition = 3 if create else 1
        else:
            desired = 0x00100000 | 0x00010000 | 0x80000000 | 0x80
            if writable:
                desired |= 0x40000000 | 0x100
            options = 0x00200000 | 0x20 | 0x40
            disposition = 2 if create else 1
        status = self._ntdll.NtCreateFile(
            ctypes.byref(opened), desired, ctypes.byref(attributes),
            ctypes.byref(io_status), None, 0, 0x7, disposition, options, None, 0,
        )
        if status != 0:
            code = int(status) & 0xFFFFFFFF
            if code in {
                0xC000000F,
                0xC0000034,
                0xC0000039,
                0xC000003A,
                0xC0000056,  # STATUS_DELETE_PENDING: the name is no longer openable.
            }:
                raise FileNotFoundError(name)
            if code in {0xC0000035, 0xC0000043}:
                raise FileExistsError(name)
            raise UnsafeXhsArtifactStore("XHS relative handle open failed.")
        return int(opened.value)

    def _open_relative_directory(self, parent: int, name: str, expected: Path, *, create: bool) -> int:
        handle = self._relative_native_handle(parent, name, directory=True, create=create)
        self._raw_handles.append(handle)
        self._validate_directory(handle, expected)
        return handle

    def _open_file(self, area: str, name: str, *, create: bool = False, writable: bool = False) -> int:
        import msvcrt

        parent = self._stage_parent if area == "stage" else self._final_parent
        native = self._relative_native_handle(
            parent, name, directory=False, create=create, writable=writable
        )
        try:
            flags = (os.O_RDWR if writable else os.O_RDONLY) | getattr(os, "O_BINARY", 0)
            return msvcrt.open_osfhandle(native, flags)
        except BaseException:
            self._kernel32.CloseHandle(self._wintypes.HANDLE(native))
            raise

    def create_stage(self, name: str, payload: bytes) -> XhsArtifactIdentity:
        descriptor: int | None = None
        try:
            descriptor = self._open_file("stage", name, create=True, writable=True)
            self._stage_handles[name] = descriptor
            descriptor = None
            held = self._stage_handles[name]
            if _stage_creation_after_open_hook is not None:
                _stage_creation_after_open_hook(held)
            view = memoryview(payload)
            offset = 0
            while offset < len(view):
                written = os.write(held, view[offset:])
                if written <= 0:
                    raise OSError("short XHS staging write")
                offset += written
            os.fsync(held)
            identity = _trusted_identity(os.fstat(held), max_bytes=self.max_bytes)
            if identity.size_bytes != len(payload):
                raise UnsafeXhsArtifactStore("XHS staging write changed identity.")
            named = self._open_file("stage", name)
            try:
                if _trusted_identity(os.fstat(named), max_bytes=self.max_bytes) != identity:
                    raise UnsafeXhsArtifactStore("XHS staging name changed while writing.")
            finally:
                os.close(named)
            return identity
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def inspect(self, area: str, name: str) -> XhsArtifactInspection:
        descriptor: int | None = None
        try:
            descriptor = self._open_file(area, name)
            return XhsArtifactInspection(
                "trusted",
                _trusted_identity(os.fstat(descriptor), max_bytes=self.max_bytes),
            )
        except FileNotFoundError:
            return XhsArtifactInspection("missing")
        except (OSError, ValueError, TypeError):
            return XhsArtifactInspection("ambiguous")
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def read(self, area: str, name: str, identity: XhsArtifactIdentity) -> bytes:
        descriptor = self._open_file(area, name)
        try:
            if _trusted_identity(os.fstat(descriptor), max_bytes=self.max_bytes) != identity:
                raise UnsafeXhsArtifactStore("XHS artifact identity changed.")
            payload = b""
            while len(payload) <= self.max_bytes:
                chunk = os.read(descriptor, min(65536, self.max_bytes + 1 - len(payload)))
                if not chunk:
                    break
                payload += chunk
            if len(payload) > self.max_bytes or _trusted_identity(os.fstat(descriptor), max_bytes=self.max_bytes) != identity:
                raise UnsafeXhsArtifactStore("XHS artifact changed while reading.")
            return payload
        finally:
            os.close(descriptor)

    def promote(self, stage_name: str, final_name: str, identity: XhsArtifactIdentity) -> XhsArtifactIdentity:
        descriptor = self._stage_handles.get(stage_name)
        if descriptor is None or _trusted_identity(os.fstat(descriptor), max_bytes=self.max_bytes) != identity:
            raise UnsafeXhsArtifactStore("XHS staging handle identity changed.")
        stage = self.inspect("stage", stage_name)
        if stage.status != "trusted" or stage.identity != identity:
            raise UnsafeXhsArtifactStore("XHS staging name identity changed.")
        if self.inspect("final", final_name).status != "missing":
            raise UnsafeXhsArtifactStore("XHS final evidence target is not absent.")
        if not _rename_windows_fd_relative(descriptor, self._final_parent, final_name):
            raise UnsafeXhsArtifactStore("XHS handle-bound promotion failed.")
        os.fsync(descriptor)
        os.close(self._stage_handles.pop(stage_name))
        promoted = self.inspect("final", final_name)
        if promoted.status != "trusted" or promoted.identity != identity:
            raise UnsafeXhsArtifactStore("XHS promotion identity changed.")
        return identity

    def recover_promote(self, stage_name: str, final_name: str, identity: XhsArtifactIdentity) -> XhsArtifactIdentity:
        descriptor = self._open_file("stage", stage_name, writable=True)
        try:
            if _trusted_identity(os.fstat(descriptor), max_bytes=self.max_bytes) != identity:
                raise UnsafeXhsArtifactStore("Recovered XHS staging identity changed.")
            if self.inspect("final", final_name).status != "missing":
                raise UnsafeXhsArtifactStore("Recovered XHS final target is not absent.")
            if not _rename_windows_fd_relative(descriptor, self._final_parent, final_name):
                raise UnsafeXhsArtifactStore("Recovered XHS handle-bound promotion failed.")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        promoted = self.inspect("final", final_name)
        if promoted.status != "trusted" or promoted.identity != identity:
            raise UnsafeXhsArtifactStore("Recovered XHS promotion identity changed.")
        return identity

    def demote_and_discard(self, final_name: str, stage_name: str, identity: XhsArtifactIdentity) -> bool:
        final = self.inspect("final", final_name)
        stage = self.inspect("stage", stage_name)
        if final.status == "missing":
            return stage.status == "missing" or self.discard_stage(stage_name, identity)
        if final.status != "trusted" or final.identity != identity or stage.status != "missing":
            return False
        descriptor = self._open_file("final", final_name, writable=True)
        try:
            if _trusted_identity(os.fstat(descriptor), max_bytes=self.max_bytes) != identity:
                return False
            if not _rename_windows_fd_relative(descriptor, self._stage_parent, stage_name):
                return False
        finally:
            os.close(descriptor)
        return self.discard_stage(stage_name, identity)

    def discard_stage(self, name: str, identity: XhsArtifactIdentity) -> bool:
        held = self._stage_handles.get(name)
        if held is not None:
            if _trusted_identity(os.fstat(held), max_bytes=self.max_bytes) != identity:
                return False
            deleted = _delete_open_file(held)
            if deleted:
                os.close(self._stage_handles.pop(name))
            return deleted
        descriptor: int | None = None
        try:
            descriptor = self._open_file("stage", name)
            if _trusted_identity(os.fstat(descriptor), max_bytes=self.max_bytes) != identity:
                return False
            return _delete_open_file(descriptor)
        except FileNotFoundError:
            return True
        except (OSError, ValueError, TypeError):
            return False
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def discard_owned_stage(self, name: str) -> bool:
        descriptor = self._stage_handles.get(name)
        if descriptor is None:
            return False
        try:
            deleted = _delete_open_file(descriptor)
        except (OSError, ValueError, TypeError):
            return False
        if deleted:
            os.close(self._stage_handles.pop(name))
        return deleted


def discard_xhs_staging_file(
    runtime_root: Path,
    path: Path,
    *,
    max_bytes: int,
) -> bool:
    """Delete one exact private stage name; never accept a formal artifact path."""
    if max_bytes < 1:
        return False
    try:
        root = runtime_root.resolve(strict=True)
        relative = path.relative_to(runtime_root)
    except (OSError, ValueError):
        return False
    if (
        relative.parts[:3] != ("evidence", "xhs", ".staging")
        or len(relative.parts) != 4
        or _STAGE_NAME.fullmatch(relative.parts[3]) is None
        or root.is_symlink()
        or (hasattr(root, "is_junction") and root.is_junction())
    ):
        return False

    inspection = inspect_contained_artifact(root, relative.as_posix())
    if inspection.status == "missing":
        return True
    if (
        inspection.status != "trusted"
        or inspection.identity is None
        or inspection.size_bytes is None
        or inspection.size_bytes > max_bytes
    ):
        return False
    if os.name == "nt":
        return remove_contained_regular(
            root,
            relative.as_posix(),
            limit=max_bytes,
            expected_identity=inspection.identity,
        )
    # A POSIX pathname unlink cannot bind the removal atomically to the
    # inspected descriptor identity.  Retain the stage for manual recovery.
    return False
