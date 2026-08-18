"""Narrow permanent-delete boundary for private XHS staging files only."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from backend.app.features.content.export import (
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
    return _discard_posix_stage(
        root,
        relative.parts[:-1],
        relative.parts[-1],
        expected_identity=inspection.identity,
        max_bytes=max_bytes,
    )


def _discard_posix_stage(
    root: Path,
    parent_parts: tuple[str, ...],
    name: str,
    *,
    expected_identity: tuple[int, int, int, int],
    max_bytes: int,
) -> bool:
    root_descriptor: int | None = None
    file_descriptor: int | None = None
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    try:
        root_descriptor = os.open(root, os.O_RDONLY | nofollow | directory)
        parent_descriptor = root_descriptor
        for component in parent_parts:
            parent_descriptor = os.open(
                component,
                os.O_RDONLY | nofollow | directory,
                dir_fd=parent_descriptor,
            )
            if root_descriptor != parent_descriptor:
                os.close(root_descriptor)
            root_descriptor = parent_descriptor
        file_descriptor = os.open(
            name,
            os.O_RDONLY | nofollow,
            dir_fd=parent_descriptor,
        )
        opened = os.fstat(file_descriptor)
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        named_identity = (named.st_dev, named.st_ino, named.st_size, named.st_mtime_ns)
        if (
            not stat.S_ISREG(opened.st_mode)
            or identity != expected_identity
            or named_identity != identity
            or opened.st_size > max_bytes
        ):
            return False
        os.unlink(name, dir_fd=parent_descriptor)
        return True
    except (OSError, TypeError, ValueError):
        return False
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if root_descriptor is not None:
            os.close(root_descriptor)
