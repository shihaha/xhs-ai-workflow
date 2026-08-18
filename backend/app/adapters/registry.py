"""Capability-based selection for replaceable adapter implementations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


class AdapterNotFound(LookupError):
    """Raised when no registered adapter can perform a requested capability."""


@dataclass(frozen=True)
class AdapterRegistration:
    name: str
    adapter: Any
    capabilities: frozenset[str]
    priority: int


class AdapterRegistry:
    """Resolve a capability to the registered adapter with the greatest priority."""

    def __init__(self) -> None:
        self._registrations: list[AdapterRegistration] = []

    def register(
        self,
        *,
        name: str,
        adapter: Any,
        capabilities: Iterable[str],
        priority: int,
    ) -> None:
        normalized_capabilities = frozenset(capability.strip() for capability in capabilities)
        if not name.strip():
            raise ValueError("Adapter registrations require a name.")
        if not normalized_capabilities or "" in normalized_capabilities:
            raise ValueError("Adapter registrations require non-empty capabilities.")
        self._registrations.append(
            AdapterRegistration(
                name=name,
                adapter=adapter,
                capabilities=normalized_capabilities,
                priority=priority,
            )
        )

    def resolve(self, capability: str) -> Any:
        candidates = [
            registration
            for registration in self._registrations
            if capability in registration.capabilities
        ]
        if not candidates:
            raise AdapterNotFound(f"No adapter is registered for capability {capability!r}.")
        return max(candidates, key=lambda registration: registration.priority).adapter

    def registrations(self) -> tuple[AdapterRegistration, ...]:
        return tuple(self._registrations)


def build_default_registry(settings: Any) -> AdapterRegistry:
    """Register production adapters from trusted application settings only."""
    from backend.app.adapters.xhs_cli_read import XhsCliReadAdapter

    registry = AdapterRegistry()
    adapter = XhsCliReadAdapter.from_settings(settings)
    registry.register(
        name="xhs-cli-read",
        adapter=adapter,
        capabilities=adapter.capabilities,
        priority=100,
    )
    return registry
