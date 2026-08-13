"""Real Home Assistant behavioral tests for the core safety actions.

These exercise the actual load-shedding behavior end-to-end against a real
(in-process) Home Assistant runtime, rather than the hand-written mock layer:

- overload shed issues a bounded physical OFF with readback;
- grid loss triggers the emergency all-stop path;
- observe mode records an intended action but never calls a physical service;
- planner auto mode persists across a config-entry reload;
- automatic restore re-enables a pending load after a safe-capacity window.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.setup import async_setup_component
from power_orchestrator.const import (
    CONF_ADD_THRESHOLD,
    CONF_AVERAGING_PERIOD,
    CONF_DEVICES,
    CONF_GRID_LOSS_MODE,
    CONF_GRID_LOSS_SENSOR,
    CONF_LOAD_SENSOR,
    CONF_PAUSE_PERIOD,
    CONF_THRESHOLD_DURATION,
    CONF_THRESHOLD_POWER,
    CONF_THRESHOLDS,
    DOMAIN,
    GRID_LOSS_MODE_SENSOR,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

REPO = Path(__file__).parents[1]
LOAD_SENSOR = "sensor.test_load"
GRID_SENSOR = "binary_sensor.test_grid"
ACTUATOR = "input_boolean.test_boiler"


def _patch_restore_dwell(monkeypatch, coordinator, dwell: float) -> None:
    """Patch the const module actually imported by the running coordinator."""
    import sys

    policy_mod = sys.modules[type(coordinator._policy_engine).__module__]
    monkeypatch.setattr(policy_mod._const, "RESTORE_SAFE_CAPACITY_DWELL_S", dwell)


def _install_integration(hass) -> None:
    source = REPO / "custom_components" / DOMAIN
    destination = Path(hass.config.path("custom_components", DOMAIN))
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source, destination, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__")
    )


async def _prepare_entities(hass, *, initial_on: bool = True, load_w: str = "3000") -> None:
    assert await async_setup_component(
        hass, "input_boolean", {"input_boolean": {"test_boiler": {"initial": initial_on}}}
    )
    hass.states.async_set(LOAD_SENSOR, load_w, {"unit_of_measurement": "W"})
    hass.states.async_set(GRID_SENSOR, "on")
    await hass.async_block_till_done()


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Power Orchestrator behavior",
        data={
            CONF_LOAD_SENSOR: LOAD_SENSOR,
            CONF_AVERAGING_PERIOD: 30,
            CONF_PAUSE_PERIOD: 0,
            CONF_THRESHOLDS: [{"power_limit": 5000, "duration_s": 0}],
            CONF_DEVICES: [
                {
                    "device_id": "boiler",
                    "name": "Test boiler",
                    "entity": ACTUATOR,
                    "expected_power": 2000,
                    "priority": 1,
                }
            ],
            CONF_GRID_LOSS_MODE: GRID_LOSS_MODE_SENSOR,
            CONF_GRID_LOSS_SENSOR: GRID_SENSOR,
        },
        version=3,
        minor_version=1,
    )


async def _setup_loaded(hass) -> MockConfigEntry:
    _install_integration(hass)
    await _prepare_entities(hass)
    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def _shed_the_boiler(hass) -> None:
    """Arm auto, drive an overload, and confirm the planner shed the load."""
    await hass.services.async_call(DOMAIN, "set_mode", {"mode": "auto"}, blocking=True)
    await hass.async_block_till_done()
    hass.states.async_set(LOAD_SENSOR, "9500", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "force_evaluate", {}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(ACTUATOR).state == "off"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_automatic_restore_reenables_pending_load(hass, monkeypatch):
    """Auto restores a pending load after a continuous safe-capacity window."""
    entry = await _setup_loaded(hass)
    coordinator = entry.runtime_data.coordinator
    _patch_restore_dwell(monkeypatch, coordinator, 0.0)
    await _shed_the_boiler(hass)

    hass.states.async_set(LOAD_SENSOR, "2000", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "force_evaluate", {}, blocking=True)
    await hass.async_block_till_done()

    assert hass.states.get(ACTUATOR).state == "on"
    assert "boiler" not in coordinator.data["planner_shed_devices"]


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_restore_requires_safe_capacity(hass, monkeypatch):
    """Projected load at or above the lowest tier blocks automatic restore."""
    entry = await _setup_loaded(hass)
    _patch_restore_dwell(monkeypatch, entry.runtime_data.coordinator, 0.0)
    await _shed_the_boiler(hass)

    # 4000 + expected 2000 = 6000 is not strictly below 5000.
    hass.states.async_set(LOAD_SENSOR, "4000", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "force_evaluate", {}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(ACTUATOR).state == "off"

    hass.states.async_set(LOAD_SENSOR, "2000", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "force_evaluate", {}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(ACTUATOR).state == "on"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_thresholds_configurable_via_options_flow(hass):
    """Thresholds can be set through the real Options flow without static defaults."""
    _install_integration(hass)
    await _prepare_entities(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Power Orchestrator options",
        data={
            CONF_LOAD_SENSOR: LOAD_SENSOR,
            CONF_AVERAGING_PERIOD: 30,
            CONF_THRESHOLDS: [{"power_limit": 5000, "duration_s": 0}],
            CONF_DEVICES: [],
            CONF_GRID_LOSS_MODE: GRID_LOSS_MODE_SENSOR,
            CONF_GRID_LOSS_SENSOR: GRID_SENSOR,
        },
        version=3,
        minor_version=1,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_LOAD_SENSOR: LOAD_SENSOR,
            CONF_DEVICES: [],
            CONF_AVERAGING_PERIOD: 30,
            CONF_PAUSE_PERIOD: 0,
            CONF_GRID_LOSS_MODE: GRID_LOSS_MODE_SENSOR,
            CONF_GRID_LOSS_SENSOR: GRID_SENSOR,
        },
    )
    assert result["step_id"] == "thresholds"
    for power, duration, add_threshold in ((6500, 300, True), (8000, 5, False)):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_THRESHOLD_POWER: power,
                CONF_THRESHOLD_DURATION: duration,
                CONF_ADD_THRESHOLD: add_threshold,
            },
        )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()

    assert entry.options[CONF_THRESHOLDS] == [
        {"power_limit": 6500.0, "duration_s": 300.0},
        {"power_limit": 8000.0, "duration_s": 5.0},
    ]
    reloaded = entry.runtime_data.coordinator
    assert reloaded.policy.lowest_limit_w == 6500.0


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_restore_dwell_resets_on_overload(hass, monkeypatch):
    """A matured overload resets the automatic restore safe-capacity window."""
    entry = await _setup_loaded(hass)
    coordinator = entry.runtime_data.coordinator
    _patch_restore_dwell(monkeypatch, coordinator, 3600.0)
    await hass.services.async_call(DOMAIN, "set_mode", {"mode": "auto"}, blocking=True)
    await hass.async_block_till_done()
    coordinator.restore_pending_restore(["boiler"])
    # Device must be confirmed OFF to be a restore candidate.
    hass.states.async_set(ACTUATOR, "off")
    await hass.async_block_till_done()

    hass.states.async_set(LOAD_SENSOR, "2000", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "force_evaluate", {}, blocking=True)
    await hass.async_block_till_done()
    assert coordinator._policy_engine.runtime.restore_since is not None

    hass.states.async_set(LOAD_SENSOR, "9500", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "force_evaluate", {}, blocking=True)
    await hass.async_block_till_done()
    assert coordinator._policy_engine.runtime.restore_since is None


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_observe_never_restores(hass, monkeypatch):
    """Observe mode never issues a physical restore."""
    entry = await _setup_loaded(hass)
    coordinator = entry.runtime_data.coordinator
    _patch_restore_dwell(monkeypatch, coordinator, 0.0)
    await _shed_the_boiler(hass)
    await hass.services.async_call(DOMAIN, "set_mode", {"mode": "observe"}, blocking=True)
    await hass.async_block_till_done()

    hass.states.async_set(LOAD_SENSOR, "2000", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "force_evaluate", {}, blocking=True)
    await hass.async_block_till_done()

    assert hass.states.get(ACTUATOR).state == "off"
    assert coordinator.restore_commands_allowed is False


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_overload_zero_dwell_tier_sheds_one_load(hass):
    """A load above a zero-dwell tier is physically switched off in Auto."""
    entry = await _setup_loaded(hass)
    coordinator = entry.runtime_data.coordinator

    await hass.services.async_call(DOMAIN, "set_mode", {"mode": "auto"}, blocking=True)
    await hass.async_block_till_done()
    assert coordinator.mode == "auto"
    assert coordinator.physical_commands_allowed is True

    hass.states.async_set(LOAD_SENSOR, "9500", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "force_evaluate", {}, blocking=True)
    await hass.async_block_till_done()

    assert hass.states.get(ACTUATOR).state == "off"
    assert coordinator.status == "load_shedding"
    assert coordinator.reason_code == "shed_custom_threshold"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_grid_loss_triggers_emergency_stop(hass):
    """Grid loss drives the emergency all-stop path for a known-on load."""
    entry = await _setup_loaded(hass)
    coordinator = entry.runtime_data.coordinator

    await hass.services.async_call(DOMAIN, "set_mode", {"mode": "auto"}, blocking=True)
    await hass.async_block_till_done()

    hass.states.async_set(GRID_SENSOR, "off")
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "force_evaluate", {}, blocking=True)
    await hass.async_block_till_done()

    assert hass.states.get(ACTUATOR).state == "off"
    assert coordinator.status == "grid_loss"
    assert "boiler" in coordinator.data["planner_shed_devices"]


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_observe_mode_records_but_does_not_switch(hass):
    """Observe mode records an intended shed but never calls a physical service."""
    entry = await _setup_loaded(hass)
    coordinator = entry.runtime_data.coordinator
    assert coordinator.mode == "observe"
    assert coordinator.physical_commands_allowed is False

    hass.states.async_set(LOAD_SENSOR, "9500", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "force_evaluate", {}, blocking=True)
    await hass.async_block_till_done()

    assert hass.states.get(ACTUATOR).state == "on"
    assert coordinator.status == "observe"
    assert "observe" in coordinator.last_action.lower()


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_state_change_listener_triggers_evaluation(hass):
    """A tracked-entity state change alone drives a guarded evaluation."""
    entry = await _setup_loaded(hass)
    coordinator = entry.runtime_data.coordinator
    await hass.services.async_call(DOMAIN, "set_mode", {"mode": "auto"}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(ACTUATOR).state == "on"

    hass.states.async_set(GRID_SENSOR, "off")
    await hass.async_block_till_done()

    assert hass.states.get(ACTUATOR).state == "off"
    assert coordinator.status == "grid_loss"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_auto_mode_persists_across_reload(hass):
    """A persisted auto planner mode is restored after a config-entry reload."""
    entry = await _setup_loaded(hass)
    coordinator = entry.runtime_data.coordinator
    await hass.services.async_call(DOMAIN, "set_mode", {"mode": "auto"}, blocking=True)
    await hass.async_block_till_done()
    assert coordinator.mode == "auto"

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    reloaded = entry.runtime_data.coordinator
    assert reloaded.mode == "auto"
