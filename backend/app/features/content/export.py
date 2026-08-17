"""Contained file reads and byte-deterministic ZIP construction."""

from __future__ import annotations

from hashlib import sha256
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import zipfile
import unicodedata


MAX_MATERIAL_BYTES = 50 * 1024 * 1024
MAX_PACKAGE_BYTES = 250 * 1024 * 1024


class UnsafeContentPath(ValueError):
    pass


ArtifactReference = tuple[str, tuple[int, int] | None]


def windows_artifact_reference(root: Path, relative: str) -> ArtifactReference | None:
    """Return a Windows-equivalent absolute key plus physical file identity.

    Untrusted, missing, linked or out-of-runtime paths intentionally have no
    identity and therefore can never authorize deletion.
    """
    posix = PurePosixPath(relative)
    if (
        posix.is_absolute() or not posix.parts
        or any(part in {"", ".", ".."} or ":" in part or "\\" in part for part in posix.parts)
    ):
        return None
    try:
        resolved_root = root.resolve(strict=True)
        candidate = root.joinpath(*posix.parts)
        current = root
        for part in posix.parts:
            current = current / part
            if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
                return None
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(resolved_root)
        absolute_key = unicodedata.normalize("NFC", str(resolved)).casefold()
        try:
            metadata = candidate.stat()
        except FileNotFoundError:
            return absolute_key, None
        if not stat.S_ISREG(metadata.st_mode):
            return absolute_key, None
        return absolute_key, (metadata.st_dev, metadata.st_ino)
    except (OSError, ValueError, TypeError):
        return None


def windows_artifact_references_conflict(
    left: ArtifactReference | None,
    right: ArtifactReference | None,
) -> bool:
    return left is not None and right is not None and (
        left[0] == right[0]
        or (left[1] is not None and right[1] is not None and left[1] == right[1])
    )


def read_contained_regular(root: Path, relative: str, *, limit: int = MAX_MATERIAL_BYTES) -> bytes:
    posix = PurePosixPath(relative)
    if (
        posix.is_absolute() or not posix.parts
        or any(part in {"", ".", ".."} or ":" in part or "\\" in part for part in posix.parts)
    ):
        raise UnsafeContentPath("Path must be a normalized runtime-relative file path.")
    try:
        resolved_root = root.resolve(strict=True)
        candidate = root.joinpath(*posix.parts)
        current = root
        for part in posix.parts:
            current = current / part
            if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
                raise UnsafeContentPath("Symlinks and junctions are not accepted.")
        before_resolved = candidate.resolve(strict=True)
        before_resolved.relative_to(resolved_root)
        before = candidate.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise UnsafeContentPath("Material must be a bounded regular file.")
        with candidate.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before):
                raise UnsafeContentPath("Material changed while opening.")
            payload = stream.read(limit + 1)
            after_handle = os.fstat(stream.fileno())
        after_resolved = candidate.resolve(strict=True)
        after = candidate.stat()
        if (
            len(payload) > limit or _identity(after_handle) != _identity(opened)
            or after_resolved != before_resolved or _identity(after) != _identity(opened)
        ):
            raise UnsafeContentPath("Material changed while reading.")
        return payload
    except UnsafeContentPath:
        raise
    except (OSError, ValueError, TypeError) as error:
        raise UnsafeContentPath("Material is unavailable or escapes the runtime directory.") from error


def write_contained_atomic(root: Path, relative: str, payload: bytes) -> None:
    posix = PurePosixPath(relative)
    if (
        posix.is_absolute() or len(posix.parts) < 2
        or any(part in {"", ".", ".."} or ":" in part or "\\" in part for part in posix.parts)
    ):
        raise UnsafeContentPath("Output path must be a normalized runtime-relative file path.")
    try:
        resolved_root = root.resolve(strict=True)
        current = root
        for part in posix.parts[:-1]:
            current = current / part
            if current.exists():
                if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
                    raise UnsafeContentPath("Output parents cannot be symlinks or junctions.")
                current.resolve(strict=True).relative_to(resolved_root)
            else:
                current.mkdir()
                current.resolve(strict=True).relative_to(resolved_root)
        target = root.joinpath(*posix.parts)
        if target.exists():
            raise UnsafeContentPath("Output target must be absent; existing artifacts are never overwritten.")
        target.resolve(strict=False).relative_to(resolved_root)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = os.open(target, flags, 0o600)
        opened = os.fstat(descriptor)
        resolved_after_open = target.resolve(strict=True)
        resolved_after_open.relative_to(resolved_root)
        path_stat = target.stat()
        if (
            not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(path_stat)
            or target.is_symlink()
        ):
            raise UnsafeContentPath("Opened output identity is not the verified runtime file.")
        view = memoryview(payload)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError("short output write")
            written += count
        os.fsync(descriptor)
        final_handle = os.fstat(descriptor)
        if final_handle.st_size != len(payload) or final_handle.st_dev != opened.st_dev or final_handle.st_ino != opened.st_ino:
            raise UnsafeContentPath("Output handle identity changed while writing.")
        os.close(descriptor)
        descriptor = None
        written = read_contained_regular(root, posix.as_posix(), limit=max(len(payload), 1))
        if written != payload:
            raise UnsafeContentPath("Output verification failed.")
    except UnsafeContentPath:
        raise
    except (OSError, ValueError, TypeError) as error:
        raise UnsafeContentPath("Output path is unavailable or escapes the runtime directory.") from error
    finally:
        if "descriptor" in locals() and descriptor is not None:
            os.close(descriptor)


def remove_contained_regular(root: Path, relative: str, *, limit: int = MAX_PACKAGE_BYTES) -> bool:
    """Delete only the already-opened Windows file, never a later path target.

    On platforms without handle-bound deletion support the conservative result is
    to retain the artifact; callers still mark its database reservation failed.
    """
    posix = PurePosixPath(relative)
    if (
        posix.is_absolute() or not posix.parts
        or any(part in {"", ".", ".."} or ":" in part or "\\" in part for part in posix.parts)
    ):
        return False
    descriptor: int | None = None
    try:
        resolved_root = root.resolve(strict=True)
        candidate = root.joinpath(*posix.parts)
        current = root
        for part in posix.parts:
            current = current / part
            if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
                return False
        resolved_before = candidate.resolve(strict=True)
        resolved_before.relative_to(resolved_root)
        before = candidate.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit or os.name != "nt":
            return False
        descriptor = _open_delete_handle(candidate)
        opened = os.fstat(descriptor)
        resolved_after = candidate.resolve(strict=True)
        after = candidate.stat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or _identity(opened) != _identity(before)
            or _identity(after) != _identity(opened)
            or resolved_after != resolved_before
        ):
            return False
        return _delete_open_file(descriptor)
    except (UnsafeContentPath, OSError, ValueError, TypeError):
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _open_delete_handle(path: Path) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    delete_access = 0x00010000
    share_all = 0x00000001 | 0x00000002 | 0x00000004
    open_existing = 3
    open_reparse_point = 0x00200000
    handle = create_file(
        str(path), delete_access | 0x80000000, share_all, None,
        open_existing, open_reparse_point, None,
    )
    invalid_handle = wintypes.HANDLE(-1).value
    if handle == invalid_handle:
        raise OSError(ctypes.get_last_error(), "Unable to open deletion handle.")
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except Exception:
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
        raise


def _delete_open_file(descriptor: int) -> bool:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FileDispositionInfo(ctypes.Structure):
        _fields_ = [("delete_file", wintypes.BOOL)]

    disposition = FileDispositionInfo(True)
    set_information = ctypes.WinDLL("kernel32", use_last_error=True).SetFileInformationByHandle
    set_information.argtypes = (
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    )
    set_information.restype = wintypes.BOOL
    succeeded = set_information(
        msvcrt.get_osfhandle(descriptor), 4,
        ctypes.byref(disposition), ctypes.sizeof(disposition),
    )
    return bool(succeeded)


def _windows_component_key(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    stem = normalized.split(".", 1)[0].rstrip(" .").casefold()
    reserved = {
        "con", "prn", "aux", "nul", "conin$", "conout$", "clock$",
        *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10)),
        "com¹", "com²", "com³", "lpt¹", "lpt²", "lpt³",
    }
    if (
        not normalized or normalized.rstrip(" .") != normalized or stem in reserved
        or any(
            ord(char) < 32 or 127 <= ord(char) <= 159 or char in '<>:"/\\|?*'
            for char in normalized
        )
    ):
        raise UnsafeContentPath("ZIP entry path is unsafe on Windows.")
    return normalized.casefold().rstrip(" .")


def deterministic_zip(entries: dict[str, bytes], manifest: dict[str, object]) -> bytes:
    if len(entries) > 127 or sum(len(value) for value in entries.values()) > MAX_PACKAGE_BYTES:
        raise UnsafeContentPath("ZIP entry count or uncompressed size limit exceeded.")
    normalized: dict[str, bytes] = {}
    windows_keys: set[str] = set()
    for name, value in entries.items():
        name = unicodedata.normalize("NFC", name)
        path = PurePosixPath(name)
        if path.is_absolute() or not path.parts or any(
            part in {"", ".", ".."} or ":" in part or "\\" in part
            or any(ord(char) < 32 for char in part) for part in path.parts
        ):
            raise UnsafeContentPath("ZIP entry path is unsafe.")
        canonical = path.as_posix()
        windows_key = "/".join(_windows_component_key(part) for part in path.parts)
        if canonical in normalized or windows_key in windows_keys:
            raise UnsafeContentPath("ZIP entry path collision.")
        normalized[canonical] = value
        windows_keys.add(windows_key)
    manifest_payload = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if _windows_component_key("manifest.json") in windows_keys:
        raise UnsafeContentPath("Manifest entry collision.")
    normalized["manifest.json"] = manifest_payload
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(normalized):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.flag_bits = 0x800
            archive.writestr(info, normalized[name])
    payload = target.getvalue()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        total_compressed = sum(max(info.compress_size, 1) for info in archive.infolist())
        total_uncompressed = sum(info.file_size for info in archive.infolist())
        if total_uncompressed > MAX_PACKAGE_BYTES or total_uncompressed / max(total_compressed, 1) > 500:
            raise UnsafeContentPath("ZIP compression ratio limit exceeded.")
    return payload


def entry_manifest(entries: dict[str, bytes]) -> list[dict[str, object]]:
    return [
        {"path": name, "sha256": sha256(entries[name]).hexdigest(), "size_bytes": len(entries[name])}
        for name in sorted(entries)
    ]


def _identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
