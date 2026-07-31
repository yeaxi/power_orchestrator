"""Additional logical-device model contracts."""
from __future__ import annotations

from power_orchestrator.power_model import ManagedDevice, PowerModel
from power_orchestrator.policy import Ownership


def test_logical_device_exposes_all_actuators_and_ownership():
    device = ManagedDevice(
        device_id="kitchen",
        name="Kitchen heater",
        entity_id="switch.kitchen",
        expected_power=3000,
        actuator_entity_ids=("climate.kitchen",),
        ownership=Ownership.EXTERNAL,
    )

    assert device.control_entity_ids == ("switch.kitchen", "climate.kitchen")
    assert device.ownership is Ownership.EXTERNAL


def test_shed_order_is_independent_from_start_priority():
    model = PowerModel()
    model.add_device(
        ManagedDevice(
            "protected",
            "Protected",
            "switch.protected",
            expected_power=1000,
            priority=1,
            shed_priority=20,
        )
    )
    model.add_device(
        ManagedDevice(
            "first",
            "First",
            "switch.first",
            expected_power=1000,
            priority=2,
            shed_priority=1,
        )
    )

    assert [d.device_id for d in model.get_sorted_devices()] == ["protected", "first"]
    assert [d.device_id for d in model.get_shed_devices()] == ["first", "protected"]


def test_invalid_measured_power_is_not_aggregated():
    model = PowerModel()
    invalid = ManagedDevice(
        "invalid",
        "Invalid telemetry",
        "switch.invalid",
        expected_power=1000,
        is_on=True,
        measured_power=2500,
        measured_power_valid=False,
        measured_power_reason="unsupported_unit",
    )
    valid = ManagedDevice(
        "valid",
        "Valid telemetry",
        "switch.valid",
        expected_power=1000,
        is_on=True,
        measured_power=1200,
        measured_power_valid=True,
        measured_power_reason="ok",
    )
    model.add_device(invalid)
    model.add_device(valid)

    assert model.total_measured_power == 1200
