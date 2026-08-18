"""Shared credential redaction for XHS adapter and durable evidence boundaries."""

from __future__ import annotations

import re
from typing import Any


_SENSITIVE_NAME = re.compile(
    r"(?:cookie|token|credential|authorization|password|secret|session|api[_-]?key)",
    re.IGNORECASE,
)
_STRUCTURED_VALUE_KEYS = frozenset({"value", "values"})


def redact_credentials(value: Any) -> Any:
    """Redact both credential keys and ``{name, value}`` header entries."""
    if isinstance(value, dict):
        semantic_name = value.get("name")
        semantic_secret = (
            isinstance(semantic_name, str)
            and _SENSITIVE_NAME.search(semantic_name) is not None
        )
        return {
            str(key): (
                "[redacted]"
                if _SENSITIVE_NAME.search(str(key))
                or (
                    semantic_secret
                    and str(key).casefold() in _STRUCTURED_VALUE_KEYS
                )
                else redact_credentials(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_credentials(item) for item in value]
    return value
