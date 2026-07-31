"""Minimal entity registry mock for local integration tests."""
from unittest.mock import MagicMock


def async_get(hass):
    """Return a registry attached by a test, or an empty registry."""
    return getattr(hass, "entity_registry", MagicMock(entities={}))
