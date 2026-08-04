"""Safety boundaries for stop-only execution."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from power_orchestrator.const import (
    EVENT_ACTION,
    MODE_AUTO,
    MODE_OFF,
    STATUS_SAFETY_BLOCKED,
)
from power_orchestrator.coordinator import PowerOrchestratorCoordinator
from power_orchestrator.policy import PolicyConfig
from power_orchestrator.power_model import ManagedDevice, PowerModel


def _state(value: str, *, age: float = 0, unit: str = "W") -> SimpleNamespace:
    timestamp = datetime.now(timezone.utc) - timedelta(seconds=age)
    return SimpleNamespace(
        state=value,
        attributes={"unit_of_measurement": unit},
        last_reported=timestamp,
        last_updated=timestamp,
    )


def _coordinator(*, execution_mode: str = "live") -> PowerOrchestratorCoordinator:
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.bus.async_fire = MagicMock()
    store = MagicMock()
    store.async_save = AsyncMock()
    store.audit_history.return_value = []
    store.unresolved_actions.return_value = []
    model = PowerModel()
    model.add_device(ManagedDevice("d1", "Device 1", "switch.d1", expected_power=1000))
    coordinator = PowerOrchestratorCoordinator(
        hass=hass,
        model=model,
        store=store,
        load_sensor="sensor.load",
        max_load=5000,
        averaging_period=10,
        safety_reserve=200,
        hysteresis=100,
        pause_period=60,
        grid_loss_mode="grid_loss_sensor",
        grid_loss_sensor="binary_sensor.grid",
        battery_threshold=None,
        battery_soc_sensor=None,
        execution_mode=execution_mode,
    )
    coordinator.mode = MODE_AUTO
    return coordinator


def test_structured_event_has_bounded_schema() -> None:
    coordinator = _coordinator(execution_mode="observe")
    coordinator._emit_event(EVENT_ACTION, {"action": "stop", "source": "test"})
    event = coordinator.hass.bus.async_fire.call_args.args[1]
    assert event["schema_version"] == 1
    assert event["event_type"] == EVENT_ACTION
    assert event["entry_id"] == coordinator._entry_id
    assert event["execution_mode"] == "observe"


def test_stale_load_is_not_accepted() -> None:
    coordinator = _coordinator()
    coordinator.hass.states.get.side_effect = lambda entity_id: (
        _state("on") if entity_id == "binary_sensor.grid" else _state("1000", age=301)
    )
    assert coordinator._read_load_sensor() == 0.0
    assert coordinator.load_sensor_valid is False
    assert coordinator.load_sensor_reason == "unavailable_or_stale"


def test_wrong_load_unit_is_not_accepted() -> None:
    coordinator = _coordinator()
    coordinator.hass.states.get.return_value = _state("1000", unit="V")
    assert coordinator._read_load_sensor() == 0.0
    assert coordinator.load_sensor_reason == "unsupported_unit"


@pytest.mark.asyncio
async def test_unconfirmed_stop_latches_fault_and_never_claims_success() -> None:
    coordinator = _coordinator()
    device = coordinator._model.get_device("d1")
    assert device is not None
    device.is_on = True
    coordinator._confirm_device_state = AsyncMock(return_value=False)
    coordinator.hass.states.get.return_value = _state("on")

    assert await coordinator.async_request_stop("d1", source="test") is False
    assert device.is_on is None
    assert "d1" in coordinator._faulted
    assert coordinator.hass.services.async_call.await_args.args[1] == "turn_off"
    assert all(call.args[1] != "turn_on" for call in coordinator.hass.services.async_call.await_args_list)


@pytest.mark.asyncio
async def test_off_mode_persists_without_authorizing_commands() -> None:
    coordinator = _coordinator()
    coordinator.hass.states.get.side_effect = lambda entity_id: (
        _state("on")
        if entity_id == "binary_sensor.grid"
        else _state("1000")
        if entity_id == "sensor.load"
        else _state("off", unit="")
    )
    await coordinator.async_set_mode(MODE_OFF)
    assert coordinator.mode == MODE_OFF
    coordinator._store.async_save.assert_awaited_once()
    assert coordinator._store.set_mode.call_args.args == (MODE_OFF,)
    assert coordinator.physical_commands_allowed is False


def test_custom_policy_is_bounded_and_shedding_only() -> None:
    policy = PolicyConfig.from_mapping({"thresholds": [{"power_limit": 1000, "duration_s": 1}]})
    assert policy.thresholds[0].limit_w == 1000
    assert policy.thresholds[0].duration_s == 1
    assert not hasattr(policy, "forecast_entity")
