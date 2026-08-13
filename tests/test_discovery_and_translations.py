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
                {
                    "type": "battery",
                    "stat_soc": "sensor.battery_soc",
                    "stat_power": "sensor.battery_power",
                },
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
        "devices": [
            {
                "entity_id": "sensor.parents_energy",
                "name": "Parents boiler energy",
                "power_sensor": "sensor.parents_power",
            }
        ],
    }
    assert "solar_power" not in result
    assert "solar_forecast" not in result


def test_translation_resources_are_valid_and_have_matching_steps() -> None:
    strings = json.loads((INTEGRATION / "strings.json").read_text())
    english = json.loads((TRANSLATIONS_DIR / "en.json").read_text())
    ukrainian = json.loads((TRANSLATIONS_DIR / "uk.json").read_text())
    assert set(strings["config"]["step"]) == {
        "user",
        "load_monitoring",
        "thresholds",
        "devices",
        "priority",
        "grid_loss",
        "grid_loss_source",
        "reconfigure",
    }
    assert english["config"] == strings["config"]
    assert set(ukrainian["config"]["step"]) == set(strings["config"]["step"])


def test_every_config_field_has_inline_description_in_all_locales() -> None:
    expected = {
        "user": {"grid_power", "battery_soc"},
        "load_monitoring": {
            "load_sensor",
            "max_load",
            "averaging_period",
            "safety_reserve",
            "hysteresis",
        },
        "thresholds": {"threshold_power", "threshold_duration", "add_threshold"},
        "devices": {
            "discovered_devices",
            "add_custom_device",
            "entity",
            "name",
            "expected_power",
            "power_sensor",
            "actuators",
            "add_another",
        },
        "priority": {"priority_order", "pause_period"},
        "grid_loss": {"grid_loss_mode"},
        "grid_loss_source": {"grid_loss_sensor", "battery_soc", "battery_threshold"},
        "reconfigure": {
            "load_sensor",
            "max_load",
            "averaging_period",
            "safety_reserve",
            "hysteresis",
            "pause_period",
            "grid_loss_mode",
            "grid_loss_sensor",
            "battery_soc",
            "battery_threshold",
            "devices",
        },
    }
    for path in (
        (INTEGRATION / "strings.json"),
        (TRANSLATIONS_DIR / "en.json"),
        (TRANSLATIONS_DIR / "uk.json"),
    ):
        data = json.loads(path.read_text())
        for step, fields in expected.items():
            descriptions = data["config"]["step"][step].get("data_description", {})
            assert fields <= descriptions.keys(), f"{path}: {step}"
            assert all(
                isinstance(descriptions[field], str) and descriptions[field].strip()
                for field in fields
            )


# Home Assistant reads flow text from <section>.step.<step_id> and validates the
# surrounding shape. Text parked anywhere else never reaches the user and fails
# hassfest, which is how the options threshold step lost its translations once.
FLOW_SECTION_KEYS = frozenset(
    {"abort", "create_entry", "error", "flow_title", "progress", "step"}
)


@pytest.mark.parametrize("resource", ["strings.json", "translations/en.json", "translations/uk.json"])
def test_flow_translations_only_use_keys_home_assistant_reads(resource: str) -> None:
    data = json.loads((INTEGRATION / resource).read_text())

    for section in ("config", "options"):
        assert set(data[section]) <= FLOW_SECTION_KEYS, f"{resource}: {section}"


@pytest.mark.parametrize("resource", ["strings.json", "translations/en.json", "translations/uk.json"])
def test_every_rendered_flow_step_has_translations(resource: str) -> None:
    """Each step_id the flows show must resolve in the matching step section."""
    data = json.loads((INTEGRATION / resource).read_text())

    assert {"thresholds", "init"} <= set(data["options"]["step"])
    assert "thresholds" in data["config"]["step"]


@pytest.mark.parametrize("resource", ["strings.json", "translations/en.json", "translations/uk.json"])
def test_described_fields_are_fields_the_step_renders(resource: str) -> None:
    """A description without a matching data label documents a field nobody sees."""
    data = json.loads((INTEGRATION / resource).read_text())

    for section in ("config", "options"):
        for step, body in data[section]["step"].items():
            described = set(body.get("data_description", {}))
            labelled = set(body.get("data", {}))
            assert described <= labelled, f"{resource}: {section}.step.{step}"
