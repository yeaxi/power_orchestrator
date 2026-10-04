"""Domain-aware commands for one logical managed load."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from homeassistant.core import HomeAssistant

from .power_model import ManagedDevice


def capture_restore_state(hass: HomeAssistant, device: ManagedDevice) -> dict[str, Any]:
    """Capture only the normal command target's bounded restorable state."""
    entity_id = device.command_entity
    state = hass.states.get(entity_id)
    payload: dict[str, Any] = {
        "entity_id": entity_id,
        "domain": entity_id.split(".", 1)[0],
    }
    raw_state = getattr(state, "state", None)
    if isinstance(raw_state, str) and raw_state not in {"unknown", "unavailable", "off"}:
        payload["state"] = raw_state[:40]
    temperature = getattr(state, "attributes", {}).get("temperature") if state else None
    if isinstance(temperature, (int, float)) and not isinstance(temperature, bool):
        value = float(temperature)
        if math.isfinite(value):
            payload["temperature"] = value
    return payload


async def async_issue_off(hass: HomeAssistant, entity_id: str) -> None:
    """Issue one domain-correct OFF command."""
    domain = entity_id.split(".", 1)[0]
    if domain == "climate":
        await hass.services.async_call(
            domain,
            "set_hvac_mode",
            {"entity_id": entity_id, "hvac_mode": "off"},
            blocking=True,
        )
        return
    await hass.services.async_call(domain, "turn_off", {"entity_id": entity_id}, blocking=True)


async def async_issue_emergency_fallback(
    hass: HomeAssistant, entity_ids: Iterable[str], *,
    permitted: Callable[[], bool] | None = None,
) -> bool:
    """Issue fallback OFF once per member, rechecking permission after awaits."""
    for entity_id in dict.fromkeys(entity_ids):
        if permitted is not None and not permitted():
            return False
        await async_issue_off(hass, entity_id)
    return True


async def async_issue_restore(
    hass: HomeAssistant,
    device: ManagedDevice,
    restore_state: Mapping[str, Any],
    *,
    permitted: Callable[[], bool],
) -> bool:
    """Restore only while fresh permission holds before each awaited command.

    False means permission was withdrawn, not a failed actuator readback. An
    already-dispatched service cannot be recalled; the next service is denied.
    """
    entity_id = device.command_entity
    domain = entity_id.split(".", 1)[0]
    if not permitted():
        return False
    if domain != "climate":
        await hass.services.async_call(domain, "turn_on", {"entity_id": entity_id}, blocking=True)
        return True
    temperature = restore_state.get("temperature")
    if isinstance(temperature, (int, float)) and not isinstance(temperature, bool):
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": entity_id, "temperature": float(temperature)},
            blocking=True,
        )
    if not permitted():
        return False
    mode = restore_state.get("state")
    if isinstance(mode, str) and mode not in {"off", "unknown", "unavailable"}:
        await hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": entity_id, "hvac_mode": mode},
            blocking=True,
        )
        return True
    await hass.services.async_call("climate", "turn_on", {"entity_id": entity_id}, blocking=True)
    return True
