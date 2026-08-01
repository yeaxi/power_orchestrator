"""Real Home Assistant loader/config-entry compatibility smoke test."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from power_orchestrator.const import (
    CONF_AVERAGING_PERIOD,
    CONF_DEVICES,
    CONF_GRID_LOSS_MODE,
    CONF_GRID_LOSS_SENSOR,
    CONF_HYSTERESIS,
    CONF_LOAD_SENSOR,
    CONF_MAX_LOAD,
    CONF_SAFETY_RESERVE,
    DOMAIN,
    GRID_LOSS_MODE_SENSOR,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry


@pytest.mark.usefixtures("hass", "enable_custom_integrations")
async def test_config_entry_loads_with_real_home_assistant(hass):
    """Load all platforms through the real HA config-entry lifecycle."""
    source = Path(__file__).parents[1] / "custom_components" / DOMAIN
    destination = Path(hass.config.path("custom_components", DOMAIN))
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Power Orchestrator smoke",
        data={
            CONF_LOAD_SENSOR: "sensor.test_load",
            CONF_MAX_LOAD: 5000,
            CONF_AVERAGING_PERIOD: 30,
            CONF_SAFETY_RESERVE: 200,
            CONF_HYSTERESIS: 100,
            CONF_DEVICES: [],
            CONF_GRID_LOSS_MODE: GRID_LOSS_MODE_SENSOR,
            CONF_GRID_LOSS_SENSOR: "binary_sensor.test_grid",
        },
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    registry = er.async_get(hass)
    registered = [
        item
        for item in registry.entities.values()
        if item.config_entry_id == entry.entry_id
    ]
    unique_ids = {item.unique_id for item in registered}
    assert f"{entry.entry_id}_sensor_status" in unique_ids
    assert f"{entry.entry_id}_sensor_current_load" in unique_ids
    assert f"{entry.entry_id}_binary_sensor_grid_ok" in unique_ids
    assert f"{entry.entry_id}_select_mode" in unique_ids

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


@pytest.mark.usefixtures("hass", "enable_custom_integrations")
async def test_config_flow_user_form_loads_with_real_home_assistant(hass):
    """Exercise the real HA ConfigFlow and selector compatibility boundary."""
    source = Path(__file__).parents[1] / "custom_components" / DOMAIN
    destination = Path(hass.config.path("custom_components", DOMAIN))
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
    )

    assert result["type"] == "form"
    assert result["step_id"] == "user"


@pytest.mark.usefixtures("hass", "enable_custom_integrations")
async def test_diagnostics_and_reconfigure_are_compatible_with_real_home_assistant(hass):
    """Exercise the new Gold-contract APIs against the real HA runtime."""
    source = Path(__file__).parents[1] / "custom_components" / DOMAIN
    destination = Path(hass.config.path("custom_components", DOMAIN))
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Power Orchestrator diagnostics smoke",
        data={
            CONF_LOAD_SENSOR: "sensor.test_load",
            CONF_MAX_LOAD: 5000,
            CONF_AVERAGING_PERIOD: 30,
            CONF_SAFETY_RESERVE: 200,
            CONF_HYSTERESIS: 100,
            CONF_DEVICES: [],
            CONF_GRID_LOSS_MODE: GRID_LOSS_MODE_SENSOR,
            CONF_GRID_LOSS_SENSOR: "binary_sensor.test_grid",
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
    )
    assert result["type"] == "form"
    assert result["step_id"] == "reconfigure"

    from power_orchestrator.diagnostics import async_get_config_entry_diagnostics

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["integration"] == DOMAIN
    assert diagnostics["runtime"]["loaded"] is True
    assert "entry_data" in diagnostics

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
