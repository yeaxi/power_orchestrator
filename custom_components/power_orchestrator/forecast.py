"""Forecast.Solar estimated-power resolution and runtime validation."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry
from homeassistant.util import dt as dt_util

from .const import FORECAST_MAX_AGE_SECONDS

_LOGGER = logging.getLogger(__name__)

_FORECAST_SOLAR_DOMAIN = "forecast_solar"
_ESTIMATED_POWER_UNIQUE_ID_SUFFIX = "_power_production_now"
_POWER_UNIT_FACTORS = {
    "w": 1.0,
    "kw": 1000.0,
}
_UNKNOWN_STATES = {"unknown", "unavailable", "none", "null", ""}


def _attributes(state: Any) -> Mapping[str, Any]:
    """Return state attributes when they are a mapping."""
    value = getattr(state, "attributes", None)
    return value if isinstance(value, Mapping) else {}


def _same_clock_hour(first: datetime, second: datetime) -> bool:
    """Compare two aware datetimes in the evaluation timezone."""
    first_local = first.astimezone(second.tzinfo)
    return (
        first_local.year,
        first_local.month,
        first_local.day,
        first_local.hour,
    ) == (
        second.year,
        second.month,
        second.day,
        second.hour,
    )


def current_power_forecast_w(
    state: Any,
    *,
    now: datetime | None = None,
    max_age_s: float = FORECAST_MAX_AGE_SECONDS,
) -> float | None:
    """Return fresh Forecast.Solar estimated power normalized to watts.

    This contract is specifically for Forecast.Solar's
    ``power_production_now`` entity.  It accepts W/kW and rejects Wh/kWh,
    because energy sensors do not prove estimated instantaneous/current power.
    Admission fails closed for invalid state, missing/naive timestamps,
    stale/future reports, and reports from another clock hour.
    """
    if state is None:
        return None

    raw_state = getattr(state, "state", None)
    if raw_state is None or str(raw_state).strip().lower() in _UNKNOWN_STATES:
        return None

    unit = _attributes(state).get("unit_of_measurement")
    if not isinstance(unit, str):
        return None
    factor = _POWER_UNIT_FACTORS.get(unit.strip().lower())
    if factor is None:
        return None

    reported = getattr(state, "last_reported", None)
    if not isinstance(reported, datetime) or reported.tzinfo is None:
        return None

    if now is None:
        reference = dt_util.now()
    elif not isinstance(now, datetime) or now.tzinfo is None:
        return None
    else:
        reference = now

    reported_local = reported.astimezone(reference.tzinfo)
    age_s = (reference - reported_local).total_seconds()
    if age_s < 0 or age_s > max_age_s:
        return None
    if not _same_clock_hour(reported_local, reference):
        return None

    try:
        value = float(raw_state) * factor
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def resolve_current_power_forecast_entity(
    hass: Any,
    config_entry_id: str,
) -> str | None:
    """Resolve exact Forecast.Solar ``power_production_now`` by registry identity."""
    if not config_entry_id:
        return None

    expected_unique_id = f"{config_entry_id}{_ESTIMATED_POWER_UNIQUE_ID_SUFFIX}"
    try:
        registry = async_get_entity_registry(hass)
        entities = getattr(registry, "entities", {})
        candidates = []
        for entity in entities.values():
            if getattr(entity, "config_entry_id", None) != config_entry_id:
                continue
            entity_id = getattr(entity, "entity_id", None)
            if not isinstance(entity_id, str) or not entity_id.startswith("sensor."):
                continue
            if getattr(entity, "platform", None) != _FORECAST_SOLAR_DOMAIN:
                continue
            if getattr(entity, "unique_id", None) != expected_unique_id:
                continue
            if getattr(entity, "disabled_by", None) is not None:
                continue
            candidates.append(entity_id)
    except Exception as exc:  # pragma: no cover - HA registry boundary
        _LOGGER.debug("Forecast entity resolution failed: %s", exc)
        return None

    if len(candidates) != 1:
        return None
    result = candidates[0]
    _LOGGER.debug(
        "Resolved Forecast.Solar config entry %s to estimated-power entity %s",
        config_entry_id,
        result,
    )
    return result


# Compatibility aliases for internal callers using the earlier helper name.
resolve_current_hour_forecast_entity = resolve_current_power_forecast_entity
resolve_forecast_entity = resolve_current_power_forecast_entity
