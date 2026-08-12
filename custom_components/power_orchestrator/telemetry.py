"""Telemetry reading: aggregate load and grid/battery safety source.

Pure interpretation of Home Assistant telemetry, separate from the coordinator's
runtime state. The aggregate-load read returns a :class:`LoadReading` value
object; the safety source is a small :class:`SafetySource` value object whose
availability/OK checks take ``hass`` explicitly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant

from .const import GRID_LOSS_MODE_SENSOR, GRID_LOSS_MODE_THRESHOLD
from .states import state_is_available, state_reported_timestamp


@dataclass(frozen=True)
class LoadReading:
    """Normalized aggregate-load reading with its validity and failure reason."""

    value: float
    valid: bool
    reason: str
    reported_at: float | None


def read_load_sensor(hass: HomeAssistant, load_sensor: str) -> LoadReading:
    """Read an available, non-negative power value in watts.

    ``kW``/``kilowatt`` readings are normalized to watts. Missing, unavailable,
    wrong-unit, non-finite, or negative readings fail closed with a reason and
    a ``0.0`` value that callers must not treat as a real measurement.
    """
    state = hass.states.get(load_sensor)
    if state is None or not state_is_available(state):
        return LoadReading(0.0, False, "unavailable", None)
    unit = getattr(state, "attributes", {}).get("unit_of_measurement")
    normalized_unit = str(unit).strip().lower()
    try:
        value = float(getattr(state, "state", ""))
    except (TypeError, ValueError):
        value = math.nan
    if normalized_unit in {"kw", "kilowatt", "kilowatts"}:
        value *= 1000
    elif normalized_unit not in {"w", "watt", "watts"}:
        return LoadReading(0.0, False, "unsupported_unit", None)
    if not math.isfinite(value) or value < 0:
        return LoadReading(0.0, False, "invalid_value", None)
    return LoadReading(value, True, "ok", state_reported_timestamp(state))


@dataclass(frozen=True)
class SafetySource:
    """The configured grid-loss/battery safety source and its validity checks.

    Home Assistant's source entity owns semantic availability; this never infers
    unavailability from ``last_updated``. The source must publish
    ``unavailable``/``unknown`` when it cannot vouch for its value.
    """

    mode: str
    grid_sensor: str | None = None
    battery_soc_sensor: str | None = None
    battery_threshold: float | None = None

    @property
    def configured(self) -> bool:
        if self.mode == GRID_LOSS_MODE_SENSOR:
            return bool(self.grid_sensor)
        if self.mode == GRID_LOSS_MODE_THRESHOLD:
            return bool(self.battery_soc_sensor and self.battery_threshold is not None)
        return False

    def _sensor_id(self) -> str | None:
        return self.grid_sensor if self.mode == GRID_LOSS_MODE_SENSOR else self.battery_soc_sensor

    def available(self, hass: HomeAssistant) -> bool:
        """Return whether the configured safety source reports a usable state."""
        if not self.configured:
            return False
        sensor_id = self._sensor_id()
        if not isinstance(sensor_id, str):
            return False
        state = hass.states.get(sensor_id)
        if not state_is_available(state):
            return False
        if self.mode == GRID_LOSS_MODE_SENSOR:
            return getattr(state, "state", None) in {STATE_ON, STATE_OFF}
        unit = getattr(state, "attributes", {}).get("unit_of_measurement")
        if str(unit).strip() not in {"%", "percent"}:
            return False
        try:
            soc = float(getattr(state, "state", ""))
        except (TypeError, ValueError):
            return False
        return math.isfinite(soc) and 0 <= soc <= 100

    def ok(self, hass: HomeAssistant) -> bool:
        """Return true only for a configured, available, and safe source."""
        if not self.configured or not self.available(hass):
            return False
        sensor_id = self._sensor_id()
        if not isinstance(sensor_id, str):
            return False
        state = hass.states.get(sensor_id)
        if self.mode == GRID_LOSS_MODE_SENSOR:
            return getattr(state, "state", None) == STATE_ON
        unit = getattr(state, "attributes", {}).get("unit_of_measurement")
        if str(unit).strip() not in {"%", "percent"}:
            return False
        try:
            soc = float(getattr(state, "state", ""))
        except (TypeError, ValueError):
            return False
        if self.battery_threshold is None:
            return False
        return math.isfinite(soc) and 0 <= soc <= 100 and soc > float(self.battery_threshold)
