"""Tests for HA 2026.7 discovery and config-flow translation resources."""
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components"))

from power_orchestrator.config_flow import _discover_energy


PROJECT_ROOT = Path(__file__).parents[1]
TRANSLATIONS_DIR = PROJECT_ROOT / "custom_components" / "power_orchestrator" / "translations"


@pytest.mark.asyncio
async def test_energy_device_without_dashboard_name_uses_state_friendly_name():
    """Optional Energy Dashboard names must fall back to HA state friendly_name."""
    hass = MagicMock()
    state = MagicMock()
    state.attributes = {"friendly_name": "Parents boiler power"}
    hass.states.get.return_value = state

    async def mock_get_manager(_hass):
        manager = MagicMock()
        manager.data = {
            "energy_sources": [],
            "device_consumption": [
                {"stat_consumption": "sensor.parents_boiler_energy"}
            ],
        }
        return manager

    with patch("homeassistant.components.energy.async_get_manager", mock_get_manager):
        result = await _discover_energy(hass)

    assert result["devices"] == [
        {
            "entity_id": "sensor.parents_boiler_energy",
            "name": None,
            "power_sensor": None,
        }
    ]



def test_translation_resources_follow_home_assistant_strings_schema():
    """strings.json and locale files must expose config at the root."""
    strings_path = PROJECT_ROOT / "custom_components" / "power_orchestrator" / "strings.json"
    assert strings_path.exists()
    strings = json.loads(strings_path.read_text())
    assert "config" in strings
    assert "en" not in strings

    english = json.loads((TRANSLATIONS_DIR / "en.json").read_text())
    ukrainian = json.loads((TRANSLATIONS_DIR / "uk.json").read_text())
    assert "config" in english
    assert "config" in ukrainian
    assert "en" not in english
    assert "en" not in ukrainian
    assert english["config"] == strings["config"]

    for locale_data in (strings, english, ukrainian):
        config = locale_data["config"]
        assert set(config["step"]) == {
            "user",
            "load_monitoring",
            "devices",
            "priority",
            "grid_loss",
            "reconfigure",
        }
        assert "summary" in config["step"]["user"]["description"]
        assert "sensor_name" in config["step"]["load_monitoring"]["description"]
        assert "count" in config["step"]["devices"]["description"]
        assert "discovered" in config["step"]["devices"]["description"]
        assert "device_list" in config["step"]["priority"]["description"]
        assert "battery_info" in config["step"]["grid_loss"]["description"]


def test_onboarding_has_field_description_for_every_config_step_field():
    """Every onboarding field must explain its behavior below the field in HA UI."""
    expected = {
        "user": {
            "grid_power", "solar_power", "solar_forecast", "battery_soc", "battery_power",
        },
        "load_monitoring": {
            "load_sensor", "max_load", "averaging_period", "safety_reserve", "hysteresis",
        },
        "devices": {
            "discovered_devices", "add_custom_device", "entity", "name", "expected_power",
            "power_sensor", "only_from_solar", "add_another",
        },
        "priority": {*(f"priority_{i}" for i in range(1, 11)), "pause_period"},
        "grid_loss": {
            "grid_loss_mode", "grid_loss_sensor", "battery_soc", "battery_threshold",
        },
        "reconfigure": {
            "load_sensor", "max_load", "averaging_period", "safety_reserve", "hysteresis",
            "pause_period", "grid_loss_mode", "grid_loss_sensor", "battery_soc",
            "battery_threshold", "solar_power", "solar_forecast_entry", "battery_power",
            "devices", "threshold_count",
        },
    }

    locale_paths = {
        "strings": PROJECT_ROOT / "custom_components" / "power_orchestrator" / "strings.json",
        "en": TRANSLATIONS_DIR / "en.json",
        "uk": TRANSLATIONS_DIR / "uk.json",
    }
    for locale_name, locale_path in locale_paths.items():
        locale_data = json.loads(locale_path.read_text())
        steps = locale_data["config"]["step"]
        for step_id, fields in expected.items():
            descriptions = steps[step_id].get("data_description", {})
            assert fields <= descriptions.keys(), f"{locale_name}/{step_id} is missing descriptions"
            assert all(
                isinstance(descriptions[field], str) and descriptions[field].strip()
                for field in fields
            ), f"{locale_name}/{step_id} has an empty field description"

    power_sensor_descriptions = [
        json.loads(path.read_text())["config"]["step"]["devices"]["data_description"]["power_sensor"]
        for path in locale_paths.values()
    ]
    assert any("auto" in text.lower() or "авто" in text.lower() for text in power_sensor_descriptions)
    assert any(
        "override" in text.lower() or "замін" in text.lower() or "інш" in text.lower()
        for text in power_sensor_descriptions
    )
