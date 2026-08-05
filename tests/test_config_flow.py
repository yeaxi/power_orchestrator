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
)
from power_orchestrator.const import (
    CONF_ADD_THRESHOLD,
    CONF_DEVICE_ACTUATORS,
    CONF_DEVICE_ENTITY,
    CONF_DEVICE_EXPECTED_POWER,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_DEVICE_POWER_SENSOR,
    CONF_PRIORITY,
    CONF_PRIORITY_ORDER,
    CONF_SHED_PRIORITY,
    CONF_THRESHOLDS,
    GRID_LOSS_MODE_SENSOR,
    GRID_LOSS_MODE_THRESHOLD,
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
    parsed, error = _parse_threshold_input(
        {"threshold_count": 1, _threshold_field(1, "power"): 6500, _threshold_field(1, "time"): 30}
    )
    assert error is None and parsed == [{"power_limit": 6500.0, "duration_s": 30.0}]
    parsed, error = _parse_threshold_input(
        {
            "threshold_count": 2,
            _threshold_field(1, "power"): 10,
            _threshold_field(1, "time"): 1,
            _threshold_field(2, "power"): 10,
            _threshold_field(2, "time"): 1,
        }
    )
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
        _normalize_options_devices(
            [
                _device(),
                _device(
                    **{
                        CONF_DEVICE_ID: "d2",
                        CONF_DEVICE_ENTITY: "switch.other",
                        CONF_DEVICE_ACTUATORS: [],
                        CONF_SHED_PRIORITY: 2,
                        CONF_PRIORITY: 1,
                    }
                ),
            ]
        )


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
            "device_consumption": [
                {"stat_consumption": "sensor.load_energy", "stat_rate": "sensor.load_power"}
            ],
        }
        return value

    with patch("homeassistant.components.energy.async_get_manager", manager):
        result = await _discover_inputs(hass)
    assert result == {
        "grid_power": "sensor.grid",
        "battery_soc": "sensor.soc",
        "devices": [
            {
                "entity_id": "sensor.load_energy",
                "name": "sensor.load_energy",
                "power_sensor": "sensor.load_power",
            }
        ],
    }
    assert not any("solar" in key or "forecast" in key for key in result)


@pytest.mark.asyncio
async def test_config_flow_user_and_grid_loss_steps() -> None:
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    flow._discovered = {"grid_power": "sensor.load", "battery_soc": "sensor.soc", "devices": []}
    form = await flow.async_step_user()
    assert form["step_id"] == "user"
    next_form = await flow.async_step_user(
        {"grid_power": "sensor.load", "battery_soc": "sensor.soc"}
    )
    assert next_form["step_id"] == "load_monitoring"
    await flow.async_step_load_monitoring(
        {
            "load_sensor": "sensor.load",
            "max_load": 5000,
            "threshold_count": 1,
            "threshold_1_power": 6500,
            "threshold_1_time": 30,
        }
    )
    flow._devices = [
        {CONF_DEVICE_ID: "d1", CONF_DEVICE_NAME: "Load", CONF_DEVICE_ENTITY: "switch.load"}
    ]
    flow._pause_period = 60
    result = await flow.async_step_grid_loss(
        {"grid_loss_mode": GRID_LOSS_MODE_SENSOR, "grid_loss_sensor": "binary_sensor.grid"}
    )
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
    assert result["step_id"] == "grid_loss_source"
    result = await flow.async_step_grid_loss_source({})
    assert result["errors"] == {"base": "missing_grid_loss_sensor"}


@pytest.mark.asyncio
async def test_initial_thresholds_are_collected_as_repeatable_steps() -> None:
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    flow._discovered = {"grid_power": "sensor.load", "battery_soc": None, "devices": []}

    result = await flow.async_step_load_monitoring(
        {
            "load_sensor": "sensor.load",
            "max_load": 9000,
            "averaging_period": 10,
            "safety_reserve": 0,
            "hysteresis": 200,
        }
    )
    assert result["step_id"] == "thresholds"

    result = await flow.async_step_thresholds(
        {"threshold_power": 6500, "threshold_duration": 300, CONF_ADD_THRESHOLD: True}
    )
    assert result["step_id"] == "thresholds"

    result = await flow.async_step_thresholds(
        {"threshold_power": 8000, "threshold_duration": 5, CONF_ADD_THRESHOLD: False}
    )
    assert result["step_id"] == "devices"
    assert flow._discovered["thresholds"] == [
        {"power_limit": 6500.0, "duration_s": 300.0},
        {"power_limit": 8000.0, "duration_s": 5.0},
    ]


@pytest.mark.asyncio
async def test_accepting_threshold_defaults_preserves_the_complete_seed() -> None:
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    result = await flow.async_step_load_monitoring(
        {
            "load_sensor": "sensor.load",
            "max_load": 9000,
            "averaging_period": 10,
            "safety_reserve": 0,
            "hysteresis": 200,
        }
    )
    add_marker = next(
        key
        for key in result["data_schema"].schema
        if getattr(key, "schema", None) == CONF_ADD_THRESHOLD
    )
    default = add_marker.default() if callable(add_marker.default) else add_marker.default
    assert default is True

    result = await flow.async_step_thresholds(
        {"threshold_power": 6500, "threshold_duration": 300, CONF_ADD_THRESHOLD: True}
    )
    assert result["step_id"] == "thresholds"
    result = await flow.async_step_thresholds(
        {"threshold_power": 7000, "threshold_duration": 30, CONF_ADD_THRESHOLD: True}
    )
    assert result["step_id"] == "thresholds"
    result = await flow.async_step_thresholds(
        {"threshold_power": 8000, "threshold_duration": 5, CONF_ADD_THRESHOLD: False}
    )
    assert result["step_id"] == "devices"
    assert flow._discovered["thresholds"] == [
        {"power_limit": 6500.0, "duration_s": 300.0},
        {"power_limit": 7000.0, "duration_s": 30.0},
        {"power_limit": 8000.0, "duration_s": 5.0},
    ]


def test_priority_form_uses_native_reorderable_entity_selector() -> None:
    flow = PowerOrchestratorConfigFlow()
    flow._devices = [
        _device(),
        _device(device_id="d2", entity="switch.other", name="Other", priority=2, shed_priority=2),
    ]

    form = flow._priority_form()
    schema_keys = list(form["data_schema"].schema)
    assert CONF_PRIORITY_ORDER in schema_keys
    selector_config = form["data_schema"].schema[CONF_PRIORITY_ORDER].config
    assert selector_config.multiple is True
    assert selector_config.reorder is True
    assert selector_config.include_entities == ["switch.load", "switch.other"]


@pytest.mark.asyncio
async def test_priority_order_maps_ui_entities_to_runtime_shed_order() -> None:
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    flow._devices = [
        _device(),
        _device(
            **{
                CONF_DEVICE_ID: "d2",
                CONF_DEVICE_ENTITY: "switch.other",
                CONF_DEVICE_NAME: "Other",
                CONF_PRIORITY: 2,
                CONF_SHED_PRIORITY: 2,
            }
        ),
    ]

    result = await flow.async_step_priority({CONF_PRIORITY_ORDER: ["switch.other", "switch.load"]})

    assert result["step_id"] == "grid_loss"
    assert [device[CONF_DEVICE_ENTITY] for device in flow._devices] == [
        "switch.other",
        "switch.load",
    ]
    assert [device[CONF_SHED_PRIORITY] for device in flow._devices] == [1, 2]


def test_config_flow_description_placeholders_are_supplied() -> None:
    flow = PowerOrchestratorConfigFlow()
    flow._discovered = {"devices": []}
    devices_form = flow._device_selection_form()
    assert set(devices_form["description_placeholders"]) == {"count", "discovered"}

    flow._discovered = {"devices": [], "battery_soc": "sensor.soc"}
    grid_form = flow._grid_loss_form()
    assert "battery_info" in grid_form["description_placeholders"]


@pytest.mark.asyncio
async def test_grid_loss_sensor_mode_does_not_render_battery_fields() -> None:
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    mode_form = await flow.async_step_grid_loss()
    assert mode_form["step_id"] == "grid_loss"
    mode_keys = list(mode_form["data_schema"].schema)
    assert "grid_loss_mode" in mode_keys
    assert "battery_soc" not in mode_keys

    source_form = await flow.async_step_grid_loss({"grid_loss_mode": GRID_LOSS_MODE_SENSOR})
    assert source_form["step_id"] == "grid_loss_source"
    source_keys = list(source_form["data_schema"].schema)
    assert "grid_loss_sensor" in source_keys
    assert "battery_soc" not in source_keys


@pytest.mark.asyncio
async def test_options_flow_normalizes_devices_and_has_no_removed_fields() -> None:
    from power_orchestrator.config_flow import PowerOrchestratorOptionsFlow

    entry = MagicMock()
    entry.data = {"load_sensor": "sensor.load", "devices": [_device()]}
    entry.options = {}
    flow = PowerOrchestratorOptionsFlow(entry)
    result = await flow.async_step_init(
        {
            "load_sensor": "sensor.load",
            "devices": [_device()],
            "grid_loss_mode": GRID_LOSS_MODE_SENSOR,
            "grid_loss_sensor": "binary_sensor.grid",
            "threshold_count": 1,
            "threshold_1_power": 6500,
            "threshold_1_time": 30,
        }
    )
    assert result["type"] == "create_entry"
    assert "only_from_solar" not in result["data"]["devices"][0]
    assert "solar_power" not in result["data"]


@pytest.mark.asyncio
async def test_options_dynamic_thresholds_and_sensor_mode_drop_irrelevant_soc() -> None:
    from power_orchestrator.config_flow import PowerOrchestratorOptionsFlow

    entry = MagicMock()
    entry.data = {
        "load_sensor": "sensor.load",
        "devices": [_device()],
        "grid_loss_mode": GRID_LOSS_MODE_SENSOR,
        "grid_loss_sensor": "binary_sensor.grid",
        CONF_THRESHOLDS: [
            {"power_limit": 6500, "duration_s": 300},
            {"power_limit": 7000, "duration_s": 30},
            {"power_limit": 8000, "duration_s": 5},
        ],
    }
    entry.options = {}
    flow = PowerOrchestratorOptionsFlow(entry)
    form = await flow.async_step_init(
        {
            "load_sensor": "sensor.load",
            "devices": [_device()],
            "grid_loss_mode": GRID_LOSS_MODE_SENSOR,
            "grid_loss_sensor": "binary_sensor.grid",
            CONF_PRIORITY_ORDER: ["switch.load"],
        }
    )
    assert form["step_id"] == "thresholds"
    result = await flow.async_step_thresholds(
        {"threshold_power": 6500, "threshold_duration": 300, CONF_ADD_THRESHOLD: True}
    )
    assert result["step_id"] == "thresholds"
    result = await flow.async_step_thresholds(
        {"threshold_power": 7000, "threshold_duration": 30, CONF_ADD_THRESHOLD: True}
    )
    assert result["step_id"] == "thresholds"
    result = await flow.async_step_thresholds(
        {"threshold_power": 8000, "threshold_duration": 5, CONF_ADD_THRESHOLD: False}
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_THRESHOLDS] == [
        {"power_limit": 6500.0, "duration_s": 300.0},
        {"power_limit": 7000.0, "duration_s": 30.0},
        {"power_limit": 8000.0, "duration_s": 5.0},
    ]
    assert result["data"]["grid_loss_sensor"] == "binary_sensor.grid"
    assert "battery_soc" not in result["data"]
    assert "battery_threshold" not in result["data"]


@pytest.mark.asyncio
async def test_options_battery_threshold_mode_requires_soc() -> None:
    from power_orchestrator.config_flow import PowerOrchestratorOptionsFlow

    entry = MagicMock()
    entry.data = {"load_sensor": "sensor.load", "devices": [_device()]}
    entry.options = {}
    flow = PowerOrchestratorOptionsFlow(entry)
    result = await flow.async_step_init(
        {
            "load_sensor": "sensor.load",
            "devices": [_device()],
            "grid_loss_mode": GRID_LOSS_MODE_THRESHOLD,
            "battery_threshold": 20,
        }
    )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "missing_battery_soc_sensor"}
