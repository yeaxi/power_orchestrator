"""Tests for discovery and Home Assistant translation resources."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from power_orchestrator.config_flow import _discover_inputs

PROJECT_ROOT = Path(__file__).parents[1]
INTEGRATION = PROJECT_ROOT / "custom_components" / "power_orchestrator"
TRANSLATIONS_DIR = INTEGRATION / "translations"


@pytest.mark.asyncio
async def test_energy_discovery_keeps_only_grid_battery_and_load_devices() -> None:
    hass = MagicMock()
    state = MagicMock()
    state.attributes = {"friendly_name": "Parents boiler energy"}
    hass.states.get.return_value = state

    async def mock_get_manager(_hass):
        manager = MagicMock()
        manager.data = {
            "energy_sources": [
                {"type": "grid", "power_config": {"stat_rate": "sensor.grid_power"}},
                {"type": "battery", "stat_soc": "sensor.battery_soc", "stat_power": "sensor.battery_power"},
            ],
            "device_consumption": [
                {"stat_consumption": "sensor.parents_energy", "stat_rate": "sensor.parents_power"}
            ],
        }
        return manager

    with patch("homeassistant.components.energy.async_get_manager", mock_get_manager):
        result = await _discover_inputs(hass)

    assert result == {
        "grid_power": "sensor.grid_power",
        "battery_soc": "sensor.battery_soc",
        "devices": [{
            "entity_id": "sensor.parents_energy",
            "name": "Parents boiler energy",
            "power_sensor": "sensor.parents_power",
        }],
    }
    assert "solar_power" not in result
    assert "solar_forecast" not in result


def test_translation_resources_are_valid_and_have_matching_steps() -> None:
    strings = json.loads((INTEGRATION / "strings.json").read_text())
    english = json.loads((TRANSLATIONS_DIR / "en.json").read_text())
    ukrainian = json.loads((TRANSLATIONS_DIR / "uk.json").read_text())
    assert set(strings["config"]["step"]) == {"user", "load_monitoring", "devices", "priority", "grid_loss", "reconfigure"}
    assert english["config"] == strings["config"]
    assert set(ukrainian["config"]["step"]) == set(strings["config"]["step"])


def test_every_config_field_has_inline_description_in_all_locales() -> None:
    expected = {
        "user": {"grid_power", "battery_soc"},
        "load_monitoring": {"load_sensor", "max_load", "averaging_period", "safety_reserve", "hysteresis", "threshold_count"},
        "devices": {"discovered_devices", "add_custom_device", "entity", "name", "expected_power", "power_sensor", "actuators", "add_another"},
        "priority": {*(f"priority_{index}" for index in range(1, 11)), "pause_period"},
        "grid_loss": {"grid_loss_mode", "grid_loss_sensor", "battery_soc", "battery_threshold"},
        "reconfigure": {"load_sensor", "max_load", "averaging_period", "safety_reserve", "hysteresis", "pause_period", "grid_loss_mode", "grid_loss_sensor", "battery_soc", "battery_threshold", "devices", "threshold_count"},
    }
    for path in ((INTEGRATION / "strings.json"), (TRANSLATIONS_DIR / "en.json"), (TRANSLATIONS_DIR / "uk.json")):
        data = json.loads(path.read_text())
        for step, fields in expected.items():
            descriptions = data["config"]["step"][step].get("data_description", {})
            assert fields <= descriptions.keys(), f"{path}: {step}"
            assert all(isinstance(descriptions[field], str) and descriptions[field].strip() for field in fields)
