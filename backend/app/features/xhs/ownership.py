"""One canonical, conflict-detecting owner extraction for normalized XHS facts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


_SAFE_OWNER = re.compile(r"[A-Za-z0-9_-]{1,500}")
_OWNER_KEYS = ("user_id", "userId")
_OWNER_CONTAINERS = ("row", "profile", "author", "user", "authorInfo", "author_info")


class OwnerIdentityError(ValueError):
    """A retained owner alias is malformed or conflicts with another alias."""


def canonical_owner_id(
    *sources: Mapping[str, Any] | None, include_record_id: bool = False
) -> str | None:
    """Return one owner only when every supported retained alias agrees.

    ``id`` is accepted solely for profile records. Note row ids are note identities,
    never account owners. The traversal is intentionally bounded to the known public
    record and author containers; it never treats unrelated IDs in a response as an
    owner claim.
    """

    values: set[str] = set()
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        for mapping in _owner_mappings(source):
            for key in _OWNER_KEYS:
                if key in mapping:
                    values.add(_normalized_owner(mapping[key], key))
            if include_record_id and "id" in mapping:
                values.add(_normalized_owner(mapping["id"], "id"))
    if len(values) > 1:
        raise OwnerIdentityError("retained owner aliases conflict")
    return next(iter(values), None)


def _owner_mappings(source: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = [source]
    pending = [source]
    while pending:
        current = pending.pop()
        for key in _OWNER_CONTAINERS:
            candidate = current.get(key)
            if isinstance(candidate, Mapping) and candidate not in found:
                found.append(candidate)
                pending.append(candidate)
    return found


def _normalized_owner(value: object, key: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise OwnerIdentityError(f"owner alias {key} is malformed")
    normalized = str(value).strip()
    if _SAFE_OWNER.fullmatch(normalized) is None:
        raise OwnerIdentityError(f"owner alias {key} is malformed")
    return normalized
