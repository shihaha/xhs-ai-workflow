"""Contained file reads and byte-deterministic ZIP construction."""

from __future__ import annotations

from hashlib import sha256
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import zipfile
from uuid import uuid4


MAX_MATERIAL_BYTES = 50 * 1024 * 1024


class UnsafeContentPath(ValueError):
    pass


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
        if target.exists() and (
            target.is_symlink() or (hasattr(target, "is_junction") and target.is_junction())
            or not target.is_file()
        ):
            raise UnsafeContentPath("Output target must be a regular file or absent.")
        target.resolve(strict=False).relative_to(resolved_root)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        temporary.write_bytes(payload)
        if temporary.is_symlink() or not temporary.is_file():
            raise UnsafeContentPath("Temporary output is not a regular file.")
        temporary.resolve(strict=True).relative_to(resolved_root)
        temporary.replace(target)
        written = read_contained_regular(root, posix.as_posix(), limit=max(len(payload), 1))
        if written != payload:
            raise UnsafeContentPath("Output verification failed.")
    except UnsafeContentPath:
        raise
    except (OSError, ValueError, TypeError) as error:
        raise UnsafeContentPath("Output path is unavailable or escapes the runtime directory.") from error
    finally:
        if "temporary" in locals():
            temporary.unlink(missing_ok=True)


def deterministic_zip(entries: dict[str, bytes], manifest: dict[str, object]) -> bytes:
    normalized: dict[str, bytes] = {}
    for name, value in entries.items():
        path = PurePosixPath(name)
        if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
            raise UnsafeContentPath("ZIP entry path is unsafe.")
        canonical = path.as_posix()
        if canonical in normalized or canonical.casefold() in {item.casefold() for item in normalized}:
            raise UnsafeContentPath("ZIP entry path collision.")
        normalized[canonical] = value
    manifest_payload = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if "manifest.json".casefold() in {item.casefold() for item in normalized}:
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
    return target.getvalue()


def entry_manifest(entries: dict[str, bytes]) -> list[dict[str, object]]:
    return [
        {"path": name, "sha256": sha256(entries[name]).hexdigest(), "size_bytes": len(entries[name])}
        for name in sorted(entries)
    ]


def _identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
