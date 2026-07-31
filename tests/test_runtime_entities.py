"""Runtime, entity lifecycle, options, and setup contract tests."""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components"))

from power_orchestrator import (
    HomeAssistantError,
    _register_services,
    ServiceValidationError,
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
from power_orchestrator.select import PowerOrchestratorModeSelect
from power_orchestrator.sensor import (
    PowerOrchestratorAverageLoadSensor,
    PowerOrchestratorAvailableCapacitySensor,
    PowerOrchestratorCurrentLoadSensor,
    PowerOrchestratorExecutionModeSensor,
    PowerOrchestratorLastOperationSensor,
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
