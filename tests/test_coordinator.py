"""Coordinator behavior tests for stop-only load shedding."""

from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from power_orchestrator.const import (
    GRID_LOSS_MODE_SENSOR,
    MODE_AUTO,
    MODE_OFF,
    STATUS_GRID_LOSS,
    STATUS_LOAD_SHEDDING,
    STATUS_SAFETY_BLOCKED,
)
from power_orchestrator.coordinator import CoordinatorConfig, PowerOrchestratorCoordinator
from power_orchestrator.policy import Ownership, PolicyConfig
from power_orchestrator.power_model import ManagedDevice, PowerModel
from power_orchestrator.storage import RuntimeStore


def state(
    value: str, *, unit: str = "W", age: float = 0, updated_age: float | None = None
) -> SimpleNamespace:
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


def model() -> PowerModel:
    result = PowerModel()
    result.add_device(
        ManagedDevice(
            "d1", "Load 1", "switch.load_1", expected_power=1000, priority=1, shed_priority=1
        )
    )
    result.add_device(
        ManagedDevice(
            "d2", "Load 2", "switch.load_2", expected_power=2000, priority=2, shed_priority=2
        )
    )
    return result


class CopyingStoreBackend:
    """Small Store backend that makes persistence tests observe durable bytes."""

    def __init__(self) -> None:
        self.data: dict[str, object] | None = None
        self.events: list[str] = []
        self.save_calls = 0
        self.fail_on: set[int] = set()

    async def async_load(self) -> dict[str, object] | None:
        return copy.deepcopy(self.data)

    async def async_save(self, data: dict[str, object]) -> None:
        self.save_calls += 1
        self.events.append("save")
        if self.save_calls in self.fail_on:
            raise RuntimeError("synthetic persistence failure")
        self.data = copy.deepcopy(data)


def coordinator(
    *,
    hass=None,
    policy=None,
    execution_mode="live",
    grid_mode=GRID_LOSS_MODE_SENSOR,
    grid_sensor="binary_sensor.grid",
    battery_soc=None,
    battery_threshold=None,
    store=None,
):
    hass = hass or MagicMock()
    hass.services.async_call = AsyncMock()
    hass.bus.async_fire = MagicMock()
    if store is None:
        store = MagicMock()
        store.async_save = AsyncMock()
        store.audit_history.return_value = []
        store.unresolved_actions.return_value = []
    result = PowerOrchestratorCoordinator(
        hass=hass,
        model=model(),
        store=store,
        config=CoordinatorConfig(
            load_sensor="sensor.load",
            max_load=5000,
            averaging_period=10,
            safety_reserve=200,
            hysteresis=100,
            pause_period=60,
            grid_loss_mode=grid_mode,
            grid_loss_sensor=grid_sensor,
            battery_threshold=battery_threshold,
            battery_soc_sensor=battery_soc,
            policy=policy,
            execution_mode=execution_mode,
        ),
    )
    result.mode = MODE_AUTO
    return result


def test_grid_safety_uses_semantic_unavailable_state_not_timestamp_age() -> None:
    coordinator_instance = coordinator()
    coordinator_instance.hass.states.get.return_value = None
    assert coordinator_instance.grid_ok is False
    coordinator_instance.hass.states.get.return_value = state("unavailable", age=301)
    assert coordinator_instance.grid_ok is False
    coordinator_instance.hass.states.get.return_value = state("on", age=301)
    assert coordinator_instance.grid_ok is True
    coordinator_instance.hass.states.get.return_value = state("on", updated_age=301)
    assert coordinator_instance.grid_ok is True


def test_grid_sensor_on_is_valid() -> None:
    coordinator_instance = coordinator()
    coordinator_instance.hass.states.get.return_value = state("on")
    assert coordinator_instance.grid_ok is True
    assert coordinator_instance.grid_safety_source_configured is True


def test_battery_threshold_uses_semantically_available_percent_input() -> None:
    coordinator_instance = coordinator(
        grid_mode="battery_threshold",
        grid_sensor=None,
        battery_soc="sensor.soc",
        battery_threshold=20,
    )
    coordinator_instance.hass.states.get.return_value = state("20", unit="%")
    assert coordinator_instance.grid_ok is False
    coordinator_instance.hass.states.get.return_value = state("20.1", unit="%")
    assert coordinator_instance.grid_ok is True
    coordinator_instance.hass.states.get.return_value = state("unavailable", unit="%")
    assert coordinator_instance.grid_ok is False


@pytest.mark.asyncio
async def test_invalid_load_blocks_without_physical_command() -> None:
    coordinator_instance = coordinator()
    coordinator_instance.hass.states.get.side_effect = lambda entity_id: (
        state("on") if entity_id == "binary_sensor.grid" else state("unknown")
    )
    await coordinator_instance._evaluate()
    assert coordinator_instance.status == STATUS_SAFETY_BLOCKED
    coordinator_instance.hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_unavailable_grid_source_is_not_reported_as_confirmed_grid_loss() -> None:
    coordinator_instance = coordinator()
    coordinator_instance.hass.states.get.side_effect = lambda entity_id: (
        state("unavailable")
        if entity_id == "binary_sensor.grid"
        else state("off")
        if entity_id.startswith("switch.")
        else state("0")
    )

    await coordinator_instance._evaluate()

    assert coordinator_instance.status == STATUS_SAFETY_BLOCKED
    assert coordinator_instance.reason_code == "telemetry_invalid"
    assert "unavailable" in coordinator_instance.last_action.lower()
    coordinator_instance.hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_stop_is_guarded_and_confirmed() -> None:
    coordinator_instance = coordinator()
    device = coordinator_instance._model.get_device("d1")
    assert device is not None
    device.is_on = True
    device.ownership = Ownership.PLANNER
    coordinator_instance.hass.states.get.side_effect = lambda entity_id: (
        state("on") if entity_id == "switch.load_1" else state("on")
    )
    coordinator_instance._confirm_device_state = AsyncMock(return_value=True)

    assert await coordinator_instance.async_request_stop("d1", source="test") is True
    coordinator_instance.hass.services.async_call.assert_awaited_once_with(
        "switch", "turn_off", {"entity_id": "switch.load_1"}, blocking=True
    )
    assert device.is_on is False
    assert all(
        call.args[1] != "turn_on"
        for call in coordinator_instance.hass.services.async_call.await_args_list
    )


@pytest.mark.asyncio
async def test_confirmed_stop_is_durable_and_saved_before_command() -> None:
    backend = CopyingStoreBackend()
    runtime_store = RuntimeStore(backend)
    coordinator_instance = coordinator(store=runtime_store)
    device = coordinator_instance._model.get_device("d1")
    assert device is not None
    device.is_on = True
    coordinator_instance._confirm_device_state = AsyncMock(return_value=True)

    async def service_call(*_args, **_kwargs):
        backend.events.append("service")

    coordinator_instance.hass.services.async_call = AsyncMock(side_effect=service_call)

    assert await coordinator_instance.async_request_stop("d1", source="test") is True
    assert backend.events == ["save", "service", "save"]

    fresh_store = RuntimeStore(backend)
    await fresh_store.async_load()
    history = fresh_store.audit_history()
    assert len(history) == 1
    assert history[0]["phase"] == "confirmed"
    assert history[0]["source"] == "test"
    assert fresh_store.unresolved_actions() == []


@pytest.mark.asyncio
async def test_journal_persistence_failure_is_retained_and_retried() -> None:
    backend = CopyingStoreBackend()
    backend.fail_on = {2}
    runtime_store = RuntimeStore(backend)
    coordinator_instance = coordinator(store=runtime_store)
    device = coordinator_instance._model.get_device("d1")
    assert device is not None
    device.is_on = True
    coordinator_instance._confirm_device_state = AsyncMock(return_value=True)

    assert await coordinator_instance.async_request_stop("d1", source="test") is True
    assert device.is_on is False
    assert coordinator_instance._journal_dirty is True
    assert coordinator_instance._journal_persistence_blocked is True

    backend.fail_on.clear()
    assert await coordinator_instance._persist_runtime_if_dirty() is True
    assert coordinator_instance._journal_dirty is False
    assert coordinator_instance._journal_persistence_blocked is False

    fresh_store = RuntimeStore(backend)
    await fresh_store.async_load()
    assert fresh_store.audit_history()[0]["phase"] == "confirmed"
    assert fresh_store.unresolved_actions() == []


@pytest.mark.asyncio
async def test_observe_only_action_is_durable_without_physical_call() -> None:
    backend = CopyingStoreBackend()
    runtime_store = RuntimeStore(backend)
    coordinator_instance = coordinator(store=runtime_store, execution_mode="observe")
    device = coordinator_instance._model.get_device("d1")
    assert device is not None
    device.is_on = True

    assert await coordinator_instance.async_request_stop("d1", source="observe_test") is False
    assert backend.events == ["save"]
    coordinator_instance.hass.services.async_call.assert_not_awaited()

    fresh_store = RuntimeStore(backend)
    await fresh_store.async_load()
    history = fresh_store.audit_history()
    assert len(history) == 1
    assert history[0]["phase"] == "observe_only"
    assert fresh_store.unresolved_actions() == []


@pytest.mark.asyncio
async def test_overload_sheds_one_planner_owned_load_and_never_reenables_it() -> None:
    policy = PolicyConfig.from_mapping(
        {
            "thresholds": [{"power_limit": 1000, "duration_s": 0}],
            "hard_interlock": 9000,
        }
    )
    coordinator_instance = coordinator(policy=policy)
    first = coordinator_instance._model.get_device("d1")
    second = coordinator_instance._model.get_device("d2")
    assert first is not None and second is not None
    first.is_on = True
    first.ownership = Ownership.PLANNER
    second.is_on = False
    coordinator_instance.hass.states.get.side_effect = lambda entity_id: (
        state("on")
        if entity_id in {"binary_sensor.grid", "switch.load_1"}
        else state("off")
        if entity_id == "switch.load_2"
        else state("2000")
    )
    coordinator_instance._confirm_device_state = AsyncMock(return_value=True)

    await coordinator_instance._evaluate()

    assert coordinator_instance.status == STATUS_LOAD_SHEDDING
    assert first.is_on is False
    assert coordinator_instance.hass.services.async_call.await_args.args[1] == "turn_off"
    assert all(
        call.args[1] != "turn_on"
        for call in coordinator_instance.hass.services.async_call.await_args_list
    )


@pytest.mark.asyncio
async def test_zero_power_on_device_is_not_ordinary_shed_candidate() -> None:
    policy = PolicyConfig.from_mapping(
        {
            "thresholds": [{"power_limit": 1000, "duration_s": 0}],
            "hard_interlock": 9000,
        }
    )
    coordinator_instance = coordinator(policy=policy)
    device = coordinator_instance._model.get_device("d1")
    assert device is not None
    device.power_sensor_id = "sensor.load_1_power"
    device.ownership = Ownership.PLANNER
    coordinator_instance.hass.states.get.side_effect = lambda entity_id: (
        state("on")
        if entity_id in {"binary_sensor.grid", "switch.load_1"}
        else state("off")
        if entity_id == "switch.load_2"
        else state("0")
        if entity_id == "sensor.load_1_power"
        else state("2000")
    )

    await coordinator_instance._evaluate()

    assert device.measured_power_valid is True
    assert device.measured_power == 0
    assert coordinator_instance.hass.services.async_call.await_count == 0
    assert coordinator_instance._faulted == set()
    assert coordinator_instance._quarantined == set()


@pytest.mark.asyncio
async def test_no_eligible_load_reports_per_device_reasons() -> None:
    policy = PolicyConfig.from_mapping(
        {
            "thresholds": [{"power_limit": 1000, "duration_s": 0}],
            "hard_interlock": 9000,
        }
    )
    coordinator_instance = coordinator(policy=policy, execution_mode="observe")
    first = coordinator_instance._model.get_device("d1")
    second = coordinator_instance._model.get_device("d2")
    assert first is not None and second is not None
    first.power_sensor_id = "sensor.load_1_power"
    first.ownership = Ownership.PLANNER
    second.ownership = Ownership.PLANNER
    coordinator_instance.hass.states.get.side_effect = lambda entity_id: (
        state("on")
        if entity_id in {"binary_sensor.grid", "switch.load_1"}
        else state("off")
        if entity_id == "switch.load_2"
        else state("0")
        if entity_id == "sensor.load_1_power"
        else state("2000")
    )

    await coordinator_instance._evaluate()

    assert "no eligible load" in coordinator_instance.last_action
    assert "inactive_power=1" in coordinator_instance.last_action
    assert "off=1" in coordinator_instance.last_action
    assert len(coordinator_instance.last_action) <= 255
    assert coordinator_instance._shed_rejection_total == 2
    assert coordinator_instance._shed_rejection_truncated == 0


@pytest.mark.asyncio
async def test_set_execution_mode_evaluates_after_releasing_evaluation_lock() -> None:
    coordinator_instance = coordinator(execution_mode="observe")
    coordinator_instance.hass.states.get.side_effect = lambda entity_id: (
        state("on")
        if entity_id == "binary_sensor.grid"
        else state("off")
        if entity_id.startswith("switch.")
        else state("0")
    )

    await asyncio.wait_for(
        coordinator_instance.async_set_execution_mode("live", confirm_live=True),
        timeout=1,
    )

    assert coordinator_instance.execution_mode == "live"
    coordinator_instance.hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_authorize_shedding_claims_only_exact_live_loads() -> None:
    coordinator_instance = coordinator(execution_mode="observe")
    first = coordinator_instance._model.get_device("d1")
    second = coordinator_instance._model.get_device("d2")
    assert first is not None and second is not None
    first.power_sensor_id = "sensor.load_1_power"
    second.power_sensor_id = "sensor.load_2_power"

    coordinator_instance.hass.states.get.side_effect = lambda entity_id: (
        state("on")
        if entity_id in {"binary_sensor.grid", "switch.load_1"}
        else state("off")
        if entity_id == "switch.load_2"
        else state("3000")
        if entity_id == "sensor.load_1_power"
        else state("0")
        if entity_id == "sensor.load_2_power"
        else state("6000")
    )

    await coordinator_instance.async_authorize_shedding(
        ["d1"], confirm_takeover=True
    )

    assert first.ownership is Ownership.PLANNER
    assert first.ownership_until is None
    assert second.ownership is Ownership.UNKNOWN
    coordinator_instance.hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_authorize_shedding_rolls_back_on_persistence_failure() -> None:
    backend = CopyingStoreBackend()
    backend.fail_on = {1}
    runtime_store = RuntimeStore(backend)
    coordinator_instance = coordinator(store=runtime_store, execution_mode="observe")
    device = coordinator_instance._model.get_device("d1")
    assert device is not None
    device.power_sensor_id = "sensor.load_1_power"
    coordinator_instance.hass.states.get.side_effect = lambda entity_id: (
        state("on")
        if entity_id in {"binary_sensor.grid", "switch.load_1"}
        else state("3000")
        if entity_id == "sensor.load_1_power"
        else state("6000")
    )

    with pytest.raises(RuntimeError, match="synthetic persistence failure"):
        await coordinator_instance.async_authorize_shedding(
            ["d1"], confirm_takeover=True
        )

    assert device.ownership is Ownership.UNKNOWN
    assert device.ownership_until is None
    coordinator_instance.hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_planner_off_does_not_quarantine_normal_overload_candidate() -> None:
    policy = PolicyConfig.from_mapping(
        {
            "thresholds": [{"power_limit": 1000, "duration_s": 0}],
            "hard_interlock": 9000,
        }
    )
    coordinator_instance = coordinator(policy=policy, execution_mode="live")
    coordinator_instance.mode = MODE_OFF
    device = coordinator_instance._model.get_device("d1")
    assert device is not None
    device.ownership = Ownership.PLANNER
    coordinator_instance._last_observed_state["d1"] = True
    coordinator_instance._initial_device_reconciliation_complete = True
    coordinator_instance.hass.states.get.side_effect = lambda entity_id: (
        state("on")
        if entity_id in {"binary_sensor.grid", "switch.load_1"}
        else state("off")
        if entity_id == "switch.load_2"
        else state("6000")
    )

    await coordinator_instance._evaluate()

    assert coordinator_instance.hass.services.async_call.await_count == 0
    assert coordinator_instance._faulted == set()
    assert coordinator_instance._quarantined == set()
    assert "planner mode off" in coordinator_instance.last_action.lower()


@pytest.mark.asyncio
async def test_grid_loss_sheds_active_loads() -> None:
    coordinator_instance = coordinator()
    device = coordinator_instance._model.get_device("d1")
    assert device is not None
    device.is_on = True
    coordinator_instance.hass.states.get.side_effect = lambda entity_id: (
        state("off")
        if entity_id == "binary_sensor.grid"
        else state("on")
        if entity_id == "switch.load_1"
        else state("0")
    )
    coordinator_instance._confirm_device_state = AsyncMock(return_value=True)
    await coordinator_instance._evaluate()
    assert coordinator_instance.status == STATUS_GRID_LOSS
    assert coordinator_instance.hass.services.async_call.await_count == 2


def test_mode_setter_persists_auto_and_off_values() -> None:
    coordinator_instance = coordinator()
    coordinator_instance.mode = MODE_AUTO
    coordinator_instance.mode = MODE_OFF
    assert coordinator_instance._store.set_mode.call_args.args == (MODE_OFF,)
