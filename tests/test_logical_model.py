"""Logical-model safety contracts."""
from __future__ import annotations

from power_orchestrator.power_model import ManagedDevice, PowerModel


def test_logical_device_exposes_all_actuators() -> None:
    device = ManagedDevice(
        "heater",
        "Heater",
        "switch.heater",
        actuator_entity_ids=("climate.heater",),
    )
    assert device.control_entity_ids == ("switch.heater", "climate.heater")


def test_shed_order_is_independent_from_legacy_priority() -> None:
    model = PowerModel()
    model.add_device(ManagedDevice("protected", "Protected", "switch.protected", priority=1, shed_priority=20))
    model.add_device(ManagedDevice("first", "First", "switch.first", priority=2, shed_priority=1))
    assert [d.device_id for d in model.get_shed_devices()] == ["first", "protected"]


def test_invalid_measured_power_is_not_aggregated() -> None:
    model = PowerModel()
    model.add_device(ManagedDevice("valid", "Valid", "switch.valid", is_on=True, measured_power=1000, measured_power_valid=True))
    model.add_device(ManagedDevice("bad", "Bad", "switch.bad", is_on=True, measured_power=2000, measured_power_valid=False))
    assert model.total_measured_power == 1000
