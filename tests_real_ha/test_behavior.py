"""Real Home Assistant behavioral tests for the core safety actions.

These exercise the actual load-shedding behavior end-to-end against a real
(in-process) Home Assistant runtime, rather than the hand-written mock layer:

- overload hard-interlock shed issues a bounded physical OFF with readback;
- grid loss triggers the emergency all-stop path;
- observe mode records an intended action but never calls a physical service;
- planner auto mode persists across a config-entry reload.

An ``input_boolean`` helper is used as a real, switchable actuator; no real
hardware is involved.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.setup import async_setup_component
from power_orchestrator.const import (
    CONF_AVERAGING_PERIOD,
    CONF_DEVICES,
    CONF_GRID_LOSS_MODE,
    CONF_GRID_LOSS_SENSOR,
    CONF_HYSTERESIS,
    CONF_LOAD_SENSOR,
    CONF_MAX_LOAD,
    CONF_PAUSE_PERIOD,
    CONF_RESTORE_COOLDOWN,
    CONF_RESTORE_DWELL,
    CONF_RESTORE_ENABLED,
    CONF_RESTORE_HYSTERESIS,
    CONF_RESTORE_THRESHOLD,
    CONF_SAFETY_RESERVE,
    DOMAIN,
    GRID_LOSS_MODE_SENSOR,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

REPO = Path(__file__).parents[1]
LOAD_SENSOR = "sensor.test_load"
GRID_SENSOR = "binary_sensor.test_grid"
ACTUATOR = "input_boolean.test_boiler"


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
            CONF_MAX_LOAD: 5000,
            CONF_AVERAGING_PERIOD: 30,
            CONF_SAFETY_RESERVE: 200,
            CONF_HYSTERESIS: 100,
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


def _restore_entry() -> MockConfigEntry:
    """Entry with guarded restore enabled and no pause/cooldown/dwell delays."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Power Orchestrator restore",
        data={
            CONF_LOAD_SENSOR: LOAD_SENSOR,
            CONF_MAX_LOAD: 5000,
            CONF_AVERAGING_PERIOD: 30,
            CONF_SAFETY_RESERVE: 200,
            CONF_HYSTERESIS: 100,
            CONF_PAUSE_PERIOD: 0,
            CONF_RESTORE_ENABLED: True,
            CONF_RESTORE_THRESHOLD: 4000,
            CONF_RESTORE_HYSTERESIS: 200,
            CONF_RESTORE_DWELL: 0,
            CONF_RESTORE_COOLDOWN: 0,
            CONF_DEVICES: [
                {
                    "device_id": "boiler",
                    "name": "Test boiler",
                    "entity": ACTUATOR,
                    "expected_power": 500,
                    "priority": 1,
                    "restore_enabled": True,
                }
            ],
            CONF_GRID_LOSS_MODE: GRID_LOSS_MODE_SENSOR,
            CONF_GRID_LOSS_SENSOR: GRID_SENSOR,
        },
    )


async def _setup_restore(hass) -> MockConfigEntry:
    _install_integration(hass)
    await _prepare_entities(hass)
    entry = _restore_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def _shed_the_boiler(hass) -> None:
    """Arm auto/live, drive an overload, and confirm the planner shed the load."""
    await hass.services.async_call(DOMAIN, "set_mode", {"mode": "auto"}, blocking=True)
    await hass.async_block_till_done()
    hass.states.async_set(LOAD_SENSOR, "9500", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "force_evaluate", {}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(ACTUATOR).state == "off"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_guarded_restore_reenables_planner_shed_load(hass):
    """Once armed and safe, the planner restores a load it shed itself."""
    entry = await _setup_restore(hass)
    coordinator = entry.runtime_data.coordinator
    await _shed_the_boiler(hass)

    await hass.services.async_call(
        DOMAIN, "authorize_restore", {"confirm_restore": True}, blocking=True
    )
    await hass.async_block_till_done()

    # Load returns well under the restore ceiling; the pending post-shed fence
    # reconciles on this newer report and the guarded restore then fires.
    hass.states.async_set(LOAD_SENSOR, "3000", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "force_evaluate", {}, blocking=True)
    await hass.async_block_till_done()

    # The planner-shed load is physically re-enabled and released from the
    # restore-candidate set. (Status settles back to monitoring once the
    # turn-on state change triggers a follow-up evaluation.)
    assert hass.states.get(ACTUATOR).state == "on"
    assert "boiler" not in coordinator.data["planner_shed_devices"]


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_restore_does_not_fire_until_armed(hass):
    """A shed load stays off when restore is enabled but not explicitly armed."""
    entry = await _setup_restore(hass)
    coordinator = entry.runtime_data.coordinator
    await _shed_the_boiler(hass)

    assert coordinator.restore_commands_allowed is False
    hass.states.async_set(LOAD_SENSOR, "3000", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "force_evaluate", {}, blocking=True)
    await hass.async_block_till_done()

    assert hass.states.get(ACTUATOR).state == "off"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_overload_hard_interlock_sheds_one_load(hass):
    """A load above the hard interlock is physically switched off in auto+live."""
    entry = await _setup_loaded(hass)
    coordinator = entry.runtime_data.coordinator

    await hass.services.async_call(DOMAIN, "set_mode", {"mode": "auto"}, blocking=True)
    await hass.async_block_till_done()
    assert coordinator.mode == "auto"
    assert coordinator.execution_mode == "live"

    hass.states.async_set(LOAD_SENSOR, "9500", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "force_evaluate", {}, blocking=True)
    await hass.async_block_till_done()

    assert hass.states.get(ACTUATOR).state == "off"
    assert coordinator.status == "load_shedding"
    assert coordinator.reason_code == "hard_interlock"


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


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_observe_mode_records_but_does_not_switch(hass):
    """Observe mode records an intended shed but never calls a physical service."""
    entry = await _setup_loaded(hass)
    coordinator = entry.runtime_data.coordinator
    assert coordinator.execution_mode == "observe"

    # Claim planner ownership of the already-on load (observe-only, no physical
    # call) so the load is an eligible shed candidate under observe execution.
    await hass.services.async_call(
        DOMAIN,
        "authorize_shedding",
        {"device_ids": ["boiler"], "confirm_takeover": True},
        blocking=True,
    )
    await hass.async_block_till_done()

    hass.states.async_set(LOAD_SENSOR, "9500", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "force_evaluate", {}, blocking=True)
    await hass.async_block_till_done()

    # Observe must record an intended action but leave the actuator untouched.
    assert hass.states.get(ACTUATOR).state == "on"
    assert coordinator.status == "observe"
    assert "observe" in coordinator.last_action.lower()


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_state_change_listener_triggers_evaluation(hass):
    """A tracked-entity state change alone drives a guarded evaluation.

    This exercises the entity-scoped ``async_track_state_change_event``
    subscription without any explicit ``force_evaluate`` call.
    """
    entry = await _setup_loaded(hass)
    coordinator = entry.runtime_data.coordinator
    await hass.services.async_call(DOMAIN, "set_mode", {"mode": "auto"}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(ACTUATOR).state == "on"

    # Flip a tracked safety input; the listener must wake the coordinator.
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
