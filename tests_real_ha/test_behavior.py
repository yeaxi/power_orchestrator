"""Real Home Assistant behavioral tests for the core safety actions.

These exercise the actual load-shedding behavior end-to-end against a real
(in-process) Home Assistant runtime, rather than the hand-written mock layer:

- overload shed issues a bounded physical OFF with readback;
- grid loss triggers the emergency all-stop path;
- observe mode records an intended action but never calls a physical service;
- auto mode and the pending-restore queue persist across a config-entry reload;
- automatic restore re-enables pending loads after a safe-capacity window.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.setup import async_setup_component
from power_orchestrator.const import (
    CONF_ADD_THRESHOLD,
    CONF_AVERAGING_PERIOD,
    CONF_BATTERY_SOC,
    CONF_BATTERY_THRESHOLD,
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
    GRID_LOSS_MODE_THRESHOLD,
)
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_get_persistent_notifications,
)

REPO = Path(__file__).parents[1]
LOAD_SENSOR = "sensor.test_load"
GRID_SENSOR = "binary_sensor.test_grid"
ACTUATOR = "input_boolean.test_boiler"
ACTUATOR_B = "input_boolean.test_dryer"
BATTERY_SENSOR = "sensor.test_battery_soc"


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
        version=2,
        minor_version=3,
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
    assert "boiler" not in coordinator.data["pending_restore_ids"]


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
        version=2,
        minor_version=3,
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
    assert "boiler" in coordinator.data["pending_restore_ids"]


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
async def test_off_mode_never_switches_on_overload_or_grid_loss(hass):
    """Off records unsafe conditions without calling a physical device service."""
    entry = await _setup_loaded(hass)
    coordinator = entry.runtime_data.coordinator
    await hass.services.async_call(DOMAIN, "set_mode", {"mode": "off"}, blocking=True)
    await hass.async_block_till_done()

    hass.states.async_set(LOAD_SENSOR, "9500", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "force_evaluate", {}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(ACTUATOR).state == "on"

    hass.states.async_set(GRID_SENSOR, "off")
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "force_evaluate", {}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(ACTUATOR).state == "on"
    assert coordinator.physical_commands_allowed is False
    assert coordinator.emergency_commands_allowed is False


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_battery_threshold_emergency_stops_active_load(hass):
    """A confirmed low battery state uses the same emergency all-stop path."""
    _install_integration(hass)
    assert await async_setup_component(
        hass, "input_boolean", {"input_boolean": {"test_boiler": {"initial": True}}}
    )
    hass.states.async_set(LOAD_SENSOR, "3000", {"unit_of_measurement": "W"})
    hass.states.async_set(BATTERY_SENSOR, "80", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Power Orchestrator battery",
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
            CONF_GRID_LOSS_MODE: GRID_LOSS_MODE_THRESHOLD,
            CONF_BATTERY_SOC: BATTERY_SENSOR,
            CONF_BATTERY_THRESHOLD: 20,
        },
        version=2,
        minor_version=3,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "set_mode", {"mode": "auto"}, blocking=True)
    await hass.async_block_till_done()

    hass.states.async_set(BATTERY_SENSOR, "20", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "force_evaluate", {}, blocking=True)
    await hass.async_block_till_done()

    assert hass.states.get(ACTUATOR).state == "off"
    assert entry.runtime_data.coordinator.status == "grid_loss"
    assert entry.runtime_data.coordinator.data["pending_restore_ids"] == ["boiler"]


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
    """A persisted auto mode is restored after a config-entry reload."""
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


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_nonzero_tier_dwell_waits_before_shed(hass):
    """A non-zero dwell tier does not shed until the continuous exceedance matures."""
    _install_integration(hass)
    await _prepare_entities(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Power Orchestrator dwell",
        data={
            CONF_LOAD_SENSOR: LOAD_SENSOR,
            CONF_AVERAGING_PERIOD: 30,
            CONF_PAUSE_PERIOD: 0,
            CONF_THRESHOLDS: [{"power_limit": 5000, "duration_s": 300}],
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
        version=2,
        minor_version=3,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator

    await hass.services.async_call(DOMAIN, "set_mode", {"mode": "auto"}, blocking=True)
    await hass.async_block_till_done()
    hass.states.async_set(LOAD_SENSOR, "9500", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "force_evaluate", {}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(ACTUATOR).state == "on"
    assert "custom_1" in coordinator._policy_engine.runtime.tier_since

    coordinator._policy_engine.runtime.tier_since["custom_1"] = time.monotonic() - 301
    await hass.services.async_call(DOMAIN, "force_evaluate", {}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(ACTUATOR).state == "off"
    assert coordinator.status == "load_shedding"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_zero_dwell_top_tier_sheds_immediately(hass):
    """A zero-dwell top tier sheds while a lower non-zero tier is still waiting."""
    _install_integration(hass)
    await _prepare_entities(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Power Orchestrator top tier",
        data={
            CONF_LOAD_SENSOR: LOAD_SENSOR,
            CONF_AVERAGING_PERIOD: 30,
            CONF_PAUSE_PERIOD: 0,
            CONF_THRESHOLDS: [
                {"power_limit": 5000, "duration_s": 300},
                {"power_limit": 9000, "duration_s": 0},
            ],
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
        version=2,
        minor_version=3,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(DOMAIN, "set_mode", {"mode": "auto"}, blocking=True)
    await hass.async_block_till_done()
    hass.states.async_set(LOAD_SENSOR, "9500", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "force_evaluate", {}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(ACTUATOR).state == "off"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_invalid_load_and_unavailable_safety_notify_without_device_services(hass):
    """Invalid load and unavailable safety telemetry notify and never call device services."""
    assert await async_setup_component(hass, "persistent_notification", {})
    entry = await _setup_loaded(hass)
    coordinator = entry.runtime_data.coordinator
    await hass.services.async_call(DOMAIN, "set_mode", {"mode": "auto"}, blocking=True)
    await hass.async_block_till_done()

    hass.states.async_set(LOAD_SENSOR, "unknown", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "force_evaluate", {}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(ACTUATOR).state == "on"
    assert coordinator.status == "safety_blocked"
    assert coordinator._telemetry_notification_active is True
    notifications = async_get_persistent_notifications(hass)
    assert any(
        "telemetry blocked" in str(item.get("title", "")).lower()
        for item in notifications.values()
    )

    hass.states.async_set(LOAD_SENSOR, "3000", {"unit_of_measurement": "W"})
    hass.states.async_set(GRID_SENSOR, "unavailable")
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "force_evaluate", {}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(ACTUATOR).state == "on"
    assert coordinator.status == "safety_blocked"
    assert coordinator.physical_commands_allowed is False


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_manual_on_pending_under_safe_capacity_is_accepted(hass, monkeypatch):
    """Manual ON of a pending load under safe capacity is accepted and dequeued."""
    entry = await _setup_loaded(hass)
    coordinator = entry.runtime_data.coordinator
    _patch_restore_dwell(monkeypatch, coordinator, 0.0)
    await _shed_the_boiler(hass)
    assert "boiler" in coordinator.data["pending_restore_ids"]

    hass.states.async_set(LOAD_SENSOR, "2000", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    hass.states.async_set(ACTUATOR, "on")
    await hass.async_block_till_done()

    assert "boiler" not in coordinator.data["pending_restore_ids"]
    assert hass.states.get(ACTUATOR).state == "on"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_manual_on_pending_under_overload_is_reshed(hass):
    """Manual ON of a pending load under enforced overload is re-shed and stays queued."""
    entry = await _setup_loaded(hass)
    coordinator = entry.runtime_data.coordinator
    await _shed_the_boiler(hass)
    assert coordinator.data["pending_restore_ids"] == ["boiler"]

    hass.states.async_set(LOAD_SENSOR, "9500", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    # Seed the zero-dwell tier so overload is already enforced for re-shed.
    coordinator._policy_engine.observe_load(9500.0, now=time.monotonic())
    hass.states.async_set(ACTUATOR, "on")
    await hass.async_block_till_done()

    assert hass.states.get(ACTUATOR).state == "off"
    assert coordinator.data["pending_restore_ids"] == ["boiler"]


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_pending_queue_survives_reload_and_then_restores(hass, monkeypatch):
    """A pending load remains eligible for automatic restore after reload."""
    entry = await _setup_loaded(hass)
    await _shed_the_boiler(hass)
    coordinator = entry.runtime_data.coordinator
    assert coordinator.data["pending_restore_ids"] == ["boiler"]
    assert coordinator.data["pending_restore_names"] == ["Test boiler"]

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    reloaded = entry.runtime_data.coordinator
    _patch_restore_dwell(monkeypatch, reloaded, 0.0)
    assert reloaded.data["pending_restore_ids"] == ["boiler"]
    assert reloaded.data["pending_restore_names"] == ["Test boiler"]
    status_entity = next(
        (
            state
            for state in hass.states.async_all("sensor")
            if state.attributes.get("pending_restore_ids") == ["boiler"]
        ),
        None,
    )
    assert status_entity is not None
    assert status_entity.attributes.get("pending_restore_names") == ["Test boiler"]

    hass.states.async_set(LOAD_SENSOR, "2000", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "force_evaluate", {}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(ACTUATOR).state == "on"
    assert reloaded.data["pending_restore_ids"] == []


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_grid_loss_queues_multiple_and_restores_reverse_order(hass, monkeypatch):
    """Grid-loss all-stop queues multiple loads and restores them in reverse stop order."""
    _install_integration(hass)
    assert await async_setup_component(
        hass,
        "input_boolean",
        {
            "input_boolean": {
                "test_boiler": {"initial": True},
                "test_dryer": {"initial": True},
            }
        },
    )
    hass.states.async_set(LOAD_SENSOR, "3000", {"unit_of_measurement": "W"})
    hass.states.async_set(GRID_SENSOR, "on")
    await hass.async_block_till_done()

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Power Orchestrator multi",
        data={
            CONF_LOAD_SENSOR: LOAD_SENSOR,
            CONF_AVERAGING_PERIOD: 30,
            CONF_PAUSE_PERIOD: 0,
            CONF_THRESHOLDS: [{"power_limit": 8000, "duration_s": 0}],
            CONF_DEVICES: [
                {
                    "device_id": "boiler",
                    "name": "Test boiler",
                    "entity": ACTUATOR,
                    "expected_power": 1500,
                    "priority": 1,
                },
                {
                    "device_id": "dryer",
                    "name": "Test dryer",
                    "entity": ACTUATOR_B,
                    "expected_power": 1500,
                    "priority": 2,
                },
            ],
            CONF_GRID_LOSS_MODE: GRID_LOSS_MODE_SENSOR,
            CONF_GRID_LOSS_SENSOR: GRID_SENSOR,
        },
        version=2,
        minor_version=3,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    _patch_restore_dwell(monkeypatch, coordinator, 0.0)

    await hass.services.async_call(DOMAIN, "set_mode", {"mode": "auto"}, blocking=True)
    await hass.async_block_till_done()
    hass.states.async_set(GRID_SENSOR, "off")
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "force_evaluate", {}, blocking=True)
    await hass.async_block_till_done()

    assert hass.states.get(ACTUATOR).state == "off"
    assert hass.states.get(ACTUATOR_B).state == "off"
    # Emergency stops reverse shed order: dryer then boiler.
    assert coordinator.data["pending_restore_ids"] == ["dryer", "boiler"]

    # Raise aggregate first so grid recovery cannot restore under residual headroom.
    hass.states.async_set(LOAD_SENSOR, "7000", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    hass.states.async_set(GRID_SENSOR, "on")
    await hass.async_block_till_done()
    assert hass.states.get(ACTUATOR).state == "off"
    assert hass.states.get(ACTUATOR_B).state == "off"
    assert coordinator.data["pending_restore_ids"] == ["dryer", "boiler"]

    hass.states.async_set(LOAD_SENSOR, "1000", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    # Reverse actual stop order restores boiler first.
    assert hass.states.get(ACTUATOR).state == "on"
    assert hass.states.get(ACTUATOR_B).state == "off"
    assert coordinator.data["pending_restore_ids"] == ["dryer"]

    hass.states.async_set(LOAD_SENSOR, "1100", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    assert hass.states.get(ACTUATOR_B).state == "on"
    assert coordinator.data["pending_restore_ids"] == []
