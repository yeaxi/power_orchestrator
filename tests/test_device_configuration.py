"""Contracts at the shared Logical device configuration seam."""

from __future__ import annotations

import math

import pytest

from power_orchestrator.device_configuration import normalize_devices, wizard_device
from power_orchestrator.power_model import ManagedDevice


def record(**changes: object) -> dict[str, object]:
    return {"device_id": "heater", "entity": "switch.heater", "expected_power": 2000, **changes}


def test_trimmed_identity_conflicts_reject_edits_and_skip_persisted_duplicates() -> None:
    inputs = [record(device_id=" heater "), record(entity="switch.other")]
    with pytest.raises(ValueError, match="unique"):
        normalize_devices(inputs, strict=True)
    assert [item["device_id"] for item in normalize_devices(inputs, strict=False)] == ["heater"]


def test_shared_topology_defaults_reach_wizard_options_persisted_and_model() -> None:
    raw = record(actuators=["climate.heater"], name="Heater")
    options = normalize_devices([raw], strict=True)[0]
    persisted = normalize_devices([raw], strict=False)[0]
    wizard = wizard_device(raw, device_id="heater", name="Heater", power_sensor=None)
    for item in (options, persisted, wizard):
        assert item["command_entity"] == "climate.heater"
        assert item["readback_entities"] == ["switch.heater"]
        assert item["emergency_off_entities"] == ["switch.heater"]
        model = ManagedDevice.from_dict(item)
        assert model.command_entity == "climate.heater"
        assert model.readback_entities == ("switch.heater",)
        assert model.emergency_off_entities == ("switch.heater",)


def test_explicit_topology_roles_survive_configuration_and_model() -> None:
    raw = record(
        command_entity="humidifier.heater",
        readback_entities=["switch.readback"],
        emergency_off_entities=["switch.fallback"],
        battery_min_soc="25.5",
    )
    for strict in (True, False):
        configured = normalize_devices([raw], strict=strict)[0]
        configured["name"] = "Heater"
        model = ManagedDevice.from_dict(configured)
        assert model.command_entity == "humidifier.heater"
        assert model.readback_entities == ("switch.readback",)
        assert model.emergency_off_entities == ("switch.fallback",)
        assert model.battery_min_soc == 25.5


def test_overlapping_actuators_are_rejected_for_edits_and_pruned_on_load() -> None:
    inputs = [
        record(actuators=["light.shared"]),
        record(device_id="second", entity="switch.second", actuators=["light.shared"]),
    ]
    with pytest.raises(ValueError, match="unique"):
        normalize_devices(inputs, strict=True)
    loaded = normalize_devices(inputs, strict=False)
    assert loaded[0]["actuators"] == ["light.shared"]
    assert loaded[1]["actuators"] == []


@pytest.mark.parametrize("invalid", [True, math.nan, math.inf, -1, 50001, "broken"])
def test_numeric_edits_reject_invalid_power_but_persisted_records_fail_to_safe_default(
    invalid: object,
) -> None:
    with pytest.raises(ValueError):
        normalize_devices([record(expected_power=invalid)], strict=True)
    loaded = normalize_devices([record(expected_power=invalid)], strict=False)[0]
    assert loaded["expected_power"] == 1
    with pytest.raises(ValueError):
        wizard_device(
            record(expected_power=invalid), device_id="heater", name="Heater", power_sensor=None
        )


def test_fractional_power_rounds_up_consistently() -> None:
    raw = record(expected_power=2000.1)
    assert normalize_devices([raw], strict=True)[0]["expected_power"] == 2001
    assert normalize_devices([raw], strict=False)[0]["expected_power"] == 2001
    assert (
        wizard_device(raw, device_id="heater", name="Heater", power_sensor=None)["expected_power"]
        == 2001
    )


@pytest.mark.parametrize("invalid", [True, 0, -1, 101, math.nan, math.inf, "invalid"])
def test_invalid_battery_permission_rejects_edits_and_is_not_granted_on_load(
    invalid: object,
) -> None:
    with pytest.raises(ValueError, match="battery"):
        normalize_devices([record(battery_min_soc=invalid)], strict=True)
    configured = normalize_devices([record(battery_min_soc=invalid)], strict=False)[0]
    assert "battery_min_soc" not in configured
    assert ManagedDevice.from_dict(configured).battery_min_soc is None


def test_wizard_preserves_legacy_tolerant_actuators_but_rejects_bad_explicit_readback() -> None:
    raw = record(actuators=["switch.heater", "bad", "climate.heater", "climate.heater"])
    wizard = wizard_device(raw, device_id="heater", name="Heater", power_sensor=None)
    assert wizard["actuators"] == ["climate.heater"]
    with pytest.raises(ValueError, match="unique"):
        wizard_device(
            record(readback_entities=["bad"]), device_id="heater", name="Heater", power_sensor=None
        )


def test_legacy_model_keeps_permissive_defaults_and_ignores_removed_activation_fields() -> None:
    model = ManagedDevice.from_dict(
        record(
            name="Heater",
            expected_power=math.inf,
            actuators="climate.heater",
            auto_restore=True,
            supply_policy="external_or_battery",
            ownership="planner",
        )
    )
    assert model.expected_power == 0
    assert model.priority == 1
    assert model.shed_priority is None
    assert model.command_entity == "climate.heater"
    assert not hasattr(model, "auto_restore")
    configured = normalize_devices(
        [record(auto_restore=True, supply_policy="external_or_battery")], strict=False
    )[0]
    assert "auto_restore" not in configured and "supply_policy" not in configured


@pytest.mark.parametrize("value", [None, "invalid", {}, [None]])
def test_persisted_bad_envelopes_skip_but_strict_edits_reject(value: object) -> None:
    assert normalize_devices(value, strict=False) == []
    with pytest.raises(ValueError):
        normalize_devices(value, strict=True)
