"""Safety boundaries for stop-only execution."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from power_orchestrator.const import (
    EVENT_ACTION,
    MODE_AUTO,
    MODE_OFF,
    STATUS_SAFETY_BLOCKED,
)
from power_orchestrator.coordinator import CoordinatorConfig, PowerOrchestratorCoordinator
from power_orchestrator.policy import PolicyConfig
from power_orchestrator.power_model import ManagedDevice, PowerModel


def _state(value: str, *, age: float = 0, updated_age: float | None = None, unit: str = "W") -> SimpleNamespace:
    timestamp = datetime.now(timezone.utc) - timedelta(seconds=age)
    updated_timestamp = datetime.now(timezone.utc) - timedelta(
        seconds=age if updated_age is None else updated_age
    )
    return SimpleNamespace(
        state=value,
        attributes={"unit_of_measurement": unit},
        last_reported=timestamp,
        last_updated=updated_timestamp,
    )


def _coordinator(
    *, execution_mode: str = "live", policy: PolicyConfig | None = None
) -> PowerOrchestratorCoordinator:
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
        config=CoordinatorConfig(
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
            policy=policy,
            execution_mode=execution_mode,
        ),
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


def test_numeric_load_ignores_timestamp_age_but_unavailable_state_blocks() -> None:
    coordinator = _coordinator()
    coordinator.hass.states.get.side_effect = lambda entity_id: (
        _state("on") if entity_id == "binary_sensor.grid" else _state("1000", updated_age=301)
    )
    assert coordinator._read_load_sensor() == 1000.0
    assert coordinator.load_sensor_valid is True
    assert coordinator.load_sensor_reason == "ok"
    coordinator.hass.states.get.side_effect = lambda entity_id: (
        _state("on") if entity_id == "binary_sensor.grid" else _state("unavailable")
    )
    assert coordinator._read_load_sensor() == 0.0
    assert coordinator.load_sensor_valid is False
    assert coordinator.load_sensor_reason == "unavailable"


@pytest.mark.asyncio
async def test_available_unchanged_aggregate_state_remains_usable_after_window() -> None:
    """A valid unchanged HA state is not aged out by the averaging window."""
    coordinator = _coordinator(execution_mode="observe")
    coordinator._refresh_device_states = AsyncMock()
    report = datetime.fromtimestamp(100, timezone.utc)
    grid_state = SimpleNamespace(
        state="on",
        attributes={},
        last_reported=report,
        last_updated=report,
    )
    load_state = SimpleNamespace(
        state="0",
        attributes={"unit_of_measurement": "W"},
        last_reported=report,
        last_updated=report,
    )
    coordinator.hass.states.get.side_effect = lambda entity_id: (
        grid_state if entity_id == "binary_sensor.grid" else load_state
    )
    clock = [1000.0]
    with patch("power_orchestrator.coordinator.time.time", side_effect=lambda: clock[0]):
        await coordinator._evaluate()
        clock[0] += coordinator._averaging_period + 1
        await coordinator._evaluate()
        assert coordinator.load_sensor_valid is True
        assert coordinator.current_load == 0.0
        assert coordinator.average_load == 0.0
        assert coordinator.status != STATUS_SAFETY_BLOCKED


def test_zero_measured_power_is_valid_when_numeric_even_if_old_report() -> None:
    coordinator = _coordinator()
    device = coordinator._model.get_device("d1")
    assert device is not None
    device.power_sensor_id = "sensor.d1_power"
    coordinator.hass.states.get.return_value = _state("0", age=301)

    coordinator._refresh_measured_power(device)

    assert device.measured_power == 0.0
    assert device.measured_power_valid is True
    assert device.measured_power_reason == "ok"


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
    assert "d1" in coordinator._faults.faulted
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


@pytest.mark.asyncio
async def test_auto_mode_promotes_physical_execution_to_live() -> None:
    coordinator = _coordinator(execution_mode="observe")

    await coordinator.async_set_mode(MODE_OFF)
    await coordinator.async_set_mode(MODE_AUTO)

    assert coordinator.mode == MODE_AUTO
    assert coordinator.execution_mode == "live"
    assert coordinator.physical_commands_allowed is True
    assert coordinator._store.set_execution_mode.call_args.args == ("live",)


@pytest.mark.asyncio
async def test_observe_execution_cannot_disable_auto_mode() -> None:
    coordinator = _coordinator(execution_mode="live")

    with pytest.raises(ValueError, match="auto mode requires live execution"):
        await coordinator.async_set_execution_mode("observe")

    assert coordinator.mode == MODE_AUTO
    assert coordinator.execution_mode == "live"
    assert coordinator.physical_commands_allowed is True


@pytest.mark.asyncio
async def test_entering_auto_claims_currently_on_configured_loads() -> None:
    coordinator = _coordinator(execution_mode="observe")
    device = coordinator._model.get_device("d1")
    assert device is not None
    coordinator.hass.states.get.side_effect = lambda entity_id: (
        _state("on")
        if entity_id in {"binary_sensor.grid", "switch.d1"}
        else _state("1000")
    )

    await coordinator.async_set_mode(MODE_OFF)
    await coordinator.async_set_mode(MODE_AUTO)

    assert device.ownership.value == "planner"
    assert coordinator.hass.services.async_call.await_count == 0


@pytest.mark.asyncio
async def test_auto_mode_executes_guarded_stop_on_validated_overload() -> None:
    policy = PolicyConfig.from_mapping(
        {"thresholds": [{"power_limit": 1000, "duration_s": 0}]}
    )
    coordinator = _coordinator(execution_mode="observe", policy=policy)
    coordinator.hass.states.get.side_effect = lambda entity_id: (
        _state("on")
        if entity_id in {"binary_sensor.grid", "switch.d1"}
        else _state("off")
        if entity_id == "switch.d2"
        else _state("2000")
    )
    coordinator._confirm_device_state = AsyncMock(return_value=True)

    await coordinator.async_set_mode(MODE_OFF)
    await coordinator.async_set_mode(MODE_AUTO)

    coordinator.hass.services.async_call.assert_awaited_once_with(
        "switch", "turn_off", {"entity_id": "switch.d1"}, blocking=True
    )
    assert all(
        call.args[1] != "turn_on"
        for call in coordinator.hass.services.async_call.await_args_list
    )


def test_custom_policy_is_bounded_and_shedding_only() -> None:
    policy = PolicyConfig.from_mapping({"thresholds": [{"power_limit": 1000, "duration_s": 1}]})
    assert policy.thresholds[0].limit_w == 1000
    assert policy.thresholds[0].duration_s == 1
    assert not hasattr(policy, "forecast_entity")
