"""Real Home Assistant loader/config-entry compatibility smoke test."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from power_orchestrator.const import (
    CONF_ADD_THRESHOLD,
    CONF_AVERAGING_PERIOD,
    CONF_DEVICE_ACTUATORS,
    CONF_DEVICE_ENTITY,
    CONF_DEVICE_EXPECTED_POWER,
    CONF_DEVICE_NAME,
    CONF_DEVICE_POWER_SENSOR,
    CONF_DEVICES,
    CONF_GRID_LOSS_MODE,
    CONF_GRID_LOSS_SENSOR,
    CONF_LOAD_SENSOR,
    CONF_PRIORITY_ORDER,
    CONF_THRESHOLD_DURATION,
    CONF_THRESHOLD_POWER,
    DOMAIN,
    GRID_LOSS_MODE_SENSOR,
    STORAGE_VERSION,
)
from power_orchestrator.storage import RuntimeStore
from pytest_homeassistant_custom_component.common import MockConfigEntry


@pytest.mark.usefixtures("hass")
async def test_released_storage_envelope_remains_readable(hass):
    """The redesign reads the released v3 Home Assistant Store envelope."""
    key = "power_orchestrator_runtime_upgrade_test"
    released = Store(hass, 3, key)
    await released.async_save({"mode": "auto", "storage_version": 3})

    runtime = RuntimeStore(Store(hass, STORAGE_VERSION, key))
    await runtime.async_load()

    assert runtime.restore_mode() == "auto"


@pytest.mark.usefixtures("hass", "enable_custom_integrations")
async def test_config_entry_loads_with_real_home_assistant(hass):
    """Load all platforms through the real HA config-entry lifecycle."""
    source = Path(__file__).parents[1] / "custom_components" / DOMAIN
    destination = Path(hass.config.path("custom_components", DOMAIN))
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source, destination, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__")
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Power Orchestrator smoke",
        data={
            CONF_LOAD_SENSOR: "sensor.test_load",
            CONF_AVERAGING_PERIOD: 30,
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
        item for item in registry.entities.values() if item.config_entry_id == entry.entry_id
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
    shutil.copytree(
        source, destination, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__")
    )

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
    shutil.copytree(
        source, destination, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__")
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Power Orchestrator diagnostics smoke",
        data={
            CONF_LOAD_SENSOR: "sensor.test_load",
            CONF_AVERAGING_PERIOD: 30,
            CONF_DEVICES: [],
            CONF_GRID_LOSS_MODE: GRID_LOSS_MODE_SENSOR,
            CONF_GRID_LOSS_SENSOR: "binary_sensor.test_grid",
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
    )
    assert result["type"] == "form"
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_LOAD_SENSOR: "sensor.test_load",
            CONF_DEVICES: [],
            CONF_AVERAGING_PERIOD: 30,
            "pause_period": 60,
            CONF_GRID_LOSS_MODE: GRID_LOSS_MODE_SENSOR,
            CONF_GRID_LOSS_SENSOR: "binary_sensor.test_grid",
        },
    )
    assert result["step_id"] == "thresholds"
    for power, duration, add_threshold in (
        (6500, 300, True),
        (7000, 30, True),
        (8000, 5, False),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_THRESHOLD_POWER: power,
                CONF_THRESHOLD_DURATION: duration,
                CONF_ADD_THRESHOLD: add_threshold,
            },
        )
    assert result["type"] == "abort"
    assert entry.data["thresholds"] == [
        {"power_limit": 6500.0, "duration_s": 300.0},
        {"power_limit": 7000.0, "duration_s": 30.0},
        {"power_limit": 8000.0, "duration_s": 5.0},
    ]
    assert "battery_soc" not in entry.data

    options_result = await hass.config_entries.options.async_init(entry.entry_id)
    assert options_result["type"] == "form"
    assert options_result["step_id"] == "init"
    options_result = await hass.config_entries.options.async_configure(
        options_result["flow_id"],
        user_input={
            CONF_LOAD_SENSOR: "sensor.test_load",
            CONF_DEVICES: [],
            CONF_AVERAGING_PERIOD: 30,
            "pause_period": 60,
            CONF_GRID_LOSS_MODE: GRID_LOSS_MODE_SENSOR,
            CONF_GRID_LOSS_SENSOR: "binary_sensor.test_grid",
        },
    )
    assert options_result["step_id"] == "thresholds"
    for power, duration, add_threshold in (
        (6500, 300, True),
        (7000, 30, True),
        (8000, 5, False),
    ):
        options_result = await hass.config_entries.options.async_configure(
            options_result["flow_id"],
            user_input={
                CONF_THRESHOLD_POWER: power,
                CONF_THRESHOLD_DURATION: duration,
                CONF_ADD_THRESHOLD: add_threshold,
            },
        )
    assert options_result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.options["thresholds"] == entry.data["thresholds"]
    assert "battery_soc" not in entry.options

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    from power_orchestrator.diagnostics import async_get_config_entry_diagnostics

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["integration"] == DOMAIN
    assert diagnostics["runtime"]["loaded"] is True
    assert "entry_data" in diagnostics

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.usefixtures("hass", "enable_custom_integrations")
async def test_native_flow_exposes_repeatable_thresholds_and_reorderable_priority(hass):
    """Exercise changed setup steps with native HA selectors and schema validation."""
    source = Path(__file__).parents[1] / "custom_components" / DOMAIN
    destination = Path(hass.config.path("custom_components", DOMAIN))
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source, destination, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__")
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
    )
    flow_id = result["flow_id"]

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        user_input={"grid_power": "sensor.test_load"},
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        user_input={
            CONF_LOAD_SENSOR: "sensor.test_load",
            CONF_AVERAGING_PERIOD: 30,
        },
    )
    assert result["step_id"] == "thresholds"
    threshold_keys = {getattr(key, "schema", key) for key in result["data_schema"].schema}
    assert {CONF_THRESHOLD_POWER, CONF_THRESHOLD_DURATION, CONF_ADD_THRESHOLD} <= threshold_keys
    assert "threshold_count" not in threshold_keys

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        user_input={
            CONF_THRESHOLD_POWER: 6500,
            CONF_THRESHOLD_DURATION: 300,
            CONF_ADD_THRESHOLD: True,
        },
    )
    assert result["step_id"] == "thresholds"
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        user_input={
            CONF_THRESHOLD_POWER: 8000,
            CONF_THRESHOLD_DURATION: 5,
            CONF_ADD_THRESHOLD: False,
        },
    )
    assert result["step_id"] == "devices"

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        user_input={"discovered_devices": [], "add_custom_device": True},
    )
    assert result["step_id"] == "devices"
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        user_input={
            CONF_DEVICE_ENTITY: "switch.test_boiler",
            CONF_DEVICE_NAME: "Test boiler",
            CONF_DEVICE_EXPECTED_POWER: 2000,
            CONF_DEVICE_POWER_SENSOR: "sensor.test_boiler_power",
            CONF_DEVICE_ACTUATORS: [],
        },
    )
    assert result["step_id"] == "priority"
    priority_keys = {getattr(key, "schema", key) for key in result["data_schema"].schema}
    assert CONF_PRIORITY_ORDER in priority_keys
    priority_key = next(
        key
        for key in result["data_schema"].schema
        if getattr(key, "schema", key) == CONF_PRIORITY_ORDER
    )
    priority_config = result["data_schema"].schema[priority_key].config
    assert (
        priority_config.get("reorder") is True
        if isinstance(priority_config, dict)
        else priority_config.reorder is True
    )

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        user_input={CONF_PRIORITY_ORDER: ["switch.test_boiler"], "pause_period": 60},
    )
    assert result["step_id"] == "grid_loss"
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        user_input={CONF_GRID_LOSS_MODE: GRID_LOSS_MODE_SENSOR},
    )
    assert result["step_id"] == "grid_loss_source"
    source_keys = {getattr(key, "schema", key) for key in result["data_schema"].schema}
    assert CONF_GRID_LOSS_SENSOR in source_keys
    assert "battery_soc" not in source_keys

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        user_input={CONF_GRID_LOSS_SENSOR: "binary_sensor.test_grid"},
    )
    assert result["type"] == "create_entry"
    assert "battery_soc" not in result["data"]
