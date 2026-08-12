"""Unit tests for the pure state-reading helpers."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from power_orchestrator.power_model import ManagedDevice
from power_orchestrator.states import (
    actuator_state_on,
    logical_device_confirmed_off,
    logical_device_state,
    ordinary_shedding_power_eligible,
    state_is_available,
    state_reported_timestamp,
)


class _Hass:
    def __init__(self, mapping):
        self.states = SimpleNamespace(get=lambda entity_id: mapping.get(entity_id))


def _state(value, *, last_reported=None):
    return SimpleNamespace(state=value, attributes={}, last_reported=last_reported)


def test_state_is_available() -> None:
    assert state_is_available(_state("on")) is True
    assert state_is_available(_state("unknown")) is False
    assert state_is_available(_state("unavailable")) is False
    assert state_is_available(None) is False


def test_state_reported_timestamp_handles_datetime_and_numeric() -> None:
    dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert state_reported_timestamp(_state("on", last_reported=dt)) == dt.timestamp()
    # Naive datetime is treated as UTC.
    naive = datetime(2026, 1, 1)
    assert state_reported_timestamp(_state("on", last_reported=naive)) == dt.timestamp()
    assert state_reported_timestamp(_state("on", last_reported=123.0)) == 123.0
    assert state_reported_timestamp(_state("on", last_reported=None)) is None


def test_actuator_state_on_by_domain() -> None:
    assert actuator_state_on("switch.a", _state("on")) is True
    assert actuator_state_on("light.a", _state("off")) is False
    assert actuator_state_on("input_boolean.a", _state("unknown")) is None
    assert actuator_state_on("climate.a", _state("off")) is False
    assert actuator_state_on("climate.a", _state("heat")) is True
    assert actuator_state_on("sensor.a", _state("123")) is None


def test_ordinary_shedding_power_eligible() -> None:
    no_sensor = ManagedDevice("d", "D", "switch.d")
    assert ordinary_shedding_power_eligible(no_sensor) is True
    metered = ManagedDevice("d", "D", "switch.d", power_sensor_id="sensor.p")
    metered.measured_power_valid = True
    metered.measured_power = 50.0
    assert ordinary_shedding_power_eligible(metered) is True
    metered.measured_power = 0.0
    assert ordinary_shedding_power_eligible(metered) is False


def test_logical_device_state_reduces_members() -> None:
    device = ManagedDevice("d", "D", "switch.d", actuator_entity_ids=("light.d",))
    on = _Hass({"switch.d": _state("on"), "light.d": _state("on")})
    assert logical_device_state(on, device) is True
    off = _Hass({"switch.d": _state("off"), "light.d": _state("off")})
    assert logical_device_state(off, device) is False
    assert logical_device_confirmed_off(off, device) is True
    mixed = _Hass({"switch.d": _state("on"), "light.d": _state("off")})
    assert logical_device_state(mixed, device) is None
    unknown = _Hass({"switch.d": _state("on"), "light.d": _state("unavailable")})
    assert logical_device_state(unknown, device) is None
