"""Pure entity/device helpers shared by the config and options flows.

Self-contained validation and selector helpers with no dependency on the flow
classes, so the flow modules only orchestrate steps. Names keep their leading
underscore to preserve the existing (re-exported) import surface.
"""

from __future__ import annotations

import uuid
from typing import Any

import voluptuous as vol
from homeassistant.helpers import selector

from .device_configuration import entity_id as _entity_id
from .device_configuration import normalize_devices


def _gen_id() -> str:
    return uuid.uuid4().hex[:8]


def _friendly(hass: Any, entity_id: str) -> str:
    if not entity_id:
        return ""
    state = hass.states.get(entity_id)
    name = getattr(state, "attributes", {}).get("friendly_name") if state is not None else None
    return name.strip() if isinstance(name, str) and name.strip() else entity_id


def _sensor_entity_id(value: Any) -> str | None:
    return _entity_id(value, frozenset({"sensor"}))


def _optional_entity_key(name: str, default: Any = None) -> Any:
    """Avoid injecting an empty string into HA's native EntitySelector."""
    if isinstance(default, str) and default.strip():
        return vol.Optional(name, default=default)
    return vol.Optional(name)


def _entity_selector(domains: str | list[str], *, multiple: bool = False) -> Any:
    return selector.EntitySelector(selector.EntitySelectorConfig(domain=domains, multiple=multiple))


def _entry_current(entry: Any, key: str, default: Any = None) -> Any:
    options = getattr(entry, "options", {}) or {}
    data = getattr(entry, "data", {}) or {}
    if key in options:
        return options[key]
    return data.get(key, default)


def _normalize_options_devices(value: Any) -> list[dict[str, Any]]:
    """Compatibility adapter for strict household configuration edits."""
    return normalize_devices(value, strict=True)
