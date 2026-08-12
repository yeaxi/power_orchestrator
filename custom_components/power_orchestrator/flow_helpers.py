"""Pure entity/device helpers shared by the config and options flows.

Self-contained validation and selector helpers with no dependency on the flow
classes, so the flow modules only orchestrate steps. Names keep their leading
underscore to preserve the existing (re-exported) import surface.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.helpers import selector

from .const import (
    CONF_DEVICE_ACTUATORS,
    CONF_DEVICE_ENTITY,
    CONF_DEVICE_EXPECTED_POWER,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_DEVICE_POWER_SENSOR,
    CONF_DEVICE_RESTORE_ENABLED,
    CONF_PRIORITY,
    CONF_SHED_PRIORITY,
)


def _gen_id() -> str:
    return uuid.uuid4().hex[:8]


def _friendly(hass: Any, entity_id: str) -> str:
    if not entity_id:
        return ""
    state = hass.states.get(entity_id)
    name = getattr(state, "attributes", {}).get("friendly_name") if state is not None else None
    return name.strip() if isinstance(name, str) and name.strip() else entity_id


def _entity_id(value: Any, domains: frozenset[str]) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if "." not in value:
        return None
    domain, object_id = value.split(".", 1)
    return value if domain in domains and object_id else None


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
    """Validate and normalize structured device mappings."""
    if not isinstance(value, list):
        raise ValueError("devices must be a list")
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_entities: set[str] = set()
    seen_priorities: set[int] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise ValueError("device mapping must be an object")
        device_id = raw.get(CONF_DEVICE_ID)
        if not isinstance(device_id, str) or not device_id.strip() or device_id in seen_ids:
            raise ValueError("device IDs must be unique")
        device_id = device_id.strip()
        entity = _entity_id(
            raw.get(CONF_DEVICE_ENTITY), frozenset({"switch", "light", "input_boolean"})
        )
        if entity is None or entity in seen_entities:
            raise ValueError("control entities must be valid and unique")
        raw_actuators = raw.get(CONF_DEVICE_ACTUATORS, ())
        if raw_actuators in (None, ""):
            raw_actuators = ()
        if isinstance(raw_actuators, str):
            raw_actuators = (raw_actuators,)
        if not isinstance(raw_actuators, (list, tuple)):
            raise ValueError("actuators must be a list")
        actuators: list[str] = []
        for raw_actuator in raw_actuators:
            actuator = _entity_id(
                raw_actuator, frozenset({"switch", "light", "input_boolean", "climate"})
            )
            if (
                actuator is None
                or actuator == entity
                or actuator in actuators
                or actuator in seen_entities
            ):
                raise ValueError("logical actuator entities must be valid and unique")
            actuators.append(actuator)
        expected_raw = raw.get(CONF_DEVICE_EXPECTED_POWER)
        if expected_raw is None or isinstance(expected_raw, bool):
            raise ValueError("expected power must be finite")
        try:
            expected = float(expected_raw)
        except (TypeError, ValueError):
            raise ValueError("expected power must be finite") from None
        if not math.isfinite(expected) or not 1 <= expected <= 50000:
            raise ValueError("expected power is outside the allowed range")
        sensor = raw.get(CONF_DEVICE_POWER_SENSOR)
        sensor = None if sensor in (None, "") else _sensor_entity_id(sensor)
        if raw.get(CONF_DEVICE_POWER_SENSOR) not in (None, "") and sensor is None:
            raise ValueError("power sensor must be a sensor entity")
        name = raw.get(CONF_DEVICE_NAME) or entity
        if not isinstance(name, str) or not name.strip():
            name = entity

        def positive(raw_value: Any, default: int, label: str) -> int:
            if raw_value is None:
                return default
            if isinstance(raw_value, bool):
                raise ValueError(f"{label} must be an integer")
            try:
                converted = float(raw_value)
            except (TypeError, ValueError):
                raise ValueError(f"{label} must be an integer") from None
            if not math.isfinite(converted) or converted < 1 or converted != int(converted):
                raise ValueError(f"{label} must be a positive integer")
            return int(converted)

        priority = positive(raw.get(CONF_PRIORITY), index + 1, "priority")
        if priority in seen_priorities:
            raise ValueError("priorities must be unique")
        shed_priority = positive(raw.get(CONF_SHED_PRIORITY), priority, "shed priority")
        normalized.append(
            {
                CONF_DEVICE_ID: device_id,
                CONF_DEVICE_NAME: name.strip(),
                CONF_DEVICE_ENTITY: entity,
                CONF_DEVICE_EXPECTED_POWER: int(math.ceil(expected)),
                CONF_DEVICE_POWER_SENSOR: sensor,
                CONF_PRIORITY: priority,
                CONF_SHED_PRIORITY: shed_priority,
                CONF_DEVICE_ACTUATORS: actuators,
                CONF_DEVICE_RESTORE_ENABLED: bool(raw.get(CONF_DEVICE_RESTORE_ENABLED, False)),
            }
        )
        seen_ids.add(device_id)
        seen_entities.update((entity, *actuators))
        seen_priorities.add(priority)
    return normalized
