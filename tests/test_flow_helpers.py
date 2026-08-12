"""Unit tests for the shared config/options flow helpers."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from power_orchestrator.flow_helpers import (
    _entity_id,
    _entry_current,
    _friendly,
    _gen_id,
    _normalize_options_devices,
    _sensor_entity_id,
)


def test_gen_id_is_short_hex() -> None:
    value = _gen_id()
    assert len(value) == 8 and all(c in "0123456789abcdef" for c in value)


def test_entity_id_validates_domain() -> None:
    assert _entity_id("switch.a", frozenset({"switch"})) == "switch.a"
    assert _entity_id("light.a", frozenset({"switch"})) is None
    assert _entity_id("bad", frozenset({"switch"})) is None
    assert _entity_id(123, frozenset({"switch"})) is None
    assert _sensor_entity_id("sensor.p") == "sensor.p"
    assert _sensor_entity_id("switch.p") is None


def test_friendly_prefers_friendly_name() -> None:
    hass = SimpleNamespace(
        states=SimpleNamespace(get=lambda e: SimpleNamespace(attributes={"friendly_name": "Boiler"}))
    )
    assert _friendly(hass, "switch.b") == "Boiler"
    empty = SimpleNamespace(states=SimpleNamespace(get=lambda e: None))
    assert _friendly(empty, "switch.b") == "switch.b"
    assert _friendly(empty, "") == ""


def test_entry_current_prefers_options() -> None:
    entry = SimpleNamespace(options={"k": "opt"}, data={"k": "dat", "only": "d"})
    assert _entry_current(entry, "k") == "opt"
    assert _entry_current(entry, "only") == "d"
    assert _entry_current(entry, "missing", "def") == "def"


def test_normalize_options_devices_happy_path() -> None:
    devices = _normalize_options_devices(
        [
            {
                "device_id": "d1",
                "name": "Boiler",
                "entity": "switch.d1",
                "expected_power": 2000,
                "restore_enabled": True,
            }
        ]
    )
    assert devices[0]["device_id"] == "d1"
    assert devices[0]["restore_enabled"] is True
    assert devices[0]["priority"] == 1


def test_normalize_options_devices_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        _normalize_options_devices("not-a-list")
    with pytest.raises(ValueError):
        _normalize_options_devices([{"device_id": "d1", "entity": "sensor.x", "expected_power": 1}])
    with pytest.raises(ValueError):
        _normalize_options_devices(
            [
                {"device_id": "d1", "entity": "switch.a", "expected_power": 1},
                {"device_id": "d1", "entity": "switch.b", "expected_power": 1},
            ]
        )
