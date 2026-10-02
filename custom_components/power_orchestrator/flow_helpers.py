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
    CONF_DEVICE_BATTERY_MIN_SOC,
    CONF_DEVICE_COMMAND_ENTITY,
    CONF_DEVICE_EMERGENCY_OFF_ENTITIES,
    CONF_DEVICE_ENTITY,
    CONF_DEVICE_EXPECTED_POWER,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_DEVICE_POWER_SENSOR,
    CONF_DEVICE_READBACK_ENTITIES,
    CONF_PRIORITY,
    CONF_SHED_PRIORITY,
)
from .power_model import parse_battery_min_soc

_CONTROL_DOMAINS = frozenset({"switch", "light", "input_boolean", "climate", "humidifier"})


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


def _positive_integer(value: Any, default: int, label: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    try:
        converted = float(value)
    except TypeError, ValueError:
        raise ValueError(f"{label} must be an integer") from None
    if not math.isfinite(converted) or converted < 1 or converted != int(converted):
        raise ValueError(f"{label} must be a positive integer")
    return int(converted)


def _entity_list(value: Any, *, exclude: set[str] | None = None) -> list[str]:
    if value in (None, ""):
        return []
    values = (value,) if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)):
        raise ValueError("entity collection must be a list")
    result: list[str] = []
    for raw in values:
        entity = _entity_id(raw, _CONTROL_DOMAINS)
        if entity is None or entity in result or entity in (exclude or set()):
            raise ValueError("logical actuator entities must be valid and unique")
        result.append(entity)
    return result


def _expected_power(raw: Mapping[str, Any]) -> int:
    value = raw.get(CONF_DEVICE_EXPECTED_POWER)
    if value is None or isinstance(value, bool):
        raise ValueError("expected power must be finite")
    try:
        expected = float(value)
    except TypeError, ValueError:
        raise ValueError("expected power must be finite") from None
    if not math.isfinite(expected) or not 1 <= expected <= 50000:
        raise ValueError("expected power is outside the allowed range")
    return int(math.ceil(expected))


def _option_entity_roles(raw: Mapping[str, Any], entity: str, seen_entities: set[str]) -> dict[str, Any]:
    """Normalize coupled command/readback/fallback roles independently of identity."""
    actuators = _entity_list(raw.get(CONF_DEVICE_ACTUATORS), exclude={entity, *seen_entities})
    command = _entity_id(raw.get(CONF_DEVICE_COMMAND_ENTITY), _CONTROL_DOMAINS)
    command = command or next((item for item in actuators if item.startswith("climate.")), entity)
    return {
        CONF_DEVICE_ACTUATORS: actuators,
        CONF_DEVICE_COMMAND_ENTITY: command,
        CONF_DEVICE_READBACK_ENTITIES: _entity_list(
            raw.get(CONF_DEVICE_READBACK_ENTITIES)
            or [entity if command.startswith("climate.") else command]),
        CONF_DEVICE_EMERGENCY_OFF_ENTITIES: _entity_list(
            raw.get(CONF_DEVICE_EMERGENCY_OFF_ENTITIES) or ([entity] if command != entity else [])),
    }


def _option_power_sensor(raw: Mapping[str, Any]) -> str | None:
    value = raw.get(CONF_DEVICE_POWER_SENSOR)
    if value in (None, ""):
        return None
    sensor = _sensor_entity_id(value)
    if sensor is None:
        raise ValueError("power sensor must be a sensor entity")
    return sensor


def _option_battery_min_soc(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Keep an explicit per-load minimum charge; reject an invalid one."""
    value = raw.get(CONF_DEVICE_BATTERY_MIN_SOC)
    if value in (None, ""):
        return {}
    minimum = parse_battery_min_soc(value)
    if minimum is None:
        raise ValueError("battery minimum charge must be above 0 and at most 100")
    return {CONF_DEVICE_BATTERY_MIN_SOC: int(minimum) if minimum.is_integer() else minimum}


def _device_label(value: Any, entity: str) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else entity


def _normalized_device(
    raw: Mapping[str, Any],
    index: int,
    *,
    seen_ids: set[str],
    seen_entities: set[str],
    seen_priorities: set[int],
) -> dict[str, Any]:
    device_id = raw.get(CONF_DEVICE_ID)
    if not isinstance(device_id, str) or not device_id.strip():
        raise ValueError("device IDs must be unique")
    device_id = device_id.strip()
    entity = _entity_id(raw.get(CONF_DEVICE_ENTITY), _CONTROL_DOMAINS)
    if device_id in seen_ids or entity is None or entity in seen_entities:
        raise ValueError("device IDs and control entities must be unique")
    roles = _option_entity_roles(raw, entity, seen_entities)
    priority = _positive_integer(raw.get(CONF_PRIORITY), index + 1, "priority")
    if priority in seen_priorities:
        raise ValueError("priorities must be unique")
    sensor = _option_power_sensor(raw)
    name = _device_label(raw.get(CONF_DEVICE_NAME), entity)
    return {
        CONF_DEVICE_ID: device_id,
        CONF_DEVICE_NAME: name,
        CONF_DEVICE_ENTITY: entity,
        CONF_DEVICE_EXPECTED_POWER: _expected_power(raw),
        CONF_DEVICE_POWER_SENSOR: sensor,
        CONF_PRIORITY: priority,
        CONF_SHED_PRIORITY: _positive_integer(
            raw.get(CONF_SHED_PRIORITY), priority, "shed priority"
        ),
        **roles,
        **_option_battery_min_soc(raw),
    }


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
        device = _normalized_device(
            raw,
            index,
            seen_ids=seen_ids,
            seen_entities=seen_entities,
            seen_priorities=seen_priorities,
        )
        normalized.append(device)
        seen_ids.add(device[CONF_DEVICE_ID])
        seen_entities.update((device[CONF_DEVICE_ENTITY], *device[CONF_DEVICE_ACTUATORS]))
        seen_priorities.add(device[CONF_PRIORITY])
    return normalized
