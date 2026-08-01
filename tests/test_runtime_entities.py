"""Runtime, entity lifecycle, options, and setup contract tests."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components"))

from power_orchestrator import (
    HomeAssistantError,
    ServiceValidationError,
    _async_setup_entry_impl,
    _lifecycle_state,
    _loaded_runtimes,
    _register_services,
    _repair_device_ids,
    _safe_number,
    _sync_repair_issues,
    _translated_error,
    _valid_entity_id,
    async_migrate_entry,
    async_setup,
    async_setup_entry,
    async_unload_entry,
)
from power_orchestrator.binary_sensor import (
    PowerOrchestratorActionJournalHealthySensor,
    PowerOrchestratorFaultSensor,
    PowerOrchestratorGridOkSensor,
    PowerOrchestratorRecoveryBlockedSensor,
    async_setup_entry as async_setup_binary,
)
from power_orchestrator.config_flow import PowerOrchestratorOptionsFlow
from power_orchestrator.const import (
    CONF_GRID_LOSS_MODE,
    CONF_GRID_LOSS_SENSOR,
    CONF_BATTERY_SOC,
    CONF_DEVICES,
    CONF_LOAD_SENSOR,
    DOMAIN,
    GRID_LOSS_MODE_SENSOR,
    GRID_LOSS_MODE_THRESHOLD,
    MODE_OFF,
)
from power_orchestrator.select import (
    PowerOrchestratorModeSelect,
    async_setup_entry as async_setup_select,
)
from power_orchestrator.sensor import (
    PowerOrchestratorAverageLoadSensor,
    PowerOrchestratorAvailableCapacitySensor,
    PowerOrchestratorCurrentLoadSensor,
    PowerOrchestratorExecutionModeSensor,
    PowerOrchestratorLastOperationSensor,
    PowerOrchestratorLastActionSensor,
    PowerOrchestratorReasonCodeSensor,
    PowerOrchestratorStatusSensor,
    async_setup_entry as async_setup_sensor,
)
from power_orchestrator.storage import RuntimeStore


class _FakeStore:
    def __init__(self, data=None):
        self.data = data
        self.saves = 0

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        self.data = data
        self.saves += 1


class _LegacyError(Exception):
    pass


@pytest.mark.asyncio
async def test_init_helpers_fail_closed_and_global_setup_contracts():
    assert str(_translated_error(_LegacyError, "legacy_key")) == "legacy_key"
    translated = _translated_error(HomeAssistantError, "current_key", reason="bad input")
    assert isinstance(translated, HomeAssistantError)
    assert _repair_device_ids({"devices": ["d1", "", 1]}, "devices") == {"d1"}
    assert _repair_device_ids({"devices": "invalid"}, "devices") == set()
    assert _valid_entity_id("switch.ok", frozenset({"switch"})) is True
    assert _valid_entity_id("sensor.no", frozenset({"switch"})) is False
    assert _valid_entity_id(None, frozenset({"switch"})) is False
    assert _safe_number("2.5", default=1, minimum=0, maximum=3) == 2.5
    assert _safe_number(True, default=1, minimum=0, maximum=3) == 1
    assert _safe_number("bad", default=1, minimum=0, maximum=3) == 1
    assert _safe_number(float("inf"), default=1, minimum=0, maximum=3) == 1

    hass = SimpleNamespace(data=[])
    state = _lifecycle_state(hass)
    assert isinstance(hass.data, dict)
    assert isinstance(state["lock"], asyncio.Lock)
    assert isinstance(state["reservations"], set)

    no_entries = SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda _: {"bad": 1}))
    assert _loaded_runtimes(no_entries) == []
    no_api = SimpleNamespace(config_entries=SimpleNamespace())
    assert _loaded_runtimes(no_api) == []

    malformed_coordinator = SimpleNamespace(data=[])
    malformed_hass = SimpleNamespace(data=[])
    issue_module = ModuleType("homeassistant.helpers.issue_registry")
    issue_module.IssueSeverity = SimpleNamespace(ERROR="error")
    issue_module.async_create_issue = MagicMock()
    issue_module.async_delete_issue = MagicMock()
    with patch.dict(sys.modules, {"homeassistant.helpers.issue_registry": issue_module}):
        _sync_repair_issues(
            malformed_hass,
            malformed_coordinator,
            MagicMock(all_devices=lambda: []),
        )
    assert isinstance(malformed_hass.data, dict)

    setup_hass = MagicMock()
    with patch("power_orchestrator._register_services", new=AsyncMock()) as register:
        assert await async_setup(setup_hass, {}) is True
    register.assert_awaited_once_with(setup_hass)
    assert await async_migrate_entry(setup_hass, SimpleNamespace()) is True


@pytest.mark.asyncio
async def test_runtime_store_persists_and_restores_mode():
    fake = _FakeStore({"mode": MODE_OFF})
    store = RuntimeStore(fake)
    await store.async_load()
    assert store.restore_mode() == MODE_OFF
    assert store.restore_mode() != "invalid"

    store.set_mode("invalid")
    assert store.restore_mode() == MODE_OFF
    store.set_mode("auto")
    await store.async_save()
    assert fake.data["mode"] == "auto"


@pytest.mark.asyncio
async def test_entity_platform_setup_has_platform_scoped_unique_ids():
    coordinator = MagicMock()
    coordinator.hass = MagicMock()
    entry = SimpleNamespace(
        entry_id="entry-1",
        runtime_data=SimpleNamespace(coordinator=coordinator),
    )
    hass = SimpleNamespace()

    sensor_add = MagicMock()
    await async_setup_sensor(hass, entry, sensor_add)
    sensor_ids = [entity._attr_unique_id for entity in sensor_add.call_args.args[0]]
    assert len(sensor_ids) == 8
    assert all("_sensor_" in entity_id for entity_id in sensor_ids)

    binary_add = MagicMock()
    await async_setup_binary(hass, entry, binary_add)
    binary_entities = binary_add.call_args.args[0]
    assert len(binary_entities) == 4
    assert binary_entities[0]._attr_unique_id == "entry-1_binary_sensor_grid_ok"
    assert binary_entities[1]._attr_unique_id == "entry-1_binary_sensor_faulted"
    assert binary_entities[2]._attr_unique_id == "entry-1_binary_sensor_recovery_blocked"
    assert binary_entities[3]._attr_unique_id == "entry-1_binary_sensor_action_journal_healthy"


@pytest.mark.asyncio
async def test_quarantine_entities_expose_persisted_device_ids():
    coordinator = MagicMock()
    coordinator.data = {
        "faulted_devices": ["d1"],
        "fault_reasons": {"d1": "relay_readback_timeout"},
        "recovery_blocked_devices": ["d2"],
        "next_restore_target": "d2",
    }
    entry = SimpleNamespace(entry_id="entry-1")
    faulted = PowerOrchestratorFaultSensor(coordinator, entry)
    blocked = PowerOrchestratorRecoveryBlockedSensor(coordinator, entry)

    assert faulted.is_on is True
    assert faulted.extra_state_attributes == {
        "device_ids": ["d1"],
        "device_reasons": {"d1": "relay_readback_timeout"},
    }
    assert blocked.is_on is True
    assert blocked.extra_state_attributes == {
        "device_ids": ["d2"],
        "device_reasons": {"d1": "relay_readback_timeout"},
        "next_restore_target": "d2",
    }


def test_diagnostic_entities_expose_execution_reason_and_journal_state():
    coordinator = MagicMock()
    coordinator.execution_mode = "observe"
    coordinator.mode = "off"
    coordinator.reason_code = "observe_mode"
    coordinator.data = {
        "physical_commands_allowed": False,
        "journal_persistence_blocked": False,
        "status": "observe",
        "policy_phase": "monitoring",
        "safety_fault_reason": None,
        "load_sensor_reason": "ok",
        "last_operation_result": "observe_only",
        "last_action_id": "observe-1",
        "last_operation_id": "observe-observe-1",
        "pending_action_id": None,
        "journal_unresolved_count": 0,
        "action_journal_invalid": False,
        "audit_history": [],
    }
    entry = SimpleNamespace(entry_id="entry-1")

    execution = PowerOrchestratorExecutionModeSensor(coordinator, entry)
    reason = PowerOrchestratorReasonCodeSensor(coordinator, entry)
    operation = PowerOrchestratorLastOperationSensor(coordinator, entry)
    journal = PowerOrchestratorActionJournalHealthySensor(coordinator, entry)

    assert execution.native_value == "observe"
    assert execution.extra_state_attributes["physical_commands_allowed"] is False
    assert reason.native_value == "observe_mode"
    assert reason.extra_state_attributes["status"] == "observe"
    assert operation.native_value == "observe_only"
    assert operation.extra_state_attributes["action_id"] == "observe-1"
    assert journal.is_on is True

    coordinator.data["journal_persistence_blocked"] = True
    assert journal.is_on is False


def test_last_operation_entity_bounds_recorder_attributes_without_losing_total():
    coordinator = MagicMock()
    coordinator.data = {
        "last_operation_result": "observe_only",
        "last_action_id": "observe-99",
        "last_operation_id": "observe-observe-99",
        "pending_action_id": None,
        "journal_unresolved_count": 0,
        "action_journal_invalid": False,
        "journal_persistence_blocked": False,
        "audit_history": [
            {
                "action_id": f"action-{index}",
                "operation_id": f"operation-{index}",
                "action": "grid_loss_all_stop",
                "phase": "observe_only",
                "result": "observe_only",
                "reason": "r" * 160,
                "source": "grid_loss",
                "actor_id": "a" * 128,
                "context_id": "c" * 128,
            }
            for index in range(100)
        ],
    }
    entry = SimpleNamespace(entry_id="entry-1")

    operation = PowerOrchestratorLastOperationSensor(coordinator, entry)
    attributes = operation.extra_state_attributes

    assert len(attributes["audit_history"]) == 12
    assert attributes["audit_history_total"] == 100
    assert attributes["audit_history_truncated"] == 88
    assert attributes["audit_history"][0]["action_id"] == "action-88"
    assert len(json.dumps(attributes, separators=(",", ":"))) < 12000


@pytest.mark.asyncio
async def test_invalid_load_entities_are_unavailable_with_reason_on_status():
    coordinator = MagicMock()
    coordinator.load_sensor_valid = False
    coordinator.load_sensor_reason = "unsupported_unit"
    coordinator.current_load = None
    coordinator.average_load = None
    coordinator.available_capacity = None
    coordinator.status = "safety_blocked"
    coordinator.mode = "auto"
    coordinator.grid_ok = True
    coordinator.last_action = "Safety blocked — load sensor unsupported_unit"
    entry = SimpleNamespace(entry_id="entry-1")

    current = PowerOrchestratorCurrentLoadSensor(coordinator, entry)
    average = PowerOrchestratorAverageLoadSensor(coordinator, entry)
    capacity = PowerOrchestratorAvailableCapacitySensor(coordinator, entry)
    status = PowerOrchestratorStatusSensor(coordinator, entry)

    for entity in (current, average, capacity):
        assert entity.available is False
        assert entity.native_value is None
    assert status.available is True
    assert status.extra_state_attributes["load_sensor_reason"] == "unsupported_unit"


@pytest.mark.asyncio
async def test_grid_ok_entity_is_unavailable_only_when_source_is_unconfigured():
    coordinator = MagicMock()
    coordinator.grid_safety_source_configured = False
    coordinator.grid_ok = False
    entry = SimpleNamespace(entry_id="entry-1")

    entity = PowerOrchestratorGridOkSensor(coordinator, entry)
    assert entity.available is False
    assert entity.is_on is False

    coordinator.grid_safety_source_configured = True
    assert entity.available is True
    assert entity.is_on is False


@pytest.mark.asyncio
async def test_mode_select_rejects_unknown_option_and_persists_valid_option():
    coordinator = MagicMock()
    coordinator.mode = "auto"
    coordinator.async_set_mode = AsyncMock()
    entry = SimpleNamespace(entry_id="entry-1")
    entity = PowerOrchestratorModeSelect(coordinator, entry)
    entity.async_write_ha_state = MagicMock()

    with pytest.raises(ValueError):
        await entity.async_select_option("invalid")

    await entity.async_select_option("off")
    coordinator.async_set_mode.assert_awaited_once_with("off")
    entity.async_write_ha_state.assert_called_once()


@pytest.mark.asyncio
async def test_mode_select_setup_requires_runtime_and_exposes_current_option():
    entry = SimpleNamespace(entry_id="entry-select", runtime_data=None)
    with pytest.raises(RuntimeError, match="runtime data is unavailable"):
        await async_setup_select(MagicMock(), entry, MagicMock())

    add_entities = MagicMock()
    coordinator = MagicMock()
    coordinator.mode = "off"
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    await async_setup_select(MagicMock(), entry, add_entities)
    entity = add_entities.call_args.args[0][0]
    assert entity.current_option == "off"


@pytest.mark.asyncio
async def test_options_flow_exposes_runtime_and_safety_settings():
    entry = SimpleNamespace(
        data={
            CONF_LOAD_SENSOR: "sensor.load",
            "max_load": 5000,
            CONF_GRID_LOSS_MODE: GRID_LOSS_MODE_SENSOR,
        },
        options={},
    )
    flow = PowerOrchestratorOptionsFlow(entry)
    result = await flow.async_step_init()
    assert result["type"] == "form"
    keys = {str(key) for key in result["data_schema"].schema}
    assert CONF_LOAD_SENSOR in keys
    assert CONF_GRID_LOSS_MODE in keys

    created = await flow.async_step_init(
        {
            CONF_LOAD_SENSOR: "sensor.new_load",
            "max_load": 4200,
            CONF_GRID_LOSS_MODE: GRID_LOSS_MODE_SENSOR,
            CONF_GRID_LOSS_SENSOR: "binary_sensor.grid",
        }
    )
    assert created["type"] == "create_entry"
    assert created["data"]["max_load"] == 4200


@pytest.mark.asyncio
async def test_options_flow_exposes_and_persists_runtime_device_mappings():
    existing_device = {
        "device_id": "old",
        "name": "Old device",
        "entity": "switch.old",
        "expected_power": 1000,
        "power_sensor": "sensor.old_power",
        "only_from_solar": False,
        "priority": 1,
    }
    entry = SimpleNamespace(
        data={
            CONF_LOAD_SENSOR: "sensor.load",
            CONF_DEVICES: [existing_device],
            CONF_GRID_LOSS_MODE: GRID_LOSS_MODE_SENSOR,
        },
        options={},
    )
    flow = PowerOrchestratorOptionsFlow(entry)
    form = await flow.async_step_init()
    keys = {str(key) for key in form["data_schema"].schema}
    assert CONF_DEVICES in keys
    assert "solar_power" in keys
    assert "solar_forecast_entry" in keys
    assert "battery_power" in keys

    replacement = {
        "device_id": "new",
        "name": "New device",
        "entity": "switch.new",
        "expected_power": 1800,
        "power_sensor": "sensor.new_power",
        "only_from_solar": True,
        "priority": 1,
        "actuators": [],
        "hvac_mode_on": "heat",
        "shed_priority": 1,
        "restore_priority": None,
    }
    created = await flow.async_step_init(
        {
            CONF_LOAD_SENSOR: "sensor.new_load",
            CONF_DEVICES: [replacement],
            "solar_power": "sensor.pv_power",
            "solar_forecast_entry": "forecast-entry",
            "battery_power": "sensor.battery_power",
            "max_load": 4200,
            CONF_GRID_LOSS_MODE: GRID_LOSS_MODE_SENSOR,
            CONF_GRID_LOSS_SENSOR: "binary_sensor.grid",
        }
    )

    assert created["type"] == "create_entry"
    assert created["data"][CONF_DEVICES] == [replacement]
    assert created["data"]["solar_power"] == "sensor.pv_power"
    assert created["data"]["solar_forecast_entry"] == "forecast-entry"
    assert created["data"]["battery_power"] == "sensor.battery_power"


@pytest.mark.asyncio
async def test_options_flow_rejects_invalid_device_mapping():
    entry = SimpleNamespace(data={CONF_LOAD_SENSOR: "sensor.load"}, options={})
    flow = PowerOrchestratorOptionsFlow(entry)

    result = await flow.async_step_init(
        {
            CONF_LOAD_SENSOR: "sensor.load",
            CONF_DEVICES: [
                {
                    "device_id": "bad",
                    "name": "Bad device",
                    "entity": "sensor.not_a_control",
                    "expected_power": 0,
                }
            ],
            CONF_GRID_LOSS_MODE: GRID_LOSS_MODE_THRESHOLD,
            CONF_BATTERY_SOC: "sensor.battery_soc",
        }
    )

    assert result["type"] == "form"
    assert result["errors"]["base"] == "invalid_devices"


@pytest.mark.asyncio
async def test_options_flow_rejects_missing_grid_sensor_in_sensor_mode():
    entry = SimpleNamespace(data={}, options={})
    flow = PowerOrchestratorOptionsFlow(entry)

    result = await flow.async_step_init(
        {
            CONF_LOAD_SENSOR: "sensor.load",
            "max_load": 5000,
            CONF_GRID_LOSS_MODE: GRID_LOSS_MODE_SENSOR,
        }
    )

    assert result["type"] == "form"
    assert result["errors"]["base"] == "missing_grid_loss_sensor"


@pytest.mark.asyncio
async def test_options_flow_rejects_missing_battery_soc_in_threshold_mode():
    entry = SimpleNamespace(data={}, options={})
    flow = PowerOrchestratorOptionsFlow(entry)

    result = await flow.async_step_init(
        {
            CONF_LOAD_SENSOR: "sensor.load",
            "max_load": 5000,
            CONF_GRID_LOSS_MODE: GRID_LOSS_MODE_THRESHOLD,
        }
    )

    assert result["type"] == "form"
    assert result["errors"]["base"] == "missing_battery_soc_sensor"


@pytest.mark.asyncio
async def test_options_flow_persists_battery_soc_in_threshold_mode():
    entry = SimpleNamespace(data={}, options={})
    flow = PowerOrchestratorOptionsFlow(entry)

    created = await flow.async_step_init(
        {
            CONF_LOAD_SENSOR: "sensor.load",
            "max_load": 5000,
            CONF_GRID_LOSS_MODE: GRID_LOSS_MODE_THRESHOLD,
            CONF_BATTERY_SOC: "sensor.battery_soc",
        }
    )

    assert created["type"] == "create_entry"
    assert created["data"][CONF_BATTERY_SOC] == "sensor.battery_soc"


@pytest.mark.asyncio
async def test_setup_applies_options_restored_mode_and_update_listener():
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.bus.async_listen = MagicMock(return_value="unsubscribe")
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={
            CONF_LOAD_SENSOR: "sensor.old_load",
            "devices": [],
            "solar_forecast_entry": None,
            CONF_GRID_LOSS_MODE: GRID_LOSS_MODE_SENSOR,
        },
        options={CONF_LOAD_SENSOR: "sensor.new_load", "max_load": 4200},
        async_on_unload=MagicMock(),
        add_update_listener=MagicMock(return_value="update-unsubscribe"),
    )
    runtime_store = MagicMock()
    runtime_store.async_load = AsyncMock()
    runtime_store.restore_mode.return_value = MODE_OFF
    runtime_store.restore_execution_mode.return_value = "live"
    coordinator = MagicMock()
    coordinator.async_config_entry_first_refresh = AsyncMock()

    with (
        patch("power_orchestrator.Store"),
        patch("power_orchestrator.RuntimeStore", return_value=runtime_store),
        patch("power_orchestrator.PowerOrchestratorCoordinator", return_value=coordinator) as factory,
        patch("power_orchestrator._register_services", new=AsyncMock()),
    ):
        assert await async_setup_entry(hass, entry) is True

    assert factory.call_args.kwargs["load_sensor"] == "sensor.new_load"
    assert factory.call_args.kwargs["max_load"] == 4200
    assert factory.call_args.kwargs["execution_mode"] == "live"
    assert coordinator.mode == MODE_OFF
    entry.add_update_listener.assert_called_once()
    assert entry.async_on_unload.call_count >= 1


@pytest.mark.asyncio
async def test_setup_defaults_new_entry_to_off_before_first_refresh():
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.bus.async_listen = MagicMock(return_value="unsubscribe")
    entry = SimpleNamespace(
        entry_id="entry-new",
        data={
            CONF_LOAD_SENSOR: "sensor.load",
            "devices": [],
            "solar_forecast_entry": None,
            CONF_GRID_LOSS_MODE: GRID_LOSS_MODE_SENSOR,
        },
        options={},
        async_on_unload=MagicMock(),
        add_update_listener=MagicMock(return_value="update-unsubscribe"),
    )
    runtime_store = MagicMock()
    runtime_store.async_load = AsyncMock()
    runtime_store.restore_mode.return_value = None
    coordinator = MagicMock()
    coordinator.async_config_entry_first_refresh = AsyncMock()

    with (
        patch("power_orchestrator.Store"),
        patch("power_orchestrator.RuntimeStore", return_value=runtime_store),
        patch("power_orchestrator.PowerOrchestratorCoordinator", return_value=coordinator),
        patch("power_orchestrator._register_services", new=AsyncMock()),
    ):
        assert await async_setup_entry(hass, entry) is True

    assert coordinator.mode == MODE_OFF
    coordinator.async_config_entry_first_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_setup_coalesces_relevant_state_events_and_registers_cleanup():
    hass = MagicMock()
    hass.data = {}
    hass.async_create_task = None
    hass.config_entries.async_entries.return_value = []
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.config_entries.async_reload = AsyncMock()
    hass.bus.async_listen = MagicMock(return_value="unsubscribe")
    entry = SimpleNamespace(
        entry_id="entry-events",
        data={
            CONF_LOAD_SENSOR: "sensor.load",
            "grid_loss_mode": GRID_LOSS_MODE_SENSOR,
            "grid_loss_sensor": "binary_sensor.grid",
            "devices": [
                {
                    "device_id": "d1",
                    "name": "Device",
                    "entity": "switch.d1",
                    "expected_power": 1000,
                    "power_sensor": "sensor.d1_power",
                }
            ],
        },
        options={},
        async_on_unload=MagicMock(),
        add_update_listener=MagicMock(return_value="update-unsubscribe"),
    )
    runtime_store = MagicMock()
    runtime_store.async_load = AsyncMock()
    runtime_store.restore_mode.return_value = None
    runtime_store.restore_execution_mode.return_value = None
    runtime_store.restore_device_runtime.return_value = (set(), set())
    runtime_store.restore_fault_reasons.return_value = {}
    runtime_store.restore_fault_notification_state.return_value = ({}, {})
    runtime_store.unresolved_actions.return_value = []
    runtime_store.action_journal_invalid = False
    coordinator = MagicMock()
    coordinator.async_config_entry_first_refresh = AsyncMock()
    coordinator.async_force_evaluate = AsyncMock()
    coordinator.async_add_listener = MagicMock(return_value=None)

    with (
        patch("power_orchestrator.Store"),
        patch("power_orchestrator.RuntimeStore", return_value=runtime_store),
        patch("power_orchestrator.PowerOrchestratorCoordinator", return_value=coordinator),
        patch("power_orchestrator._register_services", new=AsyncMock()),
    ):
        assert await async_setup_entry(hass, entry) is True

    state_listener = hass.bus.async_listen.call_args.args[1]
    await state_listener(SimpleNamespace(data={"entity_id": "sensor.unwatched"}))
    coordinator.async_force_evaluate.assert_not_awaited()

    relevant = SimpleNamespace(data={"entity_id": "sensor.load"})
    await state_listener(relevant)
    await state_listener(relevant)
    await asyncio.sleep(0)
    assert coordinator.async_force_evaluate.await_count == 1

    cleanup_callbacks = [
        call.args[0]
        for call in entry.async_on_unload.call_args_list
        if callable(call.args[0])
    ]
    assert any(
        getattr(callback, "__name__", "") == "_cancel_state_worker"
        for callback in cleanup_callbacks
    )
    update_listener = entry.add_update_listener.call_args.args[0]
    await update_listener(hass, entry)
    hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)


@pytest.mark.asyncio
async def test_setup_forward_failure_cleans_runtime_and_shuts_down():
    hass = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock(
        side_effect=RuntimeError("platform setup failed")
    )
    hass.bus.async_listen = MagicMock(return_value="unsubscribe")
    entry = SimpleNamespace(
        entry_id="entry-forward-failure",
        data={
            CONF_LOAD_SENSOR: "sensor.load",
            "devices": [],
            "solar_forecast_entry": None,
            CONF_GRID_LOSS_MODE: GRID_LOSS_MODE_SENSOR,
        },
        options={},
        async_on_unload=MagicMock(),
        add_update_listener=MagicMock(return_value="update-unsubscribe"),
    )
    runtime_store = MagicMock()
    runtime_store.async_load = AsyncMock()
    runtime_store.restore_mode.return_value = None
    runtime_store.restore_execution_mode.return_value = None
    coordinator = MagicMock()
    coordinator.async_config_entry_first_refresh = AsyncMock()
    coordinator.async_shutdown = AsyncMock()

    with (
        patch("power_orchestrator.Store"),
        patch("power_orchestrator.RuntimeStore", return_value=runtime_store),
        patch("power_orchestrator.PowerOrchestratorCoordinator", return_value=coordinator),
        patch("power_orchestrator._register_services", new=AsyncMock()),
    ):
        with pytest.raises(RuntimeError, match="platform setup failed"):
            await async_setup_entry(hass, entry)

    assert entry.runtime_data is None
    coordinator.async_shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_singleton_setup_is_arbitrated_before_first_await():
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_entries.return_value = []
    entry_one = SimpleNamespace(entry_id="entry-1", runtime_data=None)
    entry_two = SimpleNamespace(entry_id="entry-2", runtime_data=None)
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_setup(_hass, _entry):
        started.set()
        await release.wait()
        return True

    with patch("power_orchestrator._async_setup_entry_impl", side_effect=blocked_setup):
        first = asyncio.create_task(async_setup_entry(hass, entry_one))
        await started.wait()
        assert await async_setup_entry(hass, entry_two) is False
        release.set()
        assert await first is True

    assert hass.data["power_orchestrator_lifecycle"]["reservations"] == set()


@pytest.mark.asyncio
async def test_setup_first_refresh_failure_shuts_down_and_unregisters_services():
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = []
    hass.services.has_service.return_value = True
    hass.services.async_remove = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    entry = SimpleNamespace(
        entry_id="entry-refresh-failure",
        data={
            CONF_LOAD_SENSOR: "sensor.load",
            "devices": [],
            "solar_forecast_entry": None,
            CONF_GRID_LOSS_MODE: GRID_LOSS_MODE_SENSOR,
        },
        options={},
        async_on_unload=MagicMock(),
        add_update_listener=MagicMock(return_value="update-unsubscribe"),
    )
    runtime_store = MagicMock()
    runtime_store.async_load = AsyncMock()
    runtime_store.restore_mode.return_value = None
    runtime_store.restore_execution_mode.return_value = None
    coordinator = MagicMock()
    coordinator.async_config_entry_first_refresh = AsyncMock(
        side_effect=RuntimeError("first refresh failed")
    )
    coordinator.async_shutdown = AsyncMock()

    with (
        patch("power_orchestrator.Store"),
        patch("power_orchestrator.RuntimeStore", return_value=runtime_store),
        patch("power_orchestrator.PowerOrchestratorCoordinator", return_value=coordinator),
        patch("power_orchestrator._register_services", new=AsyncMock()),
    ):
        with pytest.raises(RuntimeError, match="first refresh failed"):
            await async_setup_entry(hass, entry)

    coordinator.async_shutdown.assert_awaited_once()
    assert hass.services.async_remove.call_count == 6


@pytest.mark.asyncio
async def test_setup_sanitizes_corrupt_legacy_config_fail_closed():
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.bus.async_listen = MagicMock(return_value="unsubscribe")
    entry = SimpleNamespace(
        entry_id="entry-corrupt",
        data={
            CONF_LOAD_SENSOR: "sensor.load",
            "max_load": "nan",
            "averaging_period": "inf",
            "safety_reserve": "nan",
            "hysteresis": -1,
            "grid_loss_mode": "invalid-mode",
            "grid_loss_sensor": "switch.not_a_binary_sensor",
            "devices": [
                {
                    "device_id": "bad",
                    "entity": "sensor.not_a_control",
                    "expected_power": 2000,
                },
                {
                    "device_id": "zero",
                    "entity": "switch.zero_power",
                    "expected_power": 0,
                },
            ],
        },
        options={},
        async_on_unload=MagicMock(),
        add_update_listener=MagicMock(return_value="update-unsubscribe"),
    )
    runtime_store = MagicMock()
    runtime_store.async_load = AsyncMock()
    runtime_store.restore_mode.return_value = None
    coordinator = MagicMock()
    coordinator.async_config_entry_first_refresh = AsyncMock()

    with (
        patch("power_orchestrator.Store"),
        patch("power_orchestrator.RuntimeStore", return_value=runtime_store),
        patch(
            "power_orchestrator.PowerOrchestratorCoordinator",
            return_value=coordinator,
        ) as factory,
        patch("power_orchestrator._register_services", new=AsyncMock()),
    ):
        assert await async_setup_entry(hass, entry) is True

    kwargs = factory.call_args.kwargs
    assert kwargs["max_load"] == 0
    assert kwargs["safety_reserve"] == 0
    assert kwargs["hysteresis"] == 0
    assert kwargs["grid_loss_mode"] == GRID_LOSS_MODE_SENSOR
    assert kwargs["grid_loss_sensor"] is None
    assert kwargs["model"].all_devices() == []


@pytest.mark.asyncio
async def test_setup_refuses_second_singleton_entry():
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [
        SimpleNamespace(runtime_data=SimpleNamespace(coordinator=MagicMock()))
    ]
    entry = SimpleNamespace(entry_id="second", data={}, options={})

    assert await async_setup_entry(hass, entry) is False


@pytest.mark.asyncio
async def test_global_services_refuse_multiple_loaded_entries():
    hass = MagicMock()
    coordinator_one = MagicMock()
    coordinator_one.async_force_evaluate = AsyncMock()
    coordinator_one.async_set_mode = AsyncMock()
    coordinator_two = MagicMock()
    coordinator_two.async_force_evaluate = AsyncMock()
    coordinator_two.async_set_mode = AsyncMock()
    hass.config_entries.async_entries.return_value = [
        SimpleNamespace(runtime_data=SimpleNamespace(coordinator=coordinator_one)),
        SimpleNamespace(runtime_data=SimpleNamespace(coordinator=coordinator_two)),
    ]
    hass.services.has_service.return_value = False
    handlers = {}

    def register(domain, service, handler, **kwargs):
        handlers[service] = handler

    hass.services.async_register.side_effect = register
    await _register_services(hass)

    with pytest.raises(HomeAssistantError):
        await handlers["force_evaluate"](SimpleNamespace(data={}))
    with pytest.raises(ServiceValidationError):
        await handlers["set_mode"](SimpleNamespace(data={}))
    with pytest.raises(ServiceValidationError):
        await handlers["set_mode"](SimpleNamespace(data={"mode": "invalid"}))
    with pytest.raises(HomeAssistantError):
        await handlers["set_mode"](SimpleNamespace(data={"mode": MODE_OFF}))
    coordinator_one.async_force_evaluate.assert_not_awaited()
    coordinator_two.async_force_evaluate.assert_not_awaited()
    coordinator_one.async_set_mode.assert_not_awaited()
    coordinator_two.async_set_mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_global_services_route_singleton_entry():
    hass = MagicMock()
    coordinator = MagicMock()
    coordinator.async_force_evaluate = AsyncMock()
    coordinator.async_set_mode = AsyncMock()
    coordinator.async_request_start = AsyncMock(return_value=True)
    coordinator.async_request_stop = AsyncMock(return_value=True)
    coordinator.async_clear_quarantine = AsyncMock(return_value=True)
    hass.config_entries.async_entries.return_value = [
        SimpleNamespace(runtime_data=SimpleNamespace(coordinator=coordinator))
    ]
    hass.services.has_service.return_value = False
    handlers = {}
    registrations = {}

    def register(domain, service, handler, **kwargs):
        handlers[service] = handler
        registrations[service] = kwargs

    hass.services.async_register.side_effect = register
    await _register_services(hass)
    assert set(registrations) == {
        "force_evaluate",
        "set_mode",
        "request_start",
        "request_stop",
        "clear_quarantine",
        "set_execution_mode",
    }
    assert all("schema" in kwargs for kwargs in registrations.values())
    assert all(kwargs["supports_response"] is not None for kwargs in registrations.values())

    await handlers["force_evaluate"](SimpleNamespace(data={}))
    await handlers["set_mode"](SimpleNamespace(data={"mode": MODE_OFF}))
    call = SimpleNamespace(
        data={"device_id": "d1", "source": "dashboard"},
        context=SimpleNamespace(id="ctx-1", user_id="user-1"),
    )
    start_result = await handlers["request_start"](call)
    stop_result = await handlers["request_stop"](call)
    clear_result = await handlers["clear_quarantine"](call)
    assert start_result["accepted"] is True
    assert start_result["actor_id"] == "user-1"
    assert start_result["context_id"] == "ctx-1"
    assert stop_result["action"] == "request_stop"
    assert clear_result["action"] == "clear_quarantine"
    coordinator.async_request_start.assert_awaited_once_with(
        "d1", source="dashboard", actor_id="user-1", context_id="ctx-1"
    )
    coordinator.async_request_stop.assert_awaited_once_with(
        "d1", source="dashboard", actor_id="user-1", context_id="ctx-1"
    )
    coordinator.async_clear_quarantine.assert_awaited_once_with(
        "d1", source="dashboard", actor_id="user-1", context_id="ctx-1"
    )
    coordinator.async_force_evaluate.assert_awaited_once()
    coordinator.async_set_mode.assert_awaited_once_with(MODE_OFF)


@pytest.mark.asyncio
async def test_global_services_translate_handler_failures_and_normalize_context():
    hass = MagicMock()
    coordinator = MagicMock()
    coordinator.async_force_evaluate = AsyncMock()
    coordinator.async_set_mode = AsyncMock()
    coordinator.async_request_start = AsyncMock(return_value=False)
    coordinator.async_request_stop = AsyncMock(return_value=False)
    coordinator.async_clear_quarantine = AsyncMock(return_value=False)
    coordinator.async_set_execution_mode = AsyncMock()
    hass.config_entries.async_entries.return_value = [
        SimpleNamespace(runtime_data=SimpleNamespace(coordinator=coordinator))
    ]
    hass.services.has_service.return_value = False
    handlers = {}
    hass.services.async_register.side_effect = lambda domain, service, handler, **kwargs: handlers.update(
        {service: handler}
    )
    await _register_services(hass)

    coordinator.async_force_evaluate.side_effect = RuntimeError("evaluation")
    with pytest.raises(HomeAssistantError):
        await handlers["force_evaluate"](SimpleNamespace(data={}))
    coordinator.async_force_evaluate.side_effect = None

    coordinator.async_set_mode.side_effect = ValueError("mode rejected")
    with pytest.raises(ServiceValidationError):
        await handlers["set_mode"](SimpleNamespace(data={"mode": MODE_OFF}))
    coordinator.async_set_mode.side_effect = RuntimeError("mode failed")
    with pytest.raises(HomeAssistantError):
        await handlers["set_mode"](SimpleNamespace(data={"mode": MODE_OFF}))
    coordinator.async_set_mode.side_effect = None

    with pytest.raises(ServiceValidationError):
        await handlers["request_start"](SimpleNamespace(data={"device_id": "d1", "source": " "}))
    default_result = await handlers["request_start"](
        SimpleNamespace(
            data={"device_id": "d1"},
            context=SimpleNamespace(user_id="", id=""),
        )
    )
    assert default_result["actor_id"] == "system"
    assert default_result["context_id"] is None
    coordinator.async_request_start.side_effect = ValueError("start rejected")
    with pytest.raises(ServiceValidationError):
        await handlers["request_start"](SimpleNamespace(data={"device_id": "d1"}))
    coordinator.async_request_start.side_effect = RuntimeError("start failed")
    with pytest.raises(HomeAssistantError):
        await handlers["request_start"](SimpleNamespace(data={"device_id": "d1"}))
    coordinator.async_request_start.side_effect = None

    with pytest.raises(ServiceValidationError):
        await handlers["request_stop"](SimpleNamespace(data={}))
    coordinator.async_request_stop.side_effect = ValueError("stop rejected")
    with pytest.raises(ServiceValidationError):
        await handlers["request_stop"](SimpleNamespace(data={"device_id": "d1"}))
    coordinator.async_request_stop.side_effect = RuntimeError("stop failed")
    with pytest.raises(HomeAssistantError):
        await handlers["request_stop"](SimpleNamespace(data={"device_id": "d1"}))
    coordinator.async_request_stop.side_effect = None

    with pytest.raises(ServiceValidationError):
        await handlers["clear_quarantine"](SimpleNamespace(data={}))
    coordinator.async_clear_quarantine.side_effect = ValueError("clear rejected")
    with pytest.raises(ServiceValidationError):
        await handlers["clear_quarantine"](SimpleNamespace(data={"device_id": "d1"}))
    coordinator.async_clear_quarantine.side_effect = RuntimeError("clear failed")
    with pytest.raises(HomeAssistantError):
        await handlers["clear_quarantine"](SimpleNamespace(data={"device_id": "d1"}))
    coordinator.async_clear_quarantine.side_effect = None

    with pytest.raises(ServiceValidationError):
        await handlers["set_execution_mode"](SimpleNamespace(data={"execution_mode": "armed"}))
    coordinator.async_set_execution_mode.side_effect = ValueError("execution rejected")
    with pytest.raises(ServiceValidationError):
        await handlers["set_execution_mode"](
            SimpleNamespace(data={"execution_mode": "live", "confirm_live": True})
        )
    coordinator.async_set_execution_mode.side_effect = RuntimeError("execution failed")
    with pytest.raises(HomeAssistantError):
        await handlers["set_execution_mode"](
            SimpleNamespace(data={"execution_mode": "observe"})
        )
    coordinator.async_set_execution_mode.side_effect = None
    result = await handlers["set_execution_mode"](
        SimpleNamespace(data={"execution_mode": "observe"})
    )
    assert result["accepted"] is True


@pytest.mark.asyncio
async def test_global_services_skip_registration_when_already_present():
    hass = MagicMock()
    hass.services.has_service.return_value = True
    await _register_services(hass)
    hass.services.async_register.assert_not_called()


@pytest.mark.asyncio
async def test_global_services_ignore_unloaded_runtime_and_fail_closed_without_loaded_target():
    hass = MagicMock()
    coordinator = MagicMock()
    coordinator.async_force_evaluate = AsyncMock()
    hass.config_entries.async_entries.return_value = [
        SimpleNamespace(
            state=SimpleNamespace(value="not_loaded"),
            runtime_data=SimpleNamespace(coordinator=coordinator),
        )
    ]
    hass.services.has_service.return_value = False
    handlers = {}
    hass.services.async_register.side_effect = lambda domain, service, handler, **kwargs: handlers.update(
        {service: handler}
    )

    await _register_services(hass)

    with pytest.raises(HomeAssistantError):
        await handlers["force_evaluate"](SimpleNamespace(data={}))
    coordinator.async_force_evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_last_unload_unregisters_all_guarded_services():
    hass = MagicMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    hass.config_entries.async_entries.return_value = []
    hass.services.has_service.return_value = True
    hass.services.async_remove = MagicMock()
    store = MagicMock()
    store.async_save = AsyncMock()
    coordinator = MagicMock()
    coordinator.async_shutdown = AsyncMock()
    entry = SimpleNamespace(
        entry_id="entry-last",
        runtime_data=SimpleNamespace(store=store, coordinator=coordinator),
    )

    assert await async_unload_entry(hass, entry) is True

    assert hass.services.async_remove.call_count == 6
    assert {
        call.args[1] for call in hass.services.async_remove.call_args_list
    } == {
        "force_evaluate",
        "set_mode",
        "request_start",
        "request_stop",
        "clear_quarantine",
        "set_execution_mode",
    }


@pytest.mark.asyncio
async def test_unload_keeps_services_when_another_runtime_is_loaded():
    hass = MagicMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    hass.config_entries.async_entries.return_value = [
        SimpleNamespace(state="loaded", runtime_data=SimpleNamespace())
    ]
    hass.services.has_service.return_value = True
    hass.services.async_remove = MagicMock()
    store = MagicMock()
    store.async_save = AsyncMock()
    coordinator = MagicMock()
    coordinator.async_shutdown = AsyncMock()
    entry = SimpleNamespace(
        entry_id="entry-one-of-two",
        runtime_data=SimpleNamespace(store=store, coordinator=coordinator),
    )

    assert await async_unload_entry(hass, entry) is True

    hass.services.async_remove.assert_not_called()


@pytest.mark.asyncio
async def test_unload_snapshot_failure_still_shuts_down_and_removes_runtime():
    hass = MagicMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    hass.config_entries.async_entries.return_value = []
    hass.services.has_service.return_value = True
    hass.services.async_remove = MagicMock()
    store = MagicMock()
    store.async_save = AsyncMock()
    coordinator = MagicMock()
    coordinator._save_runtime_snapshot = MagicMock(
        side_effect=RuntimeError("snapshot failure")
    )
    coordinator.async_shutdown = AsyncMock()
    entry = SimpleNamespace(
        entry_id="entry-snapshot-failure",
        runtime_data=SimpleNamespace(store=store, coordinator=coordinator),
    )

    assert await async_unload_entry(hass, entry) is True

    coordinator.async_shutdown.assert_awaited_once()
    assert entry.runtime_data is None
    assert hass.services.async_remove.call_count == 6


@pytest.mark.asyncio
async def test_unload_persists_state_and_shuts_down_coordinator():
    hass = MagicMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    store = MagicMock()
    store.async_save = AsyncMock()
    coordinator = MagicMock()
    coordinator.async_shutdown = AsyncMock()
    entry = SimpleNamespace(
        entry_id="entry-1",
        runtime_data=SimpleNamespace(store=store, coordinator=coordinator),
    )

    assert await async_unload_entry(hass, entry) is True
    store.async_save.assert_awaited_once()
    coordinator.async_shutdown.assert_awaited_once()
    assert entry.runtime_data is None


@pytest.mark.asyncio
async def test_unload_cleans_runtime_when_persistence_fails():
    hass = MagicMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    store = MagicMock()
    store.async_save = AsyncMock(side_effect=OSError("disk full"))
    coordinator = MagicMock()
    coordinator.async_shutdown = AsyncMock()
    entry = SimpleNamespace(
        entry_id="entry-save-failure",
        runtime_data=SimpleNamespace(store=store, coordinator=coordinator),
    )

    assert await async_unload_entry(hass, entry) is True
    coordinator.async_shutdown.assert_awaited_once()
    assert entry.runtime_data is None


@pytest.mark.asyncio
async def test_internal_setup_guard_rejects_loaded_runtime_before_any_side_effect():
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [
        SimpleNamespace(runtime_data=SimpleNamespace(coordinator=MagicMock()))
    ]
    entry = SimpleNamespace(entry_id="duplicate", runtime_data=None)

    assert await _async_setup_entry_impl(hass, entry) is False
    hass.config_entries.async_forward_entry_setups.assert_not_called()


@pytest.mark.asyncio
async def test_setup_normalizes_non_mapping_and_logical_actuator_inputs():
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_entries.return_value = []
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.bus.async_listen = MagicMock(return_value="unsubscribe")
    entry = SimpleNamespace(
        entry_id="entry-actuators",
        data={
            CONF_LOAD_SENSOR: "sensor.load",
            "grid_loss_mode": GRID_LOSS_MODE_SENSOR,
            CONF_DEVICES: [
                "not-a-device-mapping",
                {
                    "device_id": "non-list-actuators",
                    "name": "Device A",
                    "entity": "switch.device_a",
                    "actuators": 123,
                    "expected_power": 1000,
                },
                {
                    "device_id": "string-actuator",
                    "name": "Device B",
                    "entity": "switch.device_b",
                    "actuators": "switch.device_b_aux",
                    "expected_power": 1000,
                },
                {
                    "device_id": "duplicate-actuator",
                    "name": "Invalid duplicate",
                    "entity": "switch.device_c",
                    "actuators": ["switch.device_c"],
                    "expected_power": 1000,
                },
                {
                    "device_id": "invalid-actuator",
                    "name": "Invalid domain",
                    "entity": "switch.device_d",
                    "actuators": ["sensor.device_d_power"],
                    "expected_power": 1000,
                },
            ],
        },
        options={},
        async_on_unload=MagicMock(),
        add_update_listener=MagicMock(return_value="update-unsubscribe"),
    )
    runtime_store = MagicMock()
    runtime_store.async_load = AsyncMock()
    runtime_store.restore_mode.return_value = None
    runtime_store.restore_execution_mode.return_value = None
    runtime_store.restore_device_runtime.return_value = (set(), set())
    runtime_store.restore_fault_reasons.return_value = {}
    runtime_store.restore_fault_notification_state.return_value = ({}, {})
    runtime_store.unresolved_actions.return_value = []
    coordinator = MagicMock()
    coordinator.async_config_entry_first_refresh = AsyncMock()

    with (
        patch("power_orchestrator.Store"),
        patch("power_orchestrator.RuntimeStore", return_value=runtime_store),
        patch("power_orchestrator.PowerOrchestratorCoordinator", return_value=coordinator) as factory,
        patch("power_orchestrator._register_services", new=AsyncMock()),
    ):
        assert await async_setup_entry(hass, entry) is True

    model = factory.call_args.kwargs["model"]
    assert {device.device_id for device in model.all_devices()} == {
        "non-list-actuators",
        "string-actuator",
    }
    assert model.get_device("non-list-actuators").actuator_entity_ids == ()
    assert model.get_device("string-actuator").actuator_entity_ids == (
        "switch.device_b_aux",
    )


@pytest.mark.asyncio
async def test_unload_false_preserves_runtime_and_listener():
    hass = MagicMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=False)
    hass.config_entries.async_entries.return_value = []
    listener_remove = MagicMock()
    store = MagicMock()
    store.async_save = AsyncMock()
    coordinator = MagicMock()
    coordinator.async_shutdown = AsyncMock()
    runtime = SimpleNamespace(
        store=store,
        coordinator=coordinator,
        repair_listener_remove=listener_remove,
    )
    entry = SimpleNamespace(entry_id="entry-unload-false", runtime_data=runtime)

    assert await async_unload_entry(hass, entry) is False
    assert entry.runtime_data is runtime
    listener_remove.assert_not_called()
    store.async_save.assert_not_awaited()
    coordinator.async_shutdown.assert_not_awaited()


@pytest.mark.asyncio
async def test_unload_removes_repair_listener_and_uses_policy_fallback():
    hass = MagicMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    hass.config_entries.async_entries.return_value = []
    listener_remove = MagicMock()
    store = MagicMock()
    store.async_save = AsyncMock()
    store.save_policy_runtime = MagicMock()
    coordinator = SimpleNamespace(
        _policy_engine=object(),
        async_shutdown=AsyncMock(),
    )
    entry = SimpleNamespace(
        entry_id="entry-fallback-save",
        runtime_data=SimpleNamespace(
            store=store,
            coordinator=coordinator,
            repair_listener_remove=listener_remove,
        ),
    )

    assert await async_unload_entry(hass, entry) is True
    listener_remove.assert_called_once()
    store.save_policy_runtime.assert_called_once_with(coordinator._policy_engine)
    store.async_save.assert_awaited_once()
    coordinator.async_shutdown.assert_awaited_once()
    assert entry.runtime_data is None


@pytest.mark.asyncio
async def test_platform_setup_requires_runtime_and_entity_projections_are_typed():
    with pytest.raises(RuntimeError, match="runtime data is unavailable"):
        await async_setup_sensor(MagicMock(), SimpleNamespace(runtime_data=None), MagicMock())
    with pytest.raises(RuntimeError, match="runtime data is unavailable"):
        await async_setup_binary(MagicMock(), SimpleNamespace(runtime_data=None), MagicMock())

    coordinator = SimpleNamespace(
        hass=MagicMock(),
        status="idle",
        mode=MODE_OFF,
        grid_ok=True,
        grid_safety_source_configured=True,
        load_sensor_valid=False,
        load_sensor_reason="missing",
        startup_safe=True,
        pending_start_power=0.0,
        last_action="none",
        execution_mode="observe",
        current_load=None,
        data={},
    )
    entry = SimpleNamespace(entry_id="entry-entities")
    status = PowerOrchestratorStatusSensor(coordinator, entry)
    last_action = PowerOrchestratorLastActionSensor(coordinator, entry)
    fault = PowerOrchestratorFaultSensor(coordinator, entry)
    journal = PowerOrchestratorActionJournalHealthySensor(coordinator, entry)

    assert status._coordinator is coordinator
    assert status.native_value == "idle"
    assert last_action.native_value == "none"
    assert fault.available is True
    assert journal.extra_state_attributes == {
        "unresolved_count": 0,
        "invalid": False,
        "persistence_blocked": False,
    }
