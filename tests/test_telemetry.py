"""Unit tests for telemetry reading and the safety source value object."""
from __future__ import annotations

from types import SimpleNamespace

from power_orchestrator.const import GRID_LOSS_MODE_SENSOR, GRID_LOSS_MODE_THRESHOLD
from power_orchestrator.telemetry import SafetySource, read_load_sensor


class _Hass:
    def __init__(self, mapping):
        self.states = SimpleNamespace(get=lambda entity_id: mapping.get(entity_id))


def _state(value, unit=None, last_reported=None):
    attrs = {"unit_of_measurement": unit} if unit is not None else {}
    return SimpleNamespace(state=value, attributes=attrs, last_reported=last_reported)


def test_read_load_sensor_watts_and_kilowatts() -> None:
    hass = _Hass({"sensor.load": _state("2500", unit="W", last_reported=10.0)})
    r = read_load_sensor(hass, "sensor.load")
    assert r.valid is True and r.value == 2500.0 and r.reason == "ok" and r.reported_at == 10.0
    hass = _Hass({"sensor.load": _state("2.5", unit="kW")})
    r = read_load_sensor(hass, "sensor.load")
    assert r.valid is True and r.value == 2500.0


def test_read_load_sensor_fail_closed_paths() -> None:
    assert read_load_sensor(_Hass({}), "sensor.load").reason == "unavailable"
    assert read_load_sensor(_Hass({"sensor.load": _state("unavailable", unit="W")}), "sensor.load").reason == "unavailable"
    assert read_load_sensor(_Hass({"sensor.load": _state("100", unit="A")}), "sensor.load").reason == "unsupported_unit"
    assert read_load_sensor(_Hass({"sensor.load": _state("-5", unit="W")}), "sensor.load").reason == "invalid_value"
    bad = read_load_sensor(_Hass({"sensor.load": _state("nan", unit="W")}), "sensor.load")
    assert bad.valid is False and bad.value == 0.0


def test_safety_source_sensor_mode() -> None:
    source = SafetySource(mode=GRID_LOSS_MODE_SENSOR, grid_sensor="binary_sensor.grid")
    assert source.configured is True
    on = _Hass({"binary_sensor.grid": _state("on")})
    assert source.available(on) is True and source.ok(on) is True
    off = _Hass({"binary_sensor.grid": _state("off")})
    assert source.available(off) is True and source.ok(off) is False
    gone = _Hass({"binary_sensor.grid": _state("unavailable")})
    assert source.available(gone) is False and source.ok(gone) is False


def test_safety_source_battery_mode_threshold() -> None:
    source = SafetySource(
        mode=GRID_LOSS_MODE_THRESHOLD, battery_soc_sensor="sensor.soc", battery_threshold=20
    )
    assert source.configured is True
    safe = _Hass({"sensor.soc": _state("55", unit="%")})
    assert source.ok(safe) is True
    low = _Hass({"sensor.soc": _state("15", unit="%")})
    assert source.available(low) is True and source.ok(low) is False
    wrong_unit = _Hass({"sensor.soc": _state("55", unit="kWh")})
    assert source.available(wrong_unit) is False


def test_safety_source_unconfigured() -> None:
    assert SafetySource(mode=GRID_LOSS_MODE_SENSOR, grid_sensor=None).configured is False
    assert SafetySource(mode=GRID_LOSS_MODE_THRESHOLD, battery_soc_sensor="sensor.soc").configured is False
