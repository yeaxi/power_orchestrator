"""User-configurable threshold flow contract tests."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from power_orchestrator.config_flow import (
    PowerOrchestratorConfigFlow,
    PowerOrchestratorOptionsFlow,
)
from power_orchestrator.policy import PolicyConfig, PolicyEngine
from power_orchestrator.const import (
    CONF_DEVICE_ACTUATORS,
    CONF_DEVICE_ENTITY,
    CONF_DEVICE_EXPECTED_POWER,
    CONF_DEVICE_HVAC_MODE_ON,
    CONF_DEVICE_POWER_SENSOR,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_DEVICES,
    CONF_RESTORE_PRIORITY,
    CONF_SHED_PRIORITY,
    CONF_THRESHOLDS,
    CONF_THRESHOLD_COUNT,
)


@pytest.mark.asyncio
async def test_load_monitoring_persists_custom_threshold_pairs():
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()

    result = await flow.async_step_load_monitoring(
        user_input={
            "load_sensor": "sensor.whole_house",
            "max_load": 9000,
            "averaging_period": 10,
            "safety_reserve": 500,
            "hysteresis": 200,
            CONF_THRESHOLD_COUNT: 2,
            "threshold_1_power": 6200,
            "threshold_1_time": 120,
            "threshold_2_power": 7600,
            "threshold_2_time": 15,
        }
    )

    assert result["step_id"] == "devices"
    assert flow._discovered[CONF_THRESHOLDS] == [
        {"power_limit": 6200.0, "duration_s": 120.0},
        {"power_limit": 7600.0, "duration_s": 15.0},
    ]


@pytest.mark.asyncio
async def test_load_monitoring_rejects_threshold_above_hard_interlock():
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()

    result = await flow.async_step_load_monitoring(
        user_input={
            "load_sensor": "sensor.whole_house",
            CONF_THRESHOLD_COUNT: 1,
            "threshold_1_power": 9001,
            "threshold_1_time": 5,
        }
    )

    assert result["type"] == "form"
    assert result["errors"]["base"] == "invalid_thresholds"


@pytest.mark.asyncio
async def test_load_monitoring_rejects_threshold_count_above_ten():
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()

    result = await flow.async_step_load_monitoring(
        user_input={
            "load_sensor": "sensor.whole_house",
            CONF_THRESHOLD_COUNT: 11,
        }
    )

    assert result["type"] == "form"
    assert result["errors"]["base"] == "invalid_thresholds"


def test_threshold_form_exposes_ten_power_and_time_pairs():
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()

    result = flow._load_monitoring_form()
    schema = result["data_schema"].schema

    schema_names = {
        getattr(key, "schema", key)
        for key in schema
    }
    assert CONF_THRESHOLD_COUNT in schema_names
    for index in range(1, 11):
        assert f"threshold_{index}_power" in schema_names
        assert f"threshold_{index}_time" in schema_names


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 3, 10])
async def test_flow_policy_engine_end_to_end_selects_highest_custom_tier(count):
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    user_input = {
        "load_sensor": "sensor.whole_house",
        CONF_THRESHOLD_COUNT: count,
    }
    for index in range(1, count + 1):
        user_input[f"threshold_{index}_power"] = 5000 + index * 300
        user_input[f"threshold_{index}_time"] = 1

    result = await flow.async_step_load_monitoring(user_input)

    assert result["step_id"] == "devices"
    policy = PolicyConfig.from_mapping(flow._discovered)
    engine = PolicyEngine(policy)
    assert len(policy.thresholds) == count

    engine.observe_load(policy.thresholds[-1].limit_w + 1, now=0.0)
    decision = engine.observe_load(policy.thresholds[-1].limit_w + 1, now=2.0)

    assert decision.triggered is True
    assert decision.active_tier == f"custom_{count}"


def test_device_form_exposes_logical_actuator_fields():
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    result = flow._device_config_form(
        {
            "entity_id": "switch.kitchen",
            "name": "Kitchen heater",
            "power_sensor": "sensor.kitchen_power",
        }
    )
    schema_names = {getattr(key, "schema", key) for key in result["data_schema"].schema}
    assert CONF_DEVICE_ACTUATORS in schema_names
    assert CONF_DEVICE_HVAC_MODE_ON in schema_names

    built = flow._build_device(
        {
            CONF_DEVICE_ENTITY: "switch.kitchen",
            CONF_DEVICE_NAME: "Kitchen heater",
            CONF_DEVICE_EXPECTED_POWER: 3000,
            CONF_DEVICE_POWER_SENSOR: "sensor.kitchen_power",
            CONF_DEVICE_ACTUATORS: ["climate.kitchen"],
            CONF_DEVICE_HVAC_MODE_ON: "heat",
        },
        {"entity_id": "switch.kitchen", "name": "Kitchen heater"},
    )
    assert built[CONF_DEVICE_ACTUATORS] == ["climate.kitchen"]
    assert built[CONF_DEVICE_HVAC_MODE_ON] == "heat"


@pytest.mark.asyncio
async def test_options_flow_preserves_logical_device_fields():
    entry = SimpleNamespace(data={"load_sensor": "sensor.load"}, options={})
    flow = PowerOrchestratorOptionsFlow(entry)
    devices = [
        {
            CONF_DEVICE_ID: "kitchen",
            CONF_DEVICE_NAME: "Kitchen heater",
            CONF_DEVICE_ENTITY: "switch.kitchen",
            CONF_DEVICE_EXPECTED_POWER: 3000,
            CONF_DEVICE_ACTUATORS: ["climate.kitchen"],
            CONF_DEVICE_HVAC_MODE_ON: "heat",
            CONF_SHED_PRIORITY: 7,
            CONF_RESTORE_PRIORITY: 2,
            "priority": 3,
        }
    ]

    result = await flow.async_step_init(
        {
            "load_sensor": "sensor.load",
            "grid_loss_mode": "grid_loss_sensor",
            "grid_loss_sensor": "binary_sensor.grid",
            CONF_DEVICES: devices,
            CONF_THRESHOLD_COUNT: 1,
            "threshold_1_power": 6100,
            "threshold_1_time": 90,
        }
    )

    assert result["type"] == "create_entry"
    normalized = result["data"][CONF_DEVICES][0]
    assert normalized[CONF_DEVICE_ACTUATORS] == ["climate.kitchen"]
    assert normalized[CONF_DEVICE_HVAC_MODE_ON] == "heat"
    assert normalized[CONF_SHED_PRIORITY] == 7
    assert normalized[CONF_RESTORE_PRIORITY] == 2


@pytest.mark.asyncio
async def test_options_flow_rejects_duplicate_additional_actuator():
    entry = SimpleNamespace(data={"load_sensor": "sensor.load"}, options={})
    flow = PowerOrchestratorOptionsFlow(entry)
    devices = [
        {
            CONF_DEVICE_ID: "one",
            CONF_DEVICE_ENTITY: "switch.one",
            CONF_DEVICE_EXPECTED_POWER: 1000,
            CONF_DEVICE_ACTUATORS: ["climate.shared"],
            "priority": 1,
        },
        {
            CONF_DEVICE_ID: "two",
            CONF_DEVICE_ENTITY: "switch.two",
            CONF_DEVICE_EXPECTED_POWER: 1000,
            CONF_DEVICE_ACTUATORS: ["climate.shared"],
            "priority": 2,
        },
    ]

    result = await flow.async_step_init(
        {
            "load_sensor": "sensor.load",
            "grid_loss_mode": "grid_loss_sensor",
            "grid_loss_sensor": "binary_sensor.grid",
            CONF_DEVICES: devices,
            CONF_THRESHOLD_COUNT: 1,
            "threshold_1_power": 6100,
            "threshold_1_time": 90,
        }
    )

    assert result["type"] == "form"
    assert result["errors"]["base"] == "invalid_devices"


@pytest.mark.asyncio
async def test_options_flow_persists_custom_threshold_pairs():
    entry = SimpleNamespace(data={"load_sensor": "sensor.load"}, options={})
    flow = PowerOrchestratorOptionsFlow(entry)

    result = await flow.async_step_init(
        {
            "load_sensor": "sensor.load",
            "grid_loss_mode": "grid_loss_sensor",
            "grid_loss_sensor": "binary_sensor.grid",
            CONF_THRESHOLD_COUNT: 2,
            "threshold_1_power": 6100,
            "threshold_1_time": 90,
            "threshold_2_power": 7500,
            "threshold_2_time": 20,
        }
    )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_THRESHOLDS] == [
        {"power_limit": 6100.0, "duration_s": 90.0},
        {"power_limit": 7500.0, "duration_s": 20.0},
    ]
