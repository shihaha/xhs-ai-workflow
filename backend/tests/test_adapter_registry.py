import pytest

from backend.app.adapters.registry import AdapterNotFound, AdapterRegistry


def test_registry_resolves_highest_priority_adapter_for_capability() -> None:
    """Ignoring priority could route collection through a lower-ranked third-party source."""
    registry = AdapterRegistry()
    fallback = object()
    preferred = object()
    registry.register(
        name="fallback",
        adapter=fallback,
        capabilities={"search_notes"},
        priority=10,
    )
    registry.register(
        name="preferred",
        adapter=preferred,
        capabilities={"search_notes", "fetch_account"},
        priority=100,
    )

    assert registry.resolve("search_notes") is preferred


def test_registry_rejects_a_capability_without_a_registered_adapter() -> None:
    """Returning an arbitrary adapter for an unsupported operation would break adapter isolation."""
    with pytest.raises(AdapterNotFound):
        AdapterRegistry().resolve("collect_shop")
