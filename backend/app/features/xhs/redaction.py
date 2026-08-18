"""Shared credential redaction for XHS adapter and durable evidence boundaries."""

from __future__ import annotations

import re
from typing import Any


_CREDENTIAL_TERMS = frozenset({
    "access",
    "apikey",
    "auth",
    "authorization",
    "bearer",
    "clientsecret",
    "cookie",
    "credential",
    "credentials",
    "csrf",
    "jwt",
    "passwd",
    "password",
    "refresh",
    "secret",
    "session",
    "token",
    "xsrf",
})
_CREDENTIAL_COMBINATIONS = frozenset({
    ("api", "key"),
    ("client", "secret"),
    ("cookie", "string"),
    ("proxy", "authorization"),
    ("set", "cookie"),
})
_TOKEN_QUALIFIERS = frozenset({
    "access",
    "auth",
    "authorization",
    "bearer",
    "csrf",
    "id",
    "jwt",
    "refresh",
    "session",
    "xsrf",
})
_COOKIE_QUALIFIERS = frozenset({"auth", "csrf", "session", "xsrf"})
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
            and _is_credential_name(semantic_name)
        )
        return {
            str(key): (
                "[redacted]"
                if _is_credential_name(str(key))
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


def _is_credential_name(value: str) -> bool:
    """Classify a complete canonical name, never an arbitrary substring."""
    tokens = _credential_name_tokens(value)
    if tokens[:1] == ("x",):
        tokens = tokens[1:]
    if len(tokens) == 1 and tokens[0] in _CREDENTIAL_TERMS:
        return True
    if tokens in _CREDENTIAL_COMBINATIONS:
        return True
    if len(tokens) != 2:
        return False
    qualifier, carrier = tokens
    return (
        carrier == "token" and qualifier in _TOKEN_QUALIFIERS
    ) or (
        carrier == "cookie" and qualifier in _COOKIE_QUALIFIERS
    )


def _credential_name_tokens(value: str) -> tuple[str, ...]:
    """Split camelCase/acronyms and separators into case-folded name tokens."""
    words = _ACRONYM_BOUNDARY.sub(r"\1-\2", value.strip())
    words = _CAMEL_BOUNDARY.sub(r"\1-\2", words)
    canonical = _NAME_SEPARATOR.sub("-", words).strip("-").casefold()
    return tuple(token for token in canonical.split("-") if token)
