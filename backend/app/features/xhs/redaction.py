"""Shared credential redaction for XHS adapter and durable evidence boundaries."""

from __future__ import annotations

import re
from typing import Any


_SENSITIVE_NAMES = frozenset({
    "access-token",
    "api-key",
    "apikey",
    "auth-token",
    "authorization",
    "client-secret",
    "clientsecret",
    "cookie",
    "cookie-string",
    "credential",
    "credentials",
    "id-token",
    "password",
    "passwd",
    "proxy-authorization",
    "refresh-token",
    "secret",
    "session",
    "session-token",
    "set-cookie",
    "token",
    "x-api-key",
    "x-auth-token",
    "x-csrf-token",
    "x-xsrf-token",
})
_STRUCTURED_VALUE_KEYS = frozenset({"value", "values"})
_ACRONYM_BOUNDARY = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_NAME_SEPARATOR = re.compile(r"[^A-Za-z0-9]+")


def redact_credentials(value: Any) -> Any:
    """Redact both credential keys and ``{name, value}`` header entries."""
    if isinstance(value, dict):
        semantic_name = value.get("name")
        semantic_secret = (
            isinstance(semantic_name, str)
            and _normalized_name(semantic_name) in _SENSITIVE_NAMES
        )
        return {
            str(key): (
                "[redacted]"
                if _normalized_name(str(key)) in _SENSITIVE_NAMES
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


def _normalized_name(value: str) -> str:
    words = _ACRONYM_BOUNDARY.sub(r"\1-\2", value.strip())
    words = _CAMEL_BOUNDARY.sub(r"\1-\2", words)
    return _NAME_SEPARATOR.sub("-", words).strip("-").casefold()
