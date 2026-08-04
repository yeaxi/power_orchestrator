"""Config-flow tests for load-shedding-only onboarding."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from power_orchestrator.config_flow import (
    PowerOrchestratorConfigFlow,
    _discover_inputs,
    _entity_id,
    _friendly,
    _normalize_options_devices,
    _parse_threshold_input,
    _sensor_entity_id,
    _threshold_field,
    _threshold_form_fields,
)
from power_orchestrator.const import (
    CONF_DEVICE_ACTUATORS,
    CONF_DEVICE_ENTITY,
    CONF_DEVICE_EXPECTED_POWER,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_DEVICE_POWER_SENSOR,
    CONF_PRIORITY,
    CONF_SHED_PRIORITY,
    GRID_LOSS_MODE_SENSOR,
)


def test_entity_helpers_and_threshold_parser() -> None:
    hass = MagicMock()
    state = MagicMock()
    state.attributes = {"friendly_name": "Friendly"}
    hass.states.get.return_value = state
    assert _friendly(hass, "sensor.load") == "Friendly"
    assert _friendly(hass, "") == ""
    assert _sensor_entity_id(" sensor.load ") == "sensor.load"
    assert _sensor_entity_id("switch.load") is None
    assert _entity_id("switch.load", frozenset({"switch"})) == "switch.load"
    assert _entity_id("sensor.load", frozenset({"switch"})) is None
    fields = _threshold_form_fields()
    assert len(fields) == 21
    parsed, error = _parse_threshold_input({"threshold_count": 1, _threshold_field(1, "power"): 6500, _threshold_field(1, "time"): 30})
    assert error is None and parsed == [{"power_limit": 6500.0, "duration_s": 30.0}]
    parsed, error = _parse_threshold_input({"threshold_count": 2, _threshold_field(1, "power"): 10, _threshold_field(1, "time"): 1, _threshold_field(2, "power"): 10, _threshold_field(2, "time"): 1})
    assert parsed is None and error == "invalid_thresholds"


def _device(**overrides):
    value = {
        CONF_DEVICE_ID: "d1",
        CONF_DEVICE_NAME: "Load",
        CONF_DEVICE_ENTITY: "switch.load",
        CONF_DEVICE_EXPECTED_POWER: 1000,
        CONF_DEVICE_POWER_SENSOR: "sensor.load_power",
        CONF_DEVICE_ACTUATORS: ["climate.load"],
        CONF_PRIORITY: 1,
        CONF_SHED_PRIORITY: 1,
    }
    value.update(overrides)
    return value


def test_normalize_devices_drops_removed_policy_fields() -> None:
    normalized = _normalize_options_devices([_device(only_from_solar=True, restore_priority=2)])
    assert normalized[0][CONF_DEVICE_ID] == "d1"
    assert "only_from_solar" not in normalized[0]
    assert "restore_priority" not in normalized[0]


def test_normalize_devices_rejects_invalid_identity_and_duplicate_priority() -> None:
    with pytest.raises(ValueError):
        _normalize_options_devices([_device(**{CONF_DEVICE_ENTITY: "sensor.load"})])
    with pytest.raises(ValueError, match="priorities"):
        _normalize_options_devices([_device(), _device(**{CONF_DEVICE_ID: "d2", CONF_DEVICE_ENTITY: "switch.other", CONF_DEVICE_ACTUATORS: [], CONF_SHED_PRIORITY: 2, CONF_PRIORITY: 1})])


@pytest.mark.asyncio
async def test_discovery_returns_grid_battery_and_devices_without_generation_fields() -> None:
    hass = MagicMock()
    async def manager(_hass):
        value = MagicMock()
        value.data = {
            "energy_sources": [
                {"type": "grid", "stat_rate": "sensor.grid"},
                {"type": "battery", "stat_soc": "sensor.soc"},
            ],
            "device_consumption": [{"stat_consumption": "sensor.load_energy", "stat_rate": "sensor.load_power"}],
        }
        return value
    with patch("homeassistant.components.energy.async_get_manager", manager):
        result = await _discover_inputs(hass)
    assert result == {"grid_power": "sensor.grid", "battery_soc": "sensor.soc", "devices": [{"entity_id": "sensor.load_energy", "name": "sensor.load_energy", "power_sensor": "sensor.load_power"}]}
    assert not any("solar" in key or "forecast" in key for key in result)


@pytest.mark.asyncio
async def test_config_flow_user_and_grid_loss_steps() -> None:
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    flow._discovered = {"grid_power": "sensor.load", "battery_soc": "sensor.soc", "devices": []}
    form = await flow.async_step_user()
    assert form["step_id"] == "user"
    next_form = await flow.async_step_user({"grid_power": "sensor.load", "battery_soc": "sensor.soc"})
    assert next_form["step_id"] == "load_monitoring"
    await flow.async_step_load_monitoring({"load_sensor": "sensor.load", "max_load": 5000, "threshold_count": 1, "threshold_1_power": 6500, "threshold_1_time": 30})
    flow._devices = [{CONF_DEVICE_ID: "d1", CONF_DEVICE_NAME: "Load", CONF_DEVICE_ENTITY: "switch.load"}]
    flow._pause_period = 60
    result = await flow.async_step_grid_loss({"grid_loss_mode": GRID_LOSS_MODE_SENSOR, "grid_loss_sensor": "binary_sensor.grid"})
    assert result["type"] == "create_entry"
    assert result["data"]["load_sensor"] == "sensor.load"
    assert result["data"]["devices"][0][CONF_DEVICE_ID] == "d1"


@pytest.mark.asyncio
async def test_config_flow_rejects_missing_grid_safety_source() -> None:
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    flow._discovered = {"grid_power": "sensor.load"}
    flow._devices = []
    result = await flow.async_step_grid_loss({"grid_loss_mode": GRID_LOSS_MODE_SENSOR})
    assert result["errors"] == {"base": "missing_grid_loss_sensor"}


@pytest.mark.asyncio
async def test_options_flow_normalizes_devices_and_has_no_removed_fields() -> None:
    from power_orchestrator.config_flow import PowerOrchestratorOptionsFlow
    entry = MagicMock()
    entry.data = {"load_sensor": "sensor.load", "devices": [_device()]}
    entry.options = {}
    flow = PowerOrchestratorOptionsFlow(entry)
    result = await flow.async_step_init({"load_sensor": "sensor.load", "devices": [_device()], "grid_loss_mode": GRID_LOSS_MODE_SENSOR, "grid_loss_sensor": "binary_sensor.grid", "threshold_count": 1, "threshold_1_power": 6500, "threshold_1_time": 30})
    assert result["type"] == "create_entry"
    assert "only_from_solar" not in result["data"]["devices"][0]
    assert "solar_power" not in result["data"]
