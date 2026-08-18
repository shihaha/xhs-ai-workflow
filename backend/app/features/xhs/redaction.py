"""Shared credential redaction for XHS adapter and durable evidence boundaries."""

from __future__ import annotations

import re
from typing import Any


_SINGLE_TOKEN_CREDENTIAL_CORES = frozenset({
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
    "secret",
    "token",
    "xsrf",
})
_TOKEN_QUALIFIERS = frozenset({
    "access",
    "api",
    "auth",
    "authorization",
    "bearer",
    "csrf",
    "id",
    "jwt",
    "oauth",
    "refresh",
    "session",
    "xsrf",
})
_TOKEN_QUALIFIER_CHAINS = frozenset({("personal", "access")})
_CARRIER_QUALIFIERS = {
    "authorization": frozenset({"proxy"}),
    "cookie": frozenset({"auth", "csrf", "session", "set", "xsrf"}),
    "id": frozenset({"session"}),
    "key": frozenset({"api"}),
    "secret": frozenset({"api", "client"}),
    "session": frozenset({"web"}),
    "string": frozenset({"cookie"}),
}
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
    if len(tokens) == 1:
        return tokens[0] in _SINGLE_TOKEN_CREDENTIAL_CORES
    qualifiers, carrier = tokens[:-1], tokens[-1]
    if carrier == "token":
        return (
            len(qualifiers) == 1 and qualifiers[0] in _TOKEN_QUALIFIERS
        ) or qualifiers in _TOKEN_QUALIFIER_CHAINS
    return (
        len(qualifiers) == 1
        and qualifiers[0] in _CARRIER_QUALIFIERS.get(carrier, frozenset())
    )


def _credential_name_tokens(value: str) -> tuple[str, ...]:
    """Split camelCase/acronyms and separators into case-folded name tokens."""
    words = _ACRONYM_BOUNDARY.sub(r"\1-\2", value.strip())
    words = _CAMEL_BOUNDARY.sub(r"\1-\2", words)
    canonical = _NAME_SEPARATOR.sub("-", words).strip("-").casefold()
    return tuple(token for token in canonical.split("-") if token)
