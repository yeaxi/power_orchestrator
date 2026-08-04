"""Entity and lifecycle tests for the stop-only integration."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from power_orchestrator import (
    _lifecycle_state,
    _loaded_runtimes,
    _safe_number,
    _valid_entity_id,
    async_migrate_entry,
)
from power_orchestrator.binary_sensor import (
    PowerOrchestratorActionJournalHealthySensor,
    PowerOrchestratorFaultSensor,
    PowerOrchestratorGridOkSensor,
    async_setup_entry as async_setup_binary,
)
from power_orchestrator.const import DOMAIN, MODE_OFF
from power_orchestrator.diagnostics import async_get_config_entry_diagnostics
from power_orchestrator.select import PowerOrchestratorModeSelect, async_setup_entry as async_setup_select
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


def test_lifecycle_and_input_helpers_repair_malformed_state() -> None:
    hass = SimpleNamespace(data="invalid")
    state = _lifecycle_state(hass)
    assert isinstance(hass.data, dict)
    assert isinstance(state, dict)
    assert _valid_entity_id("switch.load", frozenset({"switch"})) == "switch.load"
    assert _valid_entity_id("sensor.load", frozenset({"switch"})) is None
    assert _safe_number(True, default=3, minimum=0, maximum=10) == 3
    assert _safe_number("bad", default=3, minimum=0, maximum=10) == 3


def test_loaded_runtimes_reads_current_runtime_registry() -> None:
    runtime = SimpleNamespace(coordinator=object())
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    assert _loaded_runtimes(hass) == [runtime]
    assert _loaded_runtimes(SimpleNamespace(data={DOMAIN: {"entry": None}})) == []


@pytest.mark.asyncio
async def test_platform_setup_has_expected_stop_only_entities() -> None:
    coordinator = MagicMock()
    coordinator.hass = MagicMock()
    entry = SimpleNamespace(entry_id="entry-1", runtime_data=SimpleNamespace(coordinator=coordinator))
    add_sensor = MagicMock()
    await async_setup_sensor(MagicMock(), entry, add_sensor)
    assert len(add_sensor.call_args.args[0]) == 8
    add_binary = MagicMock()
    await async_setup_binary(MagicMock(), entry, add_binary)
    binary_entities = add_binary.call_args.args[0]
    assert len(binary_entities) == 3
    assert binary_entities[0]._attr_unique_id.endswith("_grid_ok")
    assert binary_entities[1]._attr_unique_id.endswith("_faulted")
    assert binary_entities[2]._attr_unique_id.endswith("_action_journal_healthy")


def test_diagnostic_entities_expose_fault_and_journal_state() -> None:
    coordinator = MagicMock()
    coordinator.data = {
        "faulted_devices": ["d1"],
        "fault_reasons": {"d1": "relay_readback_timeout"},
        "journal_persistence_blocked": False,
        "action_journal_invalid": False,
        "journal_unresolved_count": 0,
    }
    entry = SimpleNamespace(entry_id="entry-1")
    fault = PowerOrchestratorFaultSensor(coordinator, entry)
    journal = PowerOrchestratorActionJournalHealthySensor(coordinator, entry)
    assert fault.is_on is True
    assert fault.extra_state_attributes["device_ids"] == ["d1"]
    assert journal.is_on is True
    coordinator.data["journal_persistence_blocked"] = True
    assert journal.is_on is False


def test_execution_reason_and_operation_entities_do_not_expose_reenable_fields() -> None:
    coordinator = MagicMock()
    coordinator.execution_mode = "observe"
    coordinator.mode = MODE_OFF
    coordinator.reason_code = "observe_mode"
    coordinator.data = {
        "status": "observe",
        "policy_phase": "monitoring",
        "physical_commands_allowed": False,
        "journal_persistence_blocked": False,
        "action_journal_invalid": False,
        "last_operation_result": "observe_only",
        "last_action_id": "action-1",
        "last_operation_id": "operation-1",
        "journal_unresolved_count": 0,
        "audit_history": [],
    }
    entry = SimpleNamespace(entry_id="entry-1")
    execution = PowerOrchestratorExecutionModeSensor(coordinator, entry)
    reason = PowerOrchestratorReasonCodeSensor(coordinator, entry)
    operation = PowerOrchestratorLastOperationSensor(coordinator, entry)
    assert execution.native_value == "observe"
    assert reason.native_value == "observe_mode"
    assert operation.native_value == "observe_only"
    assert "pending_action_id" not in operation.extra_state_attributes


def test_invalid_load_numeric_entities_are_unavailable() -> None:
    coordinator = MagicMock()
    coordinator.load_sensor_valid = False
    coordinator.load_sensor_reason = "unsupported_unit"
    coordinator.current_load = None
    coordinator.average_load = None
    coordinator.available_capacity = None
    coordinator.status = "safety_blocked"
    coordinator.mode = "auto"
    coordinator.grid_ok = True
    coordinator.startup_safe = True
    coordinator.last_action = "blocked"
    entry = SimpleNamespace(entry_id="entry-1")
    for entity in (
        PowerOrchestratorCurrentLoadSensor(coordinator, entry),
        PowerOrchestratorAverageLoadSensor(coordinator, entry),
        PowerOrchestratorAvailableCapacitySensor(coordinator, entry),
    ):
        assert entity.available is False
        assert entity.native_value is None
    status_attributes = PowerOrchestratorStatusSensor(coordinator, entry).extra_state_attributes
    assert status_attributes["load_sensor_reason"] == "unsupported_unit"
    assert "automatic_reenable" not in status_attributes


def test_grid_ok_sensor_availability_tracks_source_configuration() -> None:
    coordinator = MagicMock()
    coordinator.grid_safety_source_configured = False
    coordinator.grid_ok = False
    entity = PowerOrchestratorGridOkSensor(coordinator, SimpleNamespace(entry_id="entry"))
    assert entity.available is False
    coordinator.grid_safety_source_configured = True
    assert entity.available is True


@pytest.mark.asyncio
async def test_mode_select_delegates_only_auto_or_off() -> None:
    coordinator = MagicMock()
    coordinator.mode = "auto"
    coordinator.async_set_mode = AsyncMock()
    entity = PowerOrchestratorModeSelect(coordinator, SimpleNamespace(entry_id="entry"))
    entity.async_write_ha_state = MagicMock()
    with pytest.raises(ValueError):
        await entity.async_select_option("invalid")
    await entity.async_select_option("off")
    coordinator.async_set_mode.assert_awaited_once_with("off")


@pytest.mark.asyncio
async def test_diagnostics_are_bounded_and_redacted() -> None:
    entry = SimpleNamespace(
        data={"api_key": "secret", "nested": {"latitude": 1.0}},
        options={"password": "secret"},
        runtime_data=SimpleNamespace(coordinator=SimpleNamespace(data={"status": "safety_blocked", "faulted_devices": ["d1"]})),
    )
    result = await async_get_config_entry_diagnostics(MagicMock(), entry)
    assert result["entry_data"]["api_key"] == "**REDACTED**"
    assert result["entry_data"]["nested"]["latitude"] == "**REDACTED**"
    assert result["options"]["password"] == "**REDACTED**"
    assert result["runtime"]["data"]["faulted_devices_count"] == 1
    assert "audit_history" not in result["runtime"]["data"]


@pytest.mark.asyncio
async def test_mode_select_setup_requires_runtime() -> None:
    with pytest.raises(RuntimeError):
        await async_setup_select(MagicMock(), SimpleNamespace(runtime_data=None), MagicMock())
