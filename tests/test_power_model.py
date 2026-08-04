"""Tests for the load-shedding logical model."""
from __future__ import annotations

import time

import pytest

from power_orchestrator.policy import Ownership
from power_orchestrator.power_model import ManagedDevice, PowerModel


def test_managed_device_defaults_and_serialization() -> None:
    device = ManagedDevice("d1", "Load", "switch.load", expected_power=2000)
    assert device.control_entity_ids == ("switch.load",)
    assert device.is_on is None
    assert device.ownership is Ownership.UNKNOWN
    payload = device.to_dict()
    assert payload["device_id"] == "d1"
    assert "only_from_solar" not in payload
    assert "restore_priority" not in payload


def test_logical_device_deduplicates_actuators_and_ignores_removed_policy_fields() -> None:
    device = ManagedDevice.from_dict(
        {
            "device_id": "d1",
            "name": "Load",
            "entity": "switch.load",
            "expected_power": "2000",
            "power_sensor": "sensor.load_power",
            "actuators": ["climate.load", "switch.load", "climate.load"],
            "ownership": "external",
            "only_from_solar": True,
            "restore_priority": 1,
        }
    )
    assert device.control_entity_ids == ("switch.load", "climate.load")
    assert device.ownership is Ownership.EXTERNAL
    assert device.power_sensor_id == "sensor.load_power"


def test_pause_active_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    now = time.time()
    device = ManagedDevice("d1", "Load", "switch.load", pause_until=now + 60)
    assert device.pause_active
    device.pause_until = now - 1
    assert not device.pause_active


def test_shedding_order_is_explicit_and_deterministic() -> None:
    model = PowerModel()
    model.add_device(ManagedDevice("b", "B", "switch.b", priority=1, shed_priority=20))
    model.add_device(ManagedDevice("a", "A", "switch.a", priority=2, shed_priority=1))
    model.add_device(ManagedDevice("c", "C", "switch.c", priority=3))
    assert [item.device_id for item in model.get_shed_devices()] == ["a", "c", "b"]
    assert [item.device_id for item in model.get_sorted_devices_reversed()] == ["b", "c", "a"]


def test_invalid_measured_power_is_not_aggregated() -> None:
    model = PowerModel()
    valid = ManagedDevice("valid", "Valid", "switch.valid", is_on=True, measured_power=800, measured_power_valid=True)
    invalid = ManagedDevice("invalid", "Invalid", "switch.invalid", is_on=True, measured_power=900, measured_power_valid=False)
    model.add_device(valid)
    model.add_device(invalid)
    assert model.total_measured_power == 800
    assert model.total_expected_power == 0
