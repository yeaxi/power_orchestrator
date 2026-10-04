"""Logical device configuration, with explicit input adapter policies.

The strict options adapter rejects invalid household edits. The persisted adapter
sanitizes legacy records and skips conflicting identities. The wizard adapter
retains its tolerant actuator-list handling. All three share topology defaults,
identity normalization and electrical limits; none supplies activation permission.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

CONTROL_DOMAINS = frozenset({"switch", "light", "input_boolean", "climate", "humidifier"})


def entity_id(value: Any, domains: frozenset[str]) -> str | None:
    """Normalize one supported entity identity."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.count(".") != 1:
        return None
    domain, object_id = value.split(".", 1)
    return value if domain in domains and object_id else None


def safe_number(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        converted = float(value)
    except TypeError, ValueError, OverflowError:
        return default
    return converted if math.isfinite(converted) and minimum <= converted <= maximum else default


def parse_battery_min_soc(value: Any) -> float | None:
    """A per-load minimum charge is explicit and within 0 < value <= 100."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        converted = float(value)
    except ValueError, OverflowError:
        return None
    return converted if math.isfinite(converted) and 0 < converted <= 100 else None


def _entities(value: Any, *, strict: bool, exclude: set[str] | None = None) -> list[str]:
    if value in (None, ""):
        return []
    values = (value,) if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)):
        if strict:
            raise ValueError("entity collection must be a list")
        return []
    result: list[str] = []
    for raw in values:
        member = entity_id(raw, CONTROL_DOMAINS)
        if member is None or member in result or member in (exclude or set()):
            if strict:
                raise ValueError("logical actuator entities must be valid and unique")
            continue
        result.append(member)
    return result


def _roles(
    raw: Mapping[str, Any], entity: str, actuators: list[str], *, strict: bool
) -> dict[str, Any]:
    command = entity_id(raw.get("command_entity"), CONTROL_DOMAINS)
    command = command or next(
        (member for member in actuators if member.startswith("climate.")), entity
    )
    readbacks = _entities(raw.get("readback_entities"), strict=strict)
    emergency = _entities(raw.get("emergency_off_entities"), strict=strict)
    return {
        "actuators": actuators,
        "command_entity": command,
        "readback_entities": readbacks or [entity if command.startswith("climate.") else command],
        "emergency_off_entities": emergency or ([entity] if command != entity else []),
    }


def _positive_integer(value: Any, default: int, label: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    try:
        converted = float(value)
    except TypeError, ValueError, OverflowError:
        raise ValueError(f"{label} must be an integer") from None
    if not math.isfinite(converted) or converted < 1 or converted != int(converted):
        raise ValueError(f"{label} must be a positive integer")
    return int(converted)


def _expected_power(value: Any, *, strict: bool) -> int:
    if not strict:
        return int(math.ceil(safe_number(value, default=1, minimum=1, maximum=50000)))
    if value is None or isinstance(value, bool):
        raise ValueError("expected power must be finite")
    try:
        converted = float(value)
    except TypeError, ValueError, OverflowError:
        raise ValueError("expected power must be finite") from None
    if not math.isfinite(converted) or not 1 <= converted <= 50000:
        raise ValueError("expected power is outside the allowed range")
    return int(math.ceil(converted))


def _record(
    raw: Mapping[str, Any], index: int, seen_entities: set[str], *, strict: bool
) -> dict[str, Any] | None:
    identity = raw.get("device_id")
    entity = entity_id(raw.get("entity"), CONTROL_DOMAINS)
    if not isinstance(identity, str) or not identity.strip() or entity is None:
        if strict:
            raise ValueError("device IDs and control entities must be unique")
        return None
    actuators = _entities(raw.get("actuators"), strict=strict, exclude={entity, *seen_entities})
    if strict:
        priority = _positive_integer(raw.get("priority"), index + 1, "priority")
        shed_priority = _positive_integer(raw.get("shed_priority"), priority, "shed priority")
    else:
        priority = int(
            safe_number(
                raw.get("priority", index + 1), default=index + 1, minimum=1, maximum=100000
            )
        )
        shed_priority = int(
            safe_number(
                raw.get("shed_priority", priority), default=priority, minimum=1, maximum=100000
            )
        )
    sensor = entity_id(raw.get("power_sensor"), frozenset({"sensor"}))
    if strict and raw.get("power_sensor") not in (None, "") and sensor is None:
        raise ValueError("power sensor must be a sensor entity")
    battery = parse_battery_min_soc(raw.get("battery_min_soc"))
    if strict and raw.get("battery_min_soc") not in (None, "") and battery is None:
        raise ValueError("battery minimum charge must be above 0 and at most 100")
    name = raw.get("name")
    return {
        "device_id": identity.strip(),
        "name": name.strip() if isinstance(name, str) and name.strip() else entity,
        "entity": entity,
        "expected_power": _expected_power(raw.get("expected_power"), strict=strict),
        "power_sensor": sensor,
        "priority": priority,
        "shed_priority": shed_priority,
        **_roles(raw, entity, actuators, strict=strict),
        **({"battery_min_soc": battery} if battery is not None else {}),
    }


def normalize_devices(value: Any, *, strict: bool) -> list[dict[str, Any]]:
    """Produce complete configuration records using strict or persisted policy."""
    if not isinstance(value, list):
        if strict:
            raise ValueError("devices must be a list")
        return []
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_entities: set[str] = set()
    seen_priorities: set[int] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            if strict:
                raise ValueError("device mapping must be an object")
            continue
        record = _record(raw, index, seen_entities, strict=strict)
        if record is None:
            continue
        if record["device_id"] in seen_ids or record["entity"] in seen_entities:
            if strict:
                raise ValueError("device IDs and control entities must be unique")
            continue
        if strict and record["priority"] in seen_priorities:
            raise ValueError("priorities must be unique")
        normalized.append(record)
        seen_ids.add(record["device_id"])
        seen_entities.update((record["entity"], *record["actuators"]))
        seen_priorities.add(record["priority"])
    return normalized


def wizard_device(
    raw: Mapping[str, Any], *, device_id: str, name: str, power_sensor: Any
) -> dict[str, Any]:
    """Normalize a wizard submission without exposing its topology recipe."""
    entity = entity_id(raw.get("entity"), CONTROL_DOMAINS)
    if entity is None:
        raise ValueError("control entity must be valid")
    # The wizard historically ignores malformed actuator values; options reject them.
    actuator_input = raw.get("actuators")
    actuators = (
        _entities(actuator_input, strict=False, exclude={entity})
        if isinstance(actuator_input, (list, tuple))
        else []
    )
    return {
        "device_id": device_id.strip(),
        "name": name or entity,
        "entity": entity,
        "expected_power": _expected_power(raw.get("expected_power", 2000), strict=True),
        "power_sensor": entity_id(power_sensor, frozenset({"sensor"})),
        **_roles(raw, entity, actuators, strict=True),
    }


def _legacy_integer(value: Any, minimum: int, default: int | None) -> int | None:
    if value is None and default is None:
        return None
    try:
        return max(minimum, int(float(value)))
    except TypeError, ValueError, OverflowError:
        return default


def _legacy_members(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(member for member in value if isinstance(member, str) and member)


def model_configuration(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Adapt a legacy/current record to model configuration, retaining legacy defaults.

    Full persisted records are normalized before reaching this adapter in setup.
    Direct model construction keeps its permissive historical identity/member rules.
    Runtime fields and diagnostic projections are intentionally excluded.
    """
    for key, label in (("entity", "device_id"), ("device_id", "device_id"), ("name", "name")):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise ValueError(f"device record is missing {label}")
    return {
        "device_id": raw["device_id"],
        "name": raw["name"],
        "entity_id": raw["entity"],
        "expected_power": min(_legacy_integer(raw.get("expected_power", 0), 0, 0) or 0, 50000),
        "power_sensor_id": raw.get("power_sensor")
        if isinstance(raw.get("power_sensor"), str)
        else None,
        "priority": _legacy_integer(raw.get("priority", 1), 1, 1) or 1,
        "shed_priority": _legacy_integer(raw.get("shed_priority"), 1, None),
        "actuator_entity_ids": _legacy_members(raw.get("actuators", ())),
        "command_entity_id": raw.get("command_entity")
        if isinstance(raw.get("command_entity"), str)
        else None,
        "readback_entity_ids": _legacy_members(raw.get("readback_entities", ())),
        "emergency_off_entity_ids": _legacy_members(raw.get("emergency_off_entities", ())),
        "battery_min_soc": parse_battery_min_soc(raw.get("battery_min_soc")),
    }
