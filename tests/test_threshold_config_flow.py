"""Threshold and logical-device configuration tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from power_orchestrator.config_flow import PowerOrchestratorConfigFlow, PowerOrchestratorOptionsFlow
from power_orchestrator.const import (
    CONF_DEVICE_ACTUATORS,
    CONF_DEVICE_ENTITY,
    CONF_DEVICE_EXPECTED_POWER,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_DEVICE_POWER_SENSOR,
    CONF_DEVICES,
    CONF_THRESHOLDS,
    CONF_THRESHOLD_COUNT,
)
from power_orchestrator.policy import PolicyConfig


@pytest.mark.asyncio
async def test_load_monitoring_persists_custom_threshold_pairs() -> None:
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    result = await flow.async_step_load_monitoring(
        {
            "load_sensor": "sensor.whole_house",
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
async def test_load_monitoring_rejects_threshold_above_hard_interlock() -> None:
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    result = await flow.async_step_load_monitoring(
        {
            "load_sensor": "sensor.whole_house",
            CONF_THRESHOLD_COUNT: 1,
            "threshold_1_power": 9001,
            "threshold_1_time": 5,
        }
    )
    assert result["type"] == "form"
    assert result["errors"]["base"] == "invalid_thresholds"


def test_threshold_form_has_no_generation_or_start_fields() -> None:
    flow = PowerOrchestratorConfigFlow()
    schema_names = {
        getattr(key, "schema", key) for key in flow._threshold_form()["data_schema"].schema
    }
    assert {"threshold_power", "threshold_duration", "add_threshold"} <= schema_names
    assert CONF_THRESHOLD_COUNT not in schema_names
    assert "solar_power" not in schema_names
    assert "solar_forecast" not in schema_names
    assert "request_start" not in schema_names


def test_device_form_contains_only_shedding_fields() -> None:
    flow = PowerOrchestratorConfigFlow()
    schema_names = {
        getattr(key, "schema", key) for key in flow._device_config_form({})["data_schema"].schema
    }
    assert CONF_DEVICE_ACTUATORS in schema_names
    assert "hvac_mode_on" not in schema_names
    assert "only_from_solar" not in schema_names


def test_build_device_normalizes_actuators() -> None:
    flow = PowerOrchestratorConfigFlow()
    built = flow._build_device(
        {
            CONF_DEVICE_ID: "kitchen",
            CONF_DEVICE_NAME: "Kitchen",
            CONF_DEVICE_ENTITY: "switch.kitchen",
            CONF_DEVICE_EXPECTED_POWER: 3000,
            CONF_DEVICE_POWER_SENSOR: "sensor.kitchen_power",
            CONF_DEVICE_ACTUATORS: ["climate.kitchen", "climate.kitchen"],
        },
        {"entity_id": "switch.kitchen", "name": "Kitchen"},
    )
    assert built[CONF_DEVICE_ACTUATORS] == ["climate.kitchen"]
    assert "only_from_solar" not in built


@pytest.mark.asyncio
async def test_options_flow_normalizes_devices_without_removed_fields() -> None:
    entry = SimpleNamespace(data={"load_sensor": "sensor.load"}, options={})
    flow = PowerOrchestratorOptionsFlow(entry)
    result = await flow.async_step_init(
        {
            "load_sensor": "sensor.load",
            "grid_loss_mode": "grid_loss_sensor",
            "grid_loss_sensor": "binary_sensor.grid",
            CONF_DEVICES: [
                {
                    CONF_DEVICE_ID: "one",
                    CONF_DEVICE_NAME: "One",
                    CONF_DEVICE_ENTITY: "switch.one",
                    CONF_DEVICE_EXPECTED_POWER: 1000,
                    CONF_DEVICE_POWER_SENSOR: "sensor.one_power",
                    CONF_DEVICE_ACTUATORS: [],
                    "only_from_solar": True,
                    "priority": 1,
                }
            ],
            CONF_THRESHOLD_COUNT: 1,
            "threshold_1_power": 6100,
            "threshold_1_time": 90,
        }
    )
    assert result["type"] == "create_entry"
    normalized = result["data"][CONF_DEVICES][0]
    assert "only_from_solar" not in normalized
    assert PolicyConfig.from_mapping(result["data"]).thresholds
