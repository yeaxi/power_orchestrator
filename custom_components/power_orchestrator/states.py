"""Pure Home Assistant state-reading helpers.

These functions hold no coordinator runtime state. They read Home Assistant
entity states and normalize them into the booleans and timestamps the
coordinator and its collaborators reason about, so that state interpretation
lives in one small, independently testable place.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant

from .const import QUARANTINE_CLEAR_MAX_POWER_W
from .power_model import ManagedDevice


def state_is_available(state: Any) -> bool:
    """Return whether Home Assistant reports a semantically usable state."""
    if state is None:
        return False
    return getattr(state, "state", None) not in {
        None,
        STATE_UNKNOWN,
        STATE_UNAVAILABLE,
    }


def state_reported_timestamp(state: Any) -> float | None:
    """Return the state's ``last_reported`` as a finite epoch, else ``None``."""
    raw = getattr(state, "last_reported", None) if state is not None else None
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            raw = raw.replace(tzinfo=timezone.utc)
        value = raw.timestamp()
        if math.isfinite(value):
            return value
    elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = float(raw)
        if math.isfinite(value):
            return value
    return None


def actuator_state_on(entity_id: str, state: Any) -> bool | None:
    """Interpret one actuator state as on/off/unknown by its domain."""
    raw = getattr(state, "state", None) if state is not None else None
    if raw in {STATE_UNAVAILABLE, STATE_UNKNOWN, None}:
        return None
    domain = entity_id.split(".", 1)[0]
    if domain in {"switch", "light", "input_boolean"}:
        if raw == STATE_ON:
            return True
        if raw == STATE_OFF:
            return False
        return None
    if domain == "climate":
        return (
            False
            if raw == STATE_OFF
            else (None if raw in {STATE_UNKNOWN, STATE_UNAVAILABLE} else True)
        )
    return None


def ordinary_shedding_power_eligible(device: ManagedDevice) -> bool:
    """Require positive measured draw before ordinary shedding.

    A configured power sensor reporting a valid zero (or only the bounded
    near-zero clear threshold) describes an already-heated/idle load. It must
    not be switched off merely because another load caused overload. Emergency
    interlocks use their separate all-stop path.
    """
    if device.power_sensor_id is None:
        return True
    return device.measured_power_valid and device.measured_power > QUARANTINE_CLEAR_MAX_POWER_W


def logical_device_state(hass: HomeAssistant, device: ManagedDevice) -> bool | None:
    """Reduce a logical device's members to one on/off/unknown state."""
    states = [
        actuator_state_on(entity_id, hass.states.get(entity_id))
        for entity_id in device.control_entity_ids
    ]
    if not states or any(value is None for value in states):
        return None
    if all(states):
        return True
    if not any(states):
        return False
    return None


def logical_device_reported_at(hass: HomeAssistant, device: ManagedDevice) -> float | None:
    """Return the newest causal report timestamp across a device's members."""
    timestamps = [
        state_reported_timestamp(hass.states.get(entity_id))
        for entity_id in device.control_entity_ids
    ]
    valid = [timestamp for timestamp in timestamps if timestamp is not None]
    return max(valid) if valid else None


def logical_device_confirmed_off(hass: HomeAssistant, device: ManagedDevice) -> bool:
    """Return whether a logical device is confirmed fully OFF."""
    return logical_device_state(hass, device) is False
