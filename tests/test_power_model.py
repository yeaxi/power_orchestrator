"""Tests for power_model.py — no HA dependencies."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components"))

import pytest
from power_orchestrator.power_model import ManagedDevice, PowerModel


def test_managed_device_defaults():
    d = ManagedDevice(device_id="dev1", name="Test", entity_id="switch.test", expected_power=2000)
    assert d.device_id == "dev1"
    assert d.name == "Test"
    assert d.entity_id == "switch.test"
    assert d.expected_power == 2000
    assert d.only_from_solar is False
    assert d.power_sensor_id is None
    assert d.is_on is None  # unknown until HA reports a fresh state
    assert d.measured_power == 0
    assert d.pause_active is False


def test_managed_device_pause_active():
    import time
    d = ManagedDevice(device_id="dev1", name="Test", entity_id="switch.test", expected_power=2000)
    assert d.pause_active is False
    d.pause_until = time.time() + 100  # 100s in future
    assert d.pause_active is True
    d.pause_until = time.time() - 10  # 10s in past
    assert d.pause_active is False


def test_managed_device_to_dict():
    d = ManagedDevice(
        device_id="dev1", name="Test", entity_id="switch.test",
        expected_power=2000, only_from_solar=True, power_sensor_id="sensor.test_power",
        priority=1,
    )
    data = d.to_dict()
    assert data["device_id"] == "dev1"
    assert data["name"] == "Test"
    assert data["entity"] == "switch.test"
    assert data["expected_power"] == 2000
    assert data["only_from_solar"] is True
    assert data["power_sensor"] == "sensor.test_power"
    assert data["priority"] == 1


def test_managed_device_from_dict():
    data = {
        "device_id": "dev2",
        "name": "Boiler",
        "entity": "switch.boiler",
        "expected_power": 3000,
        "only_from_solar": True,
        "power_sensor": "sensor.boiler_power",
        "priority": 2,
    }
    d = ManagedDevice.from_dict(data)
    assert d.device_id == "dev2"
    assert d.name == "Boiler"
    assert d.entity_id == "switch.boiler"
    assert d.expected_power == 3000
    assert d.only_from_solar is True
    assert d.power_sensor_id == "sensor.boiler_power"
    assert d.priority == 2


def test_managed_device_from_dict_normalizes_legacy_values():
    d = ManagedDevice.from_dict(
        {
            "device_id": "dev3",
            "name": "Legacy",
            "entity": "switch.legacy",
            "actuators": {"invalid": True},
            "ownership": "invalid",
            "ownership_until": True,
        }
    )
    assert d.actuator_entity_ids == ()
    assert d.ownership.value == "unknown"
    assert d.ownership_until is None

    string_actuator = ManagedDevice.from_dict(
        {
            "device_id": "dev4",
            "name": "String actuator",
            "entity": "switch.primary",
            "actuators": "switch.secondary",
        }
    )
    assert string_actuator.actuator_entity_ids == ("switch.secondary",)


def test_power_model_add_device():
    m = PowerModel()
    d = ManagedDevice(device_id="dev1", name="Dev1", entity_id="switch.dev1", expected_power=1000)
    m.add_device(d)
    assert m.get_device("dev1") == d
    assert m.get_device("nonexistent") is None


def test_power_model_sorting():
    m = PowerModel()
    m.add_device(ManagedDevice(device_id="high", name="High", entity_id="switch.high", expected_power=1000, priority=1))
    m.add_device(ManagedDevice(device_id="mid", name="Mid", entity_id="switch.mid", expected_power=2000, priority=2))
    m.add_device(ManagedDevice(device_id="low", name="Low", entity_id="switch.low", expected_power=3000, priority=3))

    sorted_asc = m.get_sorted_devices()
    assert sorted_asc[0].device_id == "high"
    assert sorted_asc[1].device_id == "mid"
    assert sorted_asc[2].device_id == "low"

    sorted_desc = m.get_sorted_devices_reversed()
    assert sorted_desc[0].device_id == "low"
    assert sorted_desc[1].device_id == "mid"
    assert sorted_desc[2].device_id == "high"


def test_power_model_on_off_devices():
    m = PowerModel()
    d1 = ManagedDevice(device_id="d1", name="D1", entity_id="switch.d1", expected_power=1000, priority=1)
    d2 = ManagedDevice(device_id="d2", name="D2", entity_id="switch.d2", expected_power=2000, priority=2)
    d1.is_on = True
    d2.is_on = False
    m.add_device(d1)
    m.add_device(d2)

    on_devices = m.get_on_devices()
    assert len(on_devices) == 1
    assert on_devices[0].device_id == "d1"

    off_devices = m.get_off_devices()
    assert len(off_devices) == 1
    assert off_devices[0].device_id == "d2"


def test_power_model_total_power():
    m = PowerModel()
    d1 = ManagedDevice(device_id="d1", name="D1", entity_id="switch.d1", expected_power=1000, priority=1)
    d2 = ManagedDevice(device_id="d2", name="D2", entity_id="switch.d2", expected_power=2000, priority=2)
    d1.is_on = True
    d1.measured_power = 800
    d1.measured_power_valid = True
    d2.is_on = True
    d2.measured_power = 1800
    d2.measured_power_valid = True
    m.add_device(d1)
    m.add_device(d2)

    assert m.total_measured_power == 2600
    assert m.total_expected_power == 3000  # both on, no pause


def test_power_model_expected_excludes_paused():
    import time
    m = PowerModel()
    d1 = ManagedDevice(device_id="d1", name="D1", entity_id="switch.d1", expected_power=1000, priority=1)
    d1.pause_until = time.time() + 100
    d1.is_on = True
    m.add_device(d1)
    assert m.total_expected_power == 0  # paused, excluded