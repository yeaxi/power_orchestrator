"""Safety-contract tests for grid loss, battery threshold, mode, and pause."""
from __future__ import annotations

import asyncio
import copy
import os
import sys
import time
from types import SimpleNamespace
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components"))

from power_orchestrator.const import (
    EVENT_ACTION,
    GRID_LOSS_MODE_SENSOR,
    GRID_LOSS_MODE_THRESHOLD,
    MODE_AUTO,
    MODE_OFF,
    STATUS_GRID_LOSS,
    STATUS_LOAD_SHEDDING,
    STATUS_SAFETY_BLOCKED,
    SAFETY_INPUT_MAX_AGE_SECONDS,
)
from power_orchestrator.coordinator import PowerOrchestratorCoordinator
from power_orchestrator.policy import (
    DEFAULT_POLICY,
    AuthorizationLease,
    Ownership,
    PolicyConfig,
    PolicyPhase,
    ReasonCode,
    ShedStackEntry,
    ThresholdTier,
)
from power_orchestrator.power_model import ManagedDevice, PowerModel
from power_orchestrator.storage import RuntimeStore


def _state(value, attributes=None):
    return SimpleNamespace(
        state=value,
        attributes=attributes or {"unit_of_measurement": "W"},
        last_reported=datetime.now(timezone.utc),
    )


def _hass():
    hass = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    hass.services.async_call = AsyncMock()
    hass.bus.async_listen = MagicMock()
    return hass


def _store():
    store = MagicMock()
    store.set_pause = MagicMock()
    store.clear_pause = MagicMock()
    store.async_save = AsyncMock()
    return store


class _DeepCopyStore:
    def __init__(self):
        self.data = None

    async def async_load(self):
        return copy.deepcopy(self.data)

    async def async_save(self, data):
        self.data = copy.deepcopy(data)


def _storage_fake():
    return _DeepCopyStore()


def _model():
    model = PowerModel()
    model.add_device(
        ManagedDevice(
            device_id="d1",
            name="Device 1",
            entity_id="switch.d1",
            expected_power=1000,
            priority=1,
        )
    )
    model.add_device(
        ManagedDevice(
            device_id="d2",
            name="Device 2",
            entity_id="switch.d2",
            expected_power=2000,
            priority=2,
        )
    )
    return model


def test_structured_event_has_versioned_safety_envelope():
    coordinator = _coordinator(execution_mode="observe")

    coordinator._emit_event(
        EVENT_ACTION,
        {
            "action": "request_start",
            "reason_code": ReasonCode.OBSERVE_MODE.value,
            "source": "dashboard",
            "actor_id": "user-1",
            "context_id": "ctx-1",
        },
    )

    event = coordinator._decision_events[-1]
    assert event["event_schema"] == 1
    assert event["event_type"] == EVENT_ACTION
    assert event["entry_id"] == "entry-1"
    assert event["execution_mode"] == "observe"
    assert event["source"] == "dashboard"
    assert event["actor_id"] == "user-1"
    assert event["context_id"] == "ctx-1"
    coordinator.hass.bus.async_fire.assert_called_once_with(EVENT_ACTION, event)


def _coordinator(**kwargs):
    hass = kwargs.pop("hass", None) or _hass()
    model = kwargs.pop("model", None) or _model()
    coordinator = PowerOrchestratorCoordinator(
        hass=hass,
        model=model,
        store=kwargs.pop("store", None) or _store(),
        load_sensor=kwargs.pop("load_sensor", "sensor.load"),
        max_load=kwargs.pop("max_load", 5000),
        averaging_period=kwargs.pop("averaging_period", 10),
        safety_reserve=kwargs.pop("safety_reserve", 200),
        hysteresis=kwargs.pop("hysteresis", 200),
        pause_period=kwargs.pop("pause_period", 60),
        grid_loss_mode=kwargs.pop("grid_loss_mode", GRID_LOSS_MODE_SENSOR),
        grid_loss_sensor=kwargs.pop("grid_loss_sensor", "binary_sensor.grid"),
        battery_threshold=kwargs.pop("battery_threshold", None),
        battery_soc_sensor=kwargs.pop("battery_soc_sensor", None),
        solar_forecast_entity=None,
        solar_production_entity=None,
        entry_id=kwargs.pop("entry_id", "entry-1"),
        policy=kwargs.pop("policy", None),
        execution_mode=kwargs.pop("execution_mode", "live"),
    )
    coordinator._startup_safe = False
    coordinator.mode = "auto"
    return coordinator


@pytest.mark.parametrize("value", [None, "unknown", "unavailable", "invalid"])
def test_grid_sensor_invalid_is_fail_closed(value):
    coordinator = _coordinator()
    coordinator.hass.states.get.return_value = _state(value)
    assert coordinator.grid_ok is False


def test_grid_sensor_missing_is_fail_closed():
    coordinator = _coordinator()
    coordinator.hass.states.get.return_value = None
    assert coordinator.grid_ok is False


def test_grid_sensor_on_is_ok():
    coordinator = _coordinator()
    coordinator.hass.states.get.return_value = _state("on")
    assert coordinator.grid_ok is True


@pytest.mark.asyncio
async def test_grid_loss_stops_mixed_logical_group_even_when_primary_is_off():
    model = PowerModel()
    model.add_device(
        ManagedDevice(
            device_id="group",
            name="Grouped device",
            entity_id="switch.primary",
            expected_power=1000,
            actuator_entity_ids=("switch.auxiliary",),
        )
    )
    coordinator = _coordinator(model=model)
    device = coordinator._model.get_device("group")
    assert device is not None
    device.is_on = False
    coordinator._recovery_blocked.add("group")
    coordinator.hass.states.get.side_effect = lambda entity_id: (
        _state("off") if entity_id == "switch.primary" else _state("on")
    )
    coordinator._turn_off_device = AsyncMock(return_value=True)

    await coordinator._handle_grid_loss()

    coordinator._turn_off_device.assert_awaited_once_with(device, emergency=True)


@pytest.mark.parametrize("soc", [20, 19.99, 0, -1, 100.01, "unknown", "nan", "inf"])
def test_battery_threshold_at_or_below_invalid_is_fail_closed(soc):
    coordinator = _coordinator(
        grid_loss_mode=GRID_LOSS_MODE_THRESHOLD,
        grid_loss_sensor=None,
        battery_soc_sensor="sensor.battery_soc",
        battery_threshold=20,
    )
    coordinator.hass.states.get.return_value = _state(
        soc, {"unit_of_measurement": "%"}
    )
    assert coordinator.grid_ok is False


def test_battery_threshold_above_is_ok():
    coordinator = _coordinator(
        grid_loss_mode=GRID_LOSS_MODE_THRESHOLD,
        grid_loss_sensor=None,
        battery_soc_sensor="sensor.battery_soc",
        battery_threshold=20,
    )
    coordinator.hass.states.get.return_value = _state(
        "20.01", {"unit_of_measurement": "%"}
    )
    assert coordinator.grid_ok is True


def test_grid_safety_source_configuration_is_explicit():
    sensor = _coordinator()
    assert sensor.grid_safety_source_configured is True
    sensor._grid_loss_sensor = None
    assert sensor.grid_safety_source_configured is False

    threshold = _coordinator(
        grid_loss_mode=GRID_LOSS_MODE_THRESHOLD,
        grid_loss_sensor=None,
        battery_soc_sensor="sensor.soc",
        battery_threshold=20,
    )
    assert threshold.grid_safety_source_configured is True
    threshold._battery_threshold = None
    assert threshold.grid_safety_source_configured is False
    threshold._grid_loss_mode = "invalid"
    assert threshold.grid_safety_source_configured is False


def test_state_report_timestamp_and_freshness_fail_closed():
    coordinator = _coordinator()
    naive = SimpleNamespace(last_reported=datetime.now())
    assert coordinator._state_reported_timestamp(naive) is not None
    assert coordinator._state_reported_timestamp(SimpleNamespace(last_reported=123.5)) == 123.5
    assert coordinator._state_reported_timestamp(SimpleNamespace(last_reported=True)) is None
    assert coordinator._state_reported_timestamp(SimpleNamespace(last_reported=float("inf"))) is None
    assert coordinator._state_reported_timestamp(SimpleNamespace(last_reported="bad")) is None

    assert coordinator._state_is_fresh(_state("on")) is True
    assert coordinator._state_is_fresh(
        SimpleNamespace(last_reported=time.time() - SAFETY_INPUT_MAX_AGE_SECONDS - 1)
    ) is False
    assert coordinator._state_is_fresh(SimpleNamespace(last_reported=time.time() + 1)) is False


def test_pending_start_reservation_is_singleton_and_releasable():
    coordinator = _coordinator()
    coordinator._load_sensor_valid = True
    coordinator._load_samples.append(100.0)
    coordinator._load_reported_at = 10.0
    coordinator._load_generation = 3
    device = coordinator._model.get_device("d1")
    assert device is not None

    pending = coordinator._reserve_pending_start(device)
    assert pending.device_id == "d1"
    assert coordinator.pending_start_power == 1000
    with pytest.raises(RuntimeError, match="already active"):
        coordinator._reserve_pending_start(device)
    coordinator._clear_pending_start()
    assert coordinator.pending_start_power == 0.0



def test_battery_threshold_wrong_unit_is_fail_closed():
    coordinator = _coordinator(
        grid_loss_mode=GRID_LOSS_MODE_THRESHOLD,
        grid_loss_sensor=None,
        battery_soc_sensor="sensor.battery_soc",
        battery_threshold=20,
    )
    coordinator.hass.states.get.return_value = _state(
        "50", {"unit_of_measurement": "V"}
    )
    assert coordinator.grid_ok is False


@pytest.mark.asyncio
async def test_invalid_load_blocks_evaluation_and_physical_start():
    coordinator = _coordinator()

    def get_state(entity_id):
        if entity_id == "binary_sensor.grid":
            return _state("on")
        if entity_id == "sensor.load":
            return _state("unknown")
        return _state("off")

    coordinator.hass.states.get.side_effect = get_state
    await coordinator._evaluate()

    assert coordinator.status == STATUS_SAFETY_BLOCKED
    coordinator.hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_load_wrong_unit_blocks_evaluation_and_physical_start():
    coordinator = _coordinator()

    def get_state(entity_id):
        if entity_id == "binary_sensor.grid":
            return _state("on")
        if entity_id == "sensor.load":
            return _state("1000", {"unit_of_measurement": "V"})
        return _state("off")

    coordinator.hass.states.get.side_effect = get_state
    await coordinator._evaluate()

    assert coordinator.status == STATUS_SAFETY_BLOCKED
    assert coordinator.load_sensor_reason == "unsupported_unit"
    coordinator.hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_off_mode_never_starts_device():
    coordinator = _coordinator()
    coordinator.mode = MODE_OFF
    coordinator._load_samples.append(1000)
    device = coordinator._model.get_device("d1")
    device.is_on = False

    await coordinator._perform_adding(avg_load=1000, capacity=4000)
    result = await coordinator._turn_on_device(device)

    assert result is False
    assert device.is_on is False
    coordinator.hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_grid_loss_failed_stop_enters_persisted_fault_quarantine():
    coordinator = _coordinator()
    device = coordinator._model.get_device("d1")
    assert device is not None
    device.is_on = True
    coordinator.hass.states.get.return_value = _state("on")
    coordinator._turn_off_device = AsyncMock(return_value=False)

    await coordinator._handle_grid_loss()

    assert device.device_id in coordinator._faulted
    assert device.device_id in coordinator._recovery_blocked
    assert coordinator._fault_reasons[device.device_id] == "relay_readback_timeout"
    coordinator._store.async_save.assert_awaited_once()
    assert coordinator._fault_state_dirty is False


@pytest.mark.asyncio
async def test_manual_reenable_during_grid_loss_creates_persistent_notification():
    coordinator = _coordinator()
    device = coordinator._model.get_device("d1")
    device.is_on = True
    # The device must be causally ON before grid loss; a confirmed OFF
    # actuator is intentionally skipped by the emergency all-stop.
    states = {"switch.d1": "on", "switch.d2": "off"}

    def get_state(entity_id):
        return _state(states.get(entity_id, "off"))

    async def service_call(domain, service, data, blocking=True):
        if service == "turn_off":
            states[data["entity_id"]] = "off"

    coordinator.hass.states.get.side_effect = get_state
    coordinator.hass.services.async_call.side_effect = service_call

    await coordinator._handle_grid_loss()
    assert device.device_id in coordinator._grid_loss_expected_off

    device.is_on = True
    await coordinator._notify_manual_overrides()

    calls = [call.args for call in coordinator.hass.services.async_call.await_args_list]
    assert any(
        args[0] == "persistent_notification"
        and args[1] == "create"
        and args[2]["notification_id"] == "power_orchestrator_entry-1_d1_manual_override"
        for args in calls
    )

    notification_count = sum(
        args[0] == "persistent_notification" and args[1] == "create"
        for args in calls
    )
    await coordinator._notify_manual_overrides()
    calls_again = [call.args for call in coordinator.hass.services.async_call.await_args_list]
    assert sum(
        args[0] == "persistent_notification" and args[1] == "create"
        for args in calls_again
    ) == notification_count


@pytest.mark.asyncio
async def test_execution_mode_persistence_failure_rolls_back_memory_state():
    store = _store()
    store.restore_execution_mode.return_value = None
    store.async_save.side_effect = RuntimeError("storage failure")
    coordinator = _coordinator(store=store, execution_mode="live")

    with pytest.raises(RuntimeError, match="storage failure"):
        await coordinator.async_set_execution_mode("observe")

    assert coordinator.execution_mode == "live"
    store.clear_execution_mode.assert_called_once()


@pytest.mark.asyncio
async def test_observe_to_live_requires_post_transition_telemetry():
    coordinator = _coordinator(execution_mode="observe")
    coordinator._load_generation = 4
    coordinator._load_reported_at = time.time()
    coordinator._load_sensor_valid = True
    coordinator._load_samples.append(1000.0)

    await coordinator.async_set_execution_mode("live", confirm_live=True)

    assert coordinator.execution_mode == "live"
    assert coordinator._execution_mode_reconciliation_required is True
    coordinator._mode = MODE_AUTO
    coordinator._startup_safe = False
    with pytest.raises(ValueError, match="post-execution-mode"):
        await coordinator.async_request_start("d1")


@pytest.mark.asyncio
async def test_observe_mode_blocks_emergency_grid_loss_stop():
    coordinator = _coordinator(execution_mode="observe")
    device = coordinator._model.get_device("d1")
    device.is_on = True
    coordinator.hass.states.get.side_effect = lambda entity_id: (
        _state("on", {}) if entity_id == "switch.d1" else _state("off", {})
    )

    await coordinator._handle_grid_loss()

    coordinator.hass.services.async_call.assert_not_awaited()
    assert device.is_on is True
    assert coordinator.status == "observe"
    assert "no physical command" in coordinator._last_action


@pytest.mark.asyncio
async def test_observe_mode_low_level_emergency_off_is_noop():
    coordinator = _coordinator(execution_mode="observe")
    device = coordinator._model.get_device("d1")
    device.is_on = True

    assert await coordinator._turn_off_device(device, emergency=True) is False
    coordinator.hass.services.async_call.assert_not_awaited()
    assert coordinator._last_operation_result == "observe_only"


@pytest.mark.asyncio
async def test_observe_mode_normal_start_is_noop_without_physical_call():
    coordinator = _coordinator(execution_mode="observe")
    device = coordinator._model.get_device("d1")
    device.is_on = False

    assert await coordinator.async_request_start("d1", source="dashboard") is False
    coordinator.hass.services.async_call.assert_not_awaited()
    assert "Observe" in coordinator._last_action


@pytest.mark.asyncio
async def test_observe_mode_normal_stop_is_noop_without_physical_call():
    coordinator = _coordinator(execution_mode="observe")
    device = coordinator._model.get_device("d1")
    device.is_on = True

    assert await coordinator.async_request_stop("d1", source="dashboard") is False
    coordinator.hass.services.async_call.assert_not_awaited()
    assert "Observe" in coordinator._last_action


@pytest.mark.asyncio
async def test_observe_mode_policy_shed_is_noop_without_physical_call():
    coordinator = _coordinator(
        policy=PolicyConfig.from_mapping({}),
        execution_mode="observe",
    )
    coordinator._load_sensor_valid = True
    coordinator._load_samples.append(7000.0)
    coordinator._load_reported_at = time.time()
    coordinator._load_generation = 1
    coordinator.hass.states.get.side_effect = lambda entity_id: (
        _state("on", {"unit_of_measurement": "W"})
        if entity_id == "binary_sensor.grid"
        else _state("7000", {"unit_of_measurement": "W"})
        if entity_id == "sensor.load"
        else _state("off", {})
    )
    coordinator._policy_engine.observe_load = MagicMock(
        return_value=SimpleNamespace(
            triggered=True,
            recovery_ready=False,
            reason_code=ReasonCode.SHED_SUSTAINED_OVERLOAD,
        )
    )

    await coordinator._evaluate()

    coordinator.hass.services.async_call.assert_not_awaited()
    assert coordinator.status == "observe"
    assert "no physical command" in coordinator._last_action


@pytest.mark.asyncio
async def test_manual_start_is_blocked_during_policy_recovery_phase_without_stack_target():
    coordinator = _coordinator(policy=DEFAULT_POLICY)
    device = coordinator._model.get_device("d1")
    device.is_on = False
    coordinator._policy_engine.runtime.phase = PolicyPhase.SHEDDING
    coordinator._load_sensor_valid = True
    coordinator._load_reported_at = time.time()
    coordinator._load_samples.append(1000.0)
    coordinator.hass.states.get.return_value = _state("on", {})
    coordinator._turn_on_device = AsyncMock(return_value=True)

    with pytest.raises(ValueError, match="recovery"):
        await coordinator.async_request_start("d1", source="dashboard")

    coordinator._turn_on_device.assert_not_awaited()
    assert coordinator.pending_start_power == 0


@pytest.mark.asyncio
async def test_grid_loss_still_sheds_in_off_mode():
    coordinator = _coordinator()
    coordinator.mode = MODE_OFF

    states = {"switch.d1": "on", "switch.d2": "off"}

    def get_state(entity_id):
        if entity_id == "binary_sensor.grid":
            return _state("off")
        if entity_id == "sensor.load":
            return _state("1000")
        return _state(states.get(entity_id, "off"))

    async def service_call(domain, service, data, blocking=True):
        if service == "turn_off":
            states[data["entity_id"]] = "off"

    coordinator.hass.states.get.side_effect = get_state
    coordinator.hass.services.async_call.side_effect = service_call
    coordinator._model.get_device("d1").is_on = True

    await coordinator._evaluate()

    assert coordinator.status == STATUS_GRID_LOSS
    assert coordinator._model.get_device("d1").is_on is False
    assert any(call.args[1] == "turn_off" for call in coordinator.hass.services.async_call.await_args_list)


class _FakeStore:
    def __init__(self, data):
        self.data = data

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        self.data = data


@pytest.mark.asyncio
async def test_restore_pause_ignores_invalid_and_expired_timestamps():
    fake = _FakeStore(
        {
            "pause_timestamps": {
                "d1": "not-a-timestamp",
                "d2": -1,
                "unknown": 10**20,
            }
        }
    )
    store = RuntimeStore(fake)
    await store.async_load()
    model = _model()

    store.restore_pause_timestamps(model)

    assert model.get_device("d1").pause_until is None
    assert model.get_device("d2").pause_until is None
    assert "unknown" not in store._data["pause_timestamps"]


@pytest.mark.asyncio
async def test_invalid_samples_never_become_zeroes_that_authorize_start():
    coordinator = _coordinator()
    load_values = iter(["unknown", "6000"])

    def get_state(entity_id):
        if entity_id == "binary_sensor.grid":
            return _state("on")
        if entity_id == "sensor.load":
            return _state(next(load_values))
        return _state("off")

    coordinator.hass.states.get.side_effect = get_state
    await coordinator._evaluate()
    await coordinator._evaluate()

    assert coordinator.status == "load_shedding"
    assert list(coordinator._load_samples) == [6000]
    assert coordinator.hass.services.async_call.await_count == 0


@pytest.mark.asyncio
async def test_unknown_device_state_never_authorizes_start():
    coordinator = _coordinator()

    def get_state(entity_id):
        if entity_id == "binary_sensor.grid":
            return _state("on")
        if entity_id == "sensor.load":
            return _state("1000")
        return _state("unknown")

    coordinator.hass.states.get.side_effect = get_state
    await coordinator._evaluate()

    assert coordinator.status == "adding_load"
    assert coordinator.hass.services.async_call.await_count == 0
    assert all(device.is_on is None for device in coordinator._model.all_devices())


@pytest.mark.asyncio
async def test_command_success_without_fresh_readback_is_not_claimed():
    coordinator = _coordinator()
    device = coordinator._model.get_device("d1")
    device.is_on = False
    coordinator._reserve_pending_start(device)
    coordinator.hass.states.get.return_value = _state("off")

    assert await coordinator._turn_on_device(device) is False
    assert device.is_on is None
    assert coordinator.hass.services.async_call.await_args_list[0].args[:2] == (
        "switch", "turn_on"
    )
    assert coordinator.hass.services.async_call.await_args_list[1].args[:2] == (
        "switch", "turn_off"
    )

    device.is_on = True
    coordinator.hass.states.get.return_value = _state("on")
    assert await coordinator._turn_off_device(device) is False
    assert device.is_on is None


@pytest.mark.asyncio
async def test_failed_start_persists_quarantine_before_returning():
    coordinator = _coordinator()
    device = coordinator._model.get_device("d1")
    assert device is not None
    device.is_on = False
    coordinator._reserve_pending_start(device)
    coordinator.hass.states.get.return_value = _state("off")
    coordinator._store.async_save.reset_mock()

    assert await coordinator._turn_on_device(device) is False

    assert device.device_id in coordinator._recovery_blocked
    coordinator._store.async_save.assert_awaited()


@pytest.mark.asyncio
async def test_start_journal_failure_vetoes_physical_command_and_blocks_retry():
    coordinator = _coordinator()
    device = coordinator._model.get_device("d1")
    assert device is not None
    device.is_on = False
    coordinator._store.async_save.side_effect = OSError("journal unavailable")
    coordinator._reserve_pending_start(device)

    assert await coordinator._turn_on_device(device) is False

    assert coordinator._journal_persistence_blocked is True
    assert coordinator.hass.services.async_call.await_count == 0


@pytest.mark.asyncio
async def test_precommand_state_cannot_confirm_turn_off():
    coordinator = _coordinator()
    coordinator._relay_readback_timeout = 0.005
    coordinator._relay_readback_poll_interval = 0.001
    device = coordinator._model.get_device("d1")
    device.is_on = True
    coordinator.hass.states.get.return_value = _state("off")

    assert await coordinator._turn_off_device(device) is False
    assert device.is_on is None
    assert coordinator.hass.services.async_call.await_count == 1


@pytest.mark.asyncio
async def test_delayed_relay_readback_is_waited_for_within_bound():
    coordinator = _coordinator()
    coordinator._relay_readback_timeout = 0.05
    coordinator._relay_readback_poll_interval = 0.001
    device = coordinator._model.get_device("d1")
    device.is_on = False
    coordinator._reserve_pending_start(device)
    call_count = 0

    def get_state(_entity_id):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return _state("off")
        return _state("on")

    coordinator.hass.states.get.side_effect = get_state

    async def report_command(*_args, **_kwargs):
        return None

    coordinator.hass.services.async_call.side_effect = report_command
    assert await coordinator._turn_on_device(device) is True
    assert device.is_on is True
    assert coordinator.hass.services.async_call.await_count == 1


@pytest.mark.asyncio
async def test_failed_compensating_stop_leaves_device_unknown():
    coordinator = _coordinator()
    coordinator._relay_readback_timeout = 0.01
    coordinator._relay_readback_poll_interval = 0.001
    device = coordinator._model.get_device("d1")
    device.is_on = False
    coordinator._reserve_pending_start(device)
    coordinator.hass.states.get.return_value = _state("unknown")

    assert await coordinator._turn_on_device(device) is False
    assert device.is_on is None
    assert coordinator.status == STATUS_SAFETY_BLOCKED
    assert coordinator.hass.services.async_call.await_count == 2


@pytest.mark.asyncio
async def test_stale_inputs_cannot_start_loads():
    coordinator = _coordinator()
    fresh_load = _state("1000")
    stale_grid = SimpleNamespace(
        state="on",
        attributes={},
        last_reported=datetime.fromtimestamp(time.time() - 10_000, timezone.utc),
    )

    def get_state(entity_id):
        if entity_id == "binary_sensor.grid":
            return stale_grid
        if entity_id == "sensor.load":
            return fresh_load
        return _state("off")

    coordinator.hass.states.get.side_effect = get_state

    await coordinator._evaluate()

    assert coordinator.hass.services.async_call.await_count == 0
    assert coordinator.status == STATUS_GRID_LOSS


@pytest.mark.asyncio
async def test_safety_freshness_budget_is_independent_of_averaging_period():
    coordinator = _coordinator(averaging_period=300)
    stale_grid = SimpleNamespace(
        state="on",
        attributes={},
        last_reported=datetime.fromtimestamp(
            time.time() - SAFETY_INPUT_MAX_AGE_SECONDS - 1,
            timezone.utc,
        ),
    )

    def get_state(entity_id):
        if entity_id == "binary_sensor.grid":
            return stale_grid
        if entity_id == "sensor.load":
            return _state("1000")
        return _state("off")

    coordinator.hass.states.get.side_effect = get_state
    await coordinator._evaluate()

    assert coordinator._safety_input_max_age == SAFETY_INPUT_MAX_AGE_SECONDS
    assert coordinator.hass.services.async_call.await_count == 0
    assert coordinator.status == STATUS_GRID_LOSS


def test_pause_updates_model_and_storage_together():
    coordinator = _coordinator(pause_period=123)
    device = coordinator._model.get_device("d1")

    coordinator._pause_device(device)

    assert device.pause_until is not None
    assert device.pause_until > time.time()
    coordinator._store.set_pause.assert_called_once_with("d1", 123.0)


@pytest.mark.asyncio
async def test_physical_command_exception_fails_closed():
    coordinator = _coordinator()
    device = coordinator._model.get_device("d1")
    coordinator.hass.services.async_call.side_effect = RuntimeError("service unavailable")

    device.is_on = False
    coordinator._reserve_pending_start(device)
    assert await coordinator._turn_on_device(device) is False
    assert device.is_on is None
    assert coordinator.status == STATUS_SAFETY_BLOCKED

    device.is_on = True
    assert await coordinator._turn_off_device(device) is False
    assert device.is_on is None
    assert coordinator.status == STATUS_SAFETY_BLOCKED


@pytest.mark.asyncio
async def test_adding_attempts_only_one_device_per_cycle():
    coordinator = _coordinator()
    for device in coordinator._model.all_devices():
        device.is_on = False
    coordinator._turn_on_device = AsyncMock(return_value=True)

    await coordinator._perform_adding(avg_load=0, capacity=10_000)

    coordinator._turn_on_device.assert_awaited_once_with(
        coordinator._model.get_device("d1")
    )


@pytest.mark.asyncio
async def test_pending_start_blocks_second_start_until_new_load_report():
    model = PowerModel()
    model.add_device(
        ManagedDevice("d1", "D1", "switch.d1", expected_power=2000, priority=1)
    )
    model.add_device(
        ManagedDevice("d2", "D2", "switch.d2", expected_power=2000, priority=2)
    )
    coordinator = _coordinator(
        model=model,
        max_load=5000,
        safety_reserve=0,
        hysteresis=0,
    )
    states = {
        "sensor.load": _state("1000", {"unit_of_measurement": "W"}),
        "binary_sensor.grid": _state("on", {}),
        "switch.d1": _state("off", {}),
        "switch.d2": _state("off", {}),
    }

    def get_state(entity_id):
        return states.get(entity_id)

    async def service_call(domain, service, data, blocking=True):
        states[data["entity_id"]] = _state("on" if service == "turn_on" else "off", {})

    coordinator.hass.states.get.side_effect = get_state
    coordinator.hass.services.async_call.side_effect = service_call

    await coordinator._evaluate()
    assert coordinator._model.get_device("d1").is_on is True
    assert coordinator._model.get_device("d2").is_on is False

    await coordinator._evaluate()

    assert coordinator._model.get_device("d2").is_on is False
    assert coordinator.hass.services.async_call.await_count == 1

    states["sensor.load"] = _state("1000", {"unit_of_measurement": "W"})
    await coordinator._evaluate()
    assert coordinator._model.get_device("d2").is_on is True


@pytest.mark.asyncio
async def test_failed_start_delayed_on_after_rollback_stays_recovery_blocked():
    coordinator = _coordinator()
    for device in coordinator._model.all_devices():
        device.is_on = False
    states = {
        "binary_sensor.grid": _state("on", {}),
        "sensor.load": _state("1000", {"unit_of_measurement": "W"}),
        "switch.d1": _state("off", {}),
        "switch.d2": _state("off", {}),
    }

    async def service_call(domain, service, data, blocking=True):
        if service == "turn_off":
            states[data["entity_id"]] = _state("off", {})

    coordinator.hass.states.get.side_effect = states.get
    coordinator.hass.services.async_call.side_effect = service_call
    coordinator._relay_readback_timeout = 0.005

    await coordinator._perform_adding(avg_load=0, capacity=5000)

    device = coordinator._model.get_device("d1")
    assert device.is_on is None
    assert coordinator.pending_start_power == 1000
    assert "d1" in coordinator._recovery_blocked

    states["switch.d1"] = _state("on", {})
    await coordinator._evaluate()

    assert device.is_on is None
    assert "d1" in coordinator._recovery_blocked
    assert [call.args[1] for call in coordinator.hass.services.async_call.await_args_list] == [
        "turn_on",
        "turn_off",
        "turn_off",
    ]
    assert coordinator._model.get_device("d2").is_on is False



@pytest.mark.asyncio
async def test_automatic_recovery_clear_uses_fixed_device_power_threshold():
    coordinator = _coordinator(safety_reserve=5000)
    device = coordinator._model.get_device("d1")
    assert device is not None
    device.is_on = False
    device.measured_power = 3000.0
    device.measured_power_valid = True
    coordinator.hass.states.get.side_effect = lambda entity_id: _state("off", {})
    coordinator._load_samples.append(1000.0)
    coordinator._load_sensor_valid = True
    coordinator._load_reported_at = 100.0
    coordinator._load_generation = 1
    coordinator._reserve_pending_start(device)
    assert coordinator._pending_start is not None
    coordinator._pending_start.phase = "recovery_blocked"
    coordinator._pending_start.rollback_off_reported_at = 150.0
    coordinator._recovery_blocked.add(device.device_id)
    coordinator._load_reported_at = 200.0
    coordinator._load_generation = 2

    await coordinator._reconcile_pending_start()

    assert coordinator._pending_start is not None
    assert device.device_id in coordinator._recovery_blocked


@pytest.mark.asyncio
async def test_automatic_recovery_clear_requires_durable_persistence():
    store = _store()
    store.async_save.side_effect = OSError("disk full")
    coordinator = _coordinator(store=store)
    device = coordinator._model.get_device("d1")
    assert device is not None
    device.is_on = False
    device.measured_power = 0.0
    device.measured_power_valid = True
    coordinator.hass.states.get.side_effect = lambda entity_id: _state("off", {})
    coordinator._load_samples.append(1000.0)
    coordinator._load_sensor_valid = True
    coordinator._load_reported_at = 100.0
    coordinator._load_generation = 1
    coordinator._reserve_pending_start(device)
    assert coordinator._pending_start is not None
    coordinator._pending_start.phase = "recovery_blocked"
    coordinator._pending_start.rollback_off_reported_at = 150.0
    coordinator._recovery_blocked.add(device.device_id)
    coordinator._load_reported_at = 200.0
    coordinator._load_generation = 2

    await coordinator._reconcile_pending_start()

    assert coordinator._pending_start is not None
    assert device.device_id in coordinator._recovery_blocked
    assert store.async_save.await_count == 1


@pytest.mark.asyncio
async def test_recovery_quarantine_stays_when_aggregate_load_does_not_fall():
    coordinator = _coordinator()
    device = coordinator._model.get_device("d1")
    coordinator.hass.states.get.side_effect = lambda entity_id: _state("off", {})
    coordinator._load_samples.append(1000.0)
    coordinator._load_reported_at = 100.0
    coordinator._load_generation = 1
    coordinator._reserve_pending_start(device)
    assert coordinator._pending_start is not None
    coordinator._pending_start.phase = "recovery_blocked"
    coordinator._pending_start.rollback_off_reported_at = 150.0
    coordinator._recovery_blocked.add(device.device_id)

    coordinator._load_samples.clear()
    coordinator._load_samples.append(5000.0)
    coordinator._load_reported_at = 200.0
    coordinator._load_generation = 2
    await coordinator._reconcile_pending_start()

    assert coordinator._pending_start is not None
    assert device.device_id in coordinator._recovery_blocked


@pytest.mark.asyncio
async def test_fault_persistence_retries_after_transient_storage_failure():
    store = _store()
    store.async_save.side_effect = [OSError("temporary"), None]
    coordinator = _coordinator(store=store)
    coordinator._faulted.add("d1")
    coordinator._fault_state_dirty = True
    coordinator._evaluate = AsyncMock()

    await coordinator._evaluate_safely()
    assert coordinator._fault_state_dirty is False
    assert store.async_save.await_count == 2
    await coordinator._evaluate_safely()

    assert store.async_save.await_count == 2


@pytest.mark.asyncio
async def test_fault_notification_retries_after_create_failure_without_duplicates():
    coordinator = _coordinator()
    coordinator._faulted.add("d1")
    coordinator.hass.services.async_call.side_effect = [OSError("notify down"), None]
    coordinator._evaluate = AsyncMock()

    await coordinator._evaluate_safely()
    await coordinator._evaluate_safely()

    assert coordinator.hass.services.async_call.await_count == 2
    assert "d1" in coordinator._fault_notifications_sent


@pytest.mark.asyncio
async def test_fault_state_is_persisted_and_notification_is_deduplicated():
    coordinator = _coordinator()
    coordinator._faulted.add("d1")
    coordinator._fault_state_dirty = True
    coordinator._evaluate = AsyncMock()

    await coordinator._evaluate_safely()

    coordinator._store.async_save.assert_awaited()
    assert coordinator.hass.services.async_call.await_count == 1
    assert coordinator.hass.services.async_call.await_args.args[:2] == (
        "persistent_notification",
        "create",
    )

    await coordinator._evaluate_safely()

    coordinator._store.async_save.assert_awaited()
    assert coordinator.hass.services.async_call.await_count == 1


@pytest.mark.asyncio
async def test_clear_quarantine_requires_fresh_safe_proof_and_persists():
    coordinator = _coordinator()
    device = coordinator._model.get_device("d1")
    device.is_on = None
    coordinator._recovery_blocked.add(device.device_id)
    coordinator.hass.states.get.side_effect = lambda entity_id: _state("off", {})
    coordinator._load_samples.append(1000.0)
    coordinator._load_sensor_valid = True
    coordinator._load_reported_at = time.time()

    assert await coordinator.async_clear_quarantine("d1", source="test") is True
    assert device.device_id not in coordinator._recovery_blocked
    assert device.device_id not in coordinator._faulted
    coordinator._store.async_save.assert_awaited_once()


@pytest.mark.asyncio
async def test_clear_quarantine_does_not_use_large_safety_reserve_as_off_proof():
    coordinator = _coordinator(safety_reserve=5000)
    device = coordinator._model.get_device("d1")
    assert device is not None
    device.power_sensor_id = "sensor.d1_power"
    device.measured_power = 3000
    device.measured_power_valid = True
    coordinator._recovery_blocked.add(device.device_id)
    coordinator.hass.states.get.side_effect = lambda entity_id: (
        _state("3000", {"unit_of_measurement": "W"})
        if entity_id == "sensor.d1_power"
        else _state("off", {})
    )
    coordinator._load_samples.append(1000.0)
    coordinator._load_sensor_valid = True
    coordinator._load_reported_at = time.time()

    with pytest.raises(ValueError, match="clear level"):
        await coordinator.async_clear_quarantine("d1")

    assert device.device_id in coordinator._recovery_blocked


@pytest.mark.asyncio
async def test_clear_quarantine_rejects_high_aggregate_load():
    coordinator = _coordinator()
    device = coordinator._model.get_device("d1")
    coordinator._recovery_blocked.add(device.device_id)
    coordinator.hass.states.get.side_effect = lambda entity_id: _state("off", {})
    coordinator._load_samples.append(6000.0)
    coordinator._load_sensor_valid = True
    coordinator._load_reported_at = time.time()

    with pytest.raises(ValueError, match="above the clear gate"):
        await coordinator.async_clear_quarantine("d1")
    assert device.device_id in coordinator._recovery_blocked


@pytest.mark.asyncio
async def test_clear_quarantine_retries_failed_notification_dismissal():
    coordinator = _coordinator()
    device = coordinator._model.get_device("d1")
    device.is_on = None
    coordinator._recovery_blocked.add(device.device_id)
    coordinator._faulted.add(device.device_id)
    coordinator._fault_notifications_sent.add(device.device_id)
    coordinator.hass.states.get.side_effect = lambda entity_id: _state("off", {})
    coordinator.hass.services.async_call.side_effect = [OSError("dismiss down"), None]
    coordinator._load_samples.append(1000.0)
    coordinator._load_sensor_valid = True
    coordinator._load_reported_at = time.time()

    assert await coordinator.async_clear_quarantine("d1", source="test") is True
    assert device.device_id in coordinator._fault_notifications_pending_dismissal

    await coordinator._retry_fault_notification_dismissals()

    assert device.device_id not in coordinator._fault_notifications_pending_dismissal
    assert device.device_id not in coordinator._fault_notifications_sent


@pytest.mark.asyncio
async def test_clear_quarantine_rejects_currently_unavailable_power_sensor_despite_cached_valid_sample():
    coordinator = _coordinator()
    device = coordinator._model.get_device("d1")
    assert device is not None
    device.power_sensor_id = "sensor.d1_power"
    device.measured_power = 0.0
    device.measured_power_valid = True
    coordinator._recovery_blocked.add(device.device_id)
    coordinator.hass.states.get.side_effect = lambda entity_id: (
        _state("unavailable") if entity_id == "sensor.d1_power" else _state("off")
    )
    coordinator._load_samples.append(1000.0)
    coordinator._load_sensor_valid = True
    coordinator._load_reported_at = time.time()

    with pytest.raises(ValueError, match="device measured power"):
        await coordinator.async_clear_quarantine("d1")

    assert device.device_id in coordinator._recovery_blocked


@pytest.mark.asyncio
async def test_clear_quarantine_save_failure_restores_store_snapshot_and_audit():
    fake = _storage_fake()
    store = RuntimeStore(fake)
    await store.async_load()
    coordinator = _coordinator(store=store)
    device = coordinator._model.get_device("d1")
    assert device is not None
    device.power_sensor_id = "sensor.d1_power"
    device.measured_power = 0.0
    device.measured_power_valid = True
    coordinator._recovery_blocked.add(device.device_id)
    coordinator._fault_reasons[device.device_id] = "relay_readback_timeout"
    coordinator.hass.states.get.side_effect = lambda entity_id: (
        _state("0", {"unit_of_measurement": "W"})
        if entity_id == "sensor.d1_power"
        else _state("off")
    )
    coordinator._load_samples.append(1000.0)
    coordinator._load_sensor_valid = True
    coordinator._load_reported_at = time.time()
    coordinator._save_runtime_snapshot()
    await store.async_save()
    real_save = store.async_save
    store.async_save = AsyncMock(side_effect=OSError("disk full"))

    with pytest.raises(OSError, match="disk full"):
        await coordinator.async_clear_quarantine("d1", source="dashboard")

    assert device.device_id in coordinator._recovery_blocked
    assert device.device_id not in coordinator._faulted
    store.async_save = real_save
    store.set_mode("off")
    await store.async_save()

    restored = RuntimeStore(fake)
    await restored.async_load()
    restored_model = PowerModel()
    restored_model.add_device(ManagedDevice("d1", "Device 1", "switch.d1", expected_power=1000))
    _, recovery_blocked = restored.restore_device_runtime(restored_model)
    assert recovery_blocked == {"d1"}
    assert all(
        event.get("action") != "clear_quarantine"
        for event in restored.audit_history()
    )


@pytest.mark.asyncio
async def test_clear_quarantine_persistence_failure_retains_quarantine():
    store = _store()
    store.async_save.side_effect = OSError("disk full")
    coordinator = _coordinator(store=store)
    device = coordinator._model.get_device("d1")
    coordinator._recovery_blocked.add(device.device_id)
    coordinator.hass.states.get.side_effect = lambda entity_id: _state("off", {})
    coordinator._load_samples.append(1000.0)
    coordinator._load_sensor_valid = True
    coordinator._load_reported_at = time.time()

    with pytest.raises(OSError, match="disk full"):
        await coordinator.async_clear_quarantine("d1")
    assert device.device_id in coordinator._recovery_blocked


@pytest.mark.asyncio
async def test_logical_group_requires_per_actuator_causal_readback_markers():
    coordinator = _coordinator()
    device = coordinator._model.get_device("d1")
    device.actuator_entity_ids = ("switch.d1_aux",)
    now = time.time()
    coordinator._active_operations[device.device_id] = 1
    coordinator._relay_readback_timeout = 0
    states = {
        "switch.d1": SimpleNamespace(
            state="on",
            attributes={},
            last_reported=now - 0.05,
        ),
        "switch.d1_aux": SimpleNamespace(
            state="on",
            attributes={},
            last_reported=now - 0.2,
        ),
    }
    coordinator.hass.states.get.side_effect = states.get

    confirmed = await coordinator._confirm_device_state(
        device,
        "on",
        operation_id=1,
        command_issued_at=now - 1.5,
        pre_reported_at={
            "switch.d1": now - 1.0,
            "switch.d1_aux": now - 0.1,
        },
    )

    assert confirmed is False


@pytest.mark.asyncio
async def test_load_report_before_causal_on_does_not_release_reservation():
    coordinator = _coordinator()
    coordinator._load_reported_at = 100.0
    coordinator._load_generation = 1
    coordinator._reserve_pending_start(coordinator._model.get_device("d1"))
    coordinator._pending_start.phase = "waiting_load_telemetry"

    coordinator._load_reported_at = 200.0
    coordinator._load_generation = 2
    await coordinator._reconcile_pending_start()

    assert coordinator.pending_start_power == 1000

    coordinator._pending_start.on_confirmed_reported_at = 150.0
    await coordinator._reconcile_pending_start()
    assert coordinator.pending_start_power == 0


@pytest.mark.asyncio
async def test_recovery_stack_entry_pops_only_after_causal_aggregate_report():
    coordinator = _coordinator(policy=DEFAULT_POLICY)
    device = coordinator._model.get_device("d1")
    entry = ShedStackEntry(
        device_id="d1",
        operation_id="shed-1",
        pre_state=True,
        snapshot={"switch.d1": {"state": "on"}},
        load_generation=1,
        reason_code=ReasonCode.SHED_FAST_OVERLOAD,
    )
    coordinator._policy_engine.runtime.shed_stack.append(entry)
    device.is_on = False
    coordinator._load_generation = 1
    coordinator._load_reported_at = 1.0
    coordinator._turn_on_device = AsyncMock(return_value=True)

    await coordinator._perform_adding(avg_load=0, capacity=5000)

    assert coordinator._policy_engine.next_restore_target() is entry
    assert coordinator._pending_restore_entry is entry
    assert device.snapshot == entry.snapshot

    pending = coordinator._pending_start
    assert pending is not None
    pending.phase = "waiting_load_telemetry"
    pending.on_confirmed_reported_at = 10.0
    coordinator._load_reported_at = 11.0
    coordinator._load_generation = 2

    await coordinator._reconcile_pending_start()

    assert coordinator._policy_engine.next_restore_target() is None
    assert coordinator._pending_restore_entry is None
    assert device.is_on is False


@pytest.mark.asyncio
async def test_recovery_stack_pop_is_durable_before_reconciliation_returns():
    fake = _storage_fake()
    store = RuntimeStore(fake)
    await store.async_load()
    coordinator = _coordinator(store=store, policy=DEFAULT_POLICY)
    device = coordinator._model.get_device("d1")
    assert device is not None
    entry = ShedStackEntry(
        device_id="d1",
        operation_id="shed-durable",
        pre_state=True,
        snapshot={"switch.d1": {"state": "on"}},
        load_generation=1,
        reason_code=ReasonCode.SHED_FAST_OVERLOAD,
    )
    coordinator._policy_engine.runtime.shed_stack.append(entry)
    await coordinator.async_persist_runtime()
    assert fake.data["policy_runtime"]["shed_stack"]

    device.is_on = False
    coordinator._load_generation = 1
    coordinator._load_reported_at = 1.0
    coordinator._turn_on_device = AsyncMock(return_value=True)
    await coordinator._perform_adding(avg_load=0, capacity=5000)

    pending = coordinator._pending_start
    assert pending is not None
    pending.phase = "waiting_load_telemetry"
    pending.on_confirmed_reported_at = 10.0
    coordinator._load_reported_at = 11.0
    coordinator._load_generation = 2

    await coordinator._reconcile_pending_start()

    assert coordinator._policy_engine.next_restore_target() is None
    assert fake.data["policy_runtime"]["shed_stack"] == []


@pytest.mark.asyncio
async def test_recovery_stack_pop_save_failure_retains_target_and_blocks_starts():
    fake = _storage_fake()
    store = RuntimeStore(fake)
    await store.async_load()
    coordinator = _coordinator(store=store, policy=DEFAULT_POLICY)
    device = coordinator._model.get_device("d1")
    assert device is not None
    entry = ShedStackEntry(
        device_id="d1",
        operation_id="shed-save-failure",
        pre_state=True,
        snapshot={"switch.d1": {"state": "on"}},
        load_generation=1,
        reason_code=ReasonCode.SHED_FAST_OVERLOAD,
    )
    coordinator._policy_engine.runtime.shed_stack.append(entry)
    await coordinator.async_persist_runtime()

    async def fail_save(_data):
        raise OSError("disk full")

    fake.async_save = fail_save
    device.is_on = False
    coordinator._load_generation = 1
    coordinator._load_reported_at = 1.0
    coordinator._turn_on_device = AsyncMock(return_value=True)
    await coordinator._perform_adding(avg_load=0, capacity=5000)
    pending = coordinator._pending_start
    assert pending is not None
    pending.phase = "waiting_load_telemetry"
    pending.on_confirmed_reported_at = 10.0
    coordinator._load_reported_at = 11.0
    coordinator._load_generation = 2

    await coordinator._reconcile_pending_start()

    assert coordinator._policy_engine.next_restore_target() is entry
    assert coordinator._pending_restore_entry is entry
    assert coordinator.pending_start_power == 1000
    assert coordinator._journal_persistence_blocked is True
    assert coordinator.status == STATUS_SAFETY_BLOCKED
    assert fake.data["policy_runtime"]["shed_stack"]


@pytest.mark.asyncio
async def test_invalid_load_does_not_release_unresolved_reservation():
    coordinator = _coordinator()
    coordinator._load_reported_at = 100.0
    coordinator._load_generation = 1
    coordinator._reserve_pending_start(coordinator._model.get_device("d1"))
    coordinator.hass.states.get.side_effect = lambda entity_id: (
        _state("on", {})
        if entity_id == "binary_sensor.grid"
        else _state("unknown", {"unit_of_measurement": "W"})
        if entity_id == "sensor.load"
        else _state("off", {})
    )

    await coordinator._evaluate()

    assert coordinator.pending_start_power == 1000
    assert coordinator.status == STATUS_SAFETY_BLOCKED


@pytest.mark.asyncio
async def test_climate_unknown_state_cannot_confirm_on_readback():
    coordinator = _coordinator()
    device = coordinator._model.get_device("d1")
    device.actuator_entity_ids = ("climate.d1",)
    device.entity_id = "climate.d1"
    coordinator._active_operations[device.device_id] = 1
    coordinator._relay_readback_timeout = 0
    now = time.time()
    coordinator.hass.states.get.return_value = _state(
        "unknown",
        {"unit_of_measurement": "W"},
    )

    confirmed = await coordinator._confirm_device_state(
        device,
        "on",
        operation_id=1,
        command_issued_at=now - 1,
        pre_reported_at={"climate.d1": now - 2},
    )

    assert confirmed is False


@pytest.mark.asyncio
async def test_partial_logical_device_state_is_unknown_not_confirmed_off():
    coordinator = _coordinator()
    device = coordinator._model.get_device("d1")
    device.actuator_entity_ids = ("switch.d1_aux",)
    coordinator.hass.states.get.side_effect = lambda entity_id: (
        _state("on", {}) if entity_id == "switch.d1" else _state("off", {})
    )

    assert coordinator._logical_device_state(device) is None
    assert coordinator._actuator_state_on("switch.d1", coordinator.hass.states.get("switch.d1")) is True


@pytest.mark.asyncio
async def test_restored_external_ownership_does_not_restart_grace_on_first_refresh():
    coordinator = _coordinator()
    device = coordinator._model.get_device("d1")
    assert device is not None
    old_until = time.time() + 60
    device.ownership = Ownership.EXTERNAL
    device.ownership_until = old_until
    device.is_on = None
    coordinator.hass.states.get.side_effect = lambda entity_id: (
        _state("on")
        if entity_id in {"switch.d1", "binary_sensor.grid"}
        else _state("off")
    )

    await coordinator._refresh_device_states()

    assert device.ownership is Ownership.EXTERNAL
    assert device.ownership_until is not None
    assert device.ownership_until <= old_until


@pytest.mark.asyncio
async def test_low_load_without_shed_stack_preserves_external_start():
    coordinator = _coordinator(policy=DEFAULT_POLICY)
    device = coordinator._model.get_device("d1")
    coordinator._policy_engine.observe_load(4999, now=0.0)
    coordinator._turn_off_device = AsyncMock(return_value=True)

    await coordinator._handle_external_start(device)

    assert device.ownership is Ownership.EXTERNAL
    assert device.device_id not in coordinator._faulted
    coordinator._turn_off_device.assert_not_awaited()


def test_configured_max_load_remains_admission_ceiling_with_policy():
    coordinator = _coordinator(
        max_load=5000,
        policy=PolicyConfig.from_mapping({}),
        safety_reserve=0,
        hysteresis=0,
    )

    assert coordinator._capacity_for_admission(5500, 5500) == -500


@pytest.mark.asyncio
async def test_request_start_rejects_before_post_arm_aggregate_report():
    coordinator = _coordinator()
    device = coordinator._model.get_device("d1")
    device.is_on = False
    coordinator._mode = MODE_AUTO
    coordinator._startup_safe = False
    coordinator._post_arm_reconciliation_required = True
    coordinator._load_sensor_valid = True
    coordinator._load_samples.append(1000.0)
    coordinator.hass.states.get.return_value = _state("on")
    coordinator._turn_on_device = AsyncMock(return_value=True)

    with pytest.raises(ValueError, match="post-arm"):
        await coordinator.async_request_start("d1")

    coordinator._turn_on_device.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_start_rejects_stale_cached_load_report():
    coordinator = _coordinator()
    device = coordinator._model.get_device("d1")
    assert device is not None
    device.is_on = False
    coordinator._mode = MODE_AUTO
    coordinator._startup_safe = False
    coordinator._load_sensor_valid = True
    coordinator._load_reported_at = time.time() - coordinator._safety_input_max_age - 5
    coordinator._load_samples.append(1000.0)
    coordinator.hass.states.get.return_value = _state("on")
    coordinator._turn_on_device = AsyncMock(return_value=True)

    with pytest.raises(ValueError, match="fresh"):
        await coordinator.async_request_start("d1")

    coordinator._turn_on_device.assert_not_awaited()


@pytest.mark.asyncio
async def test_causal_start_updates_observed_state_before_later_refresh():
    coordinator = _coordinator()
    device = coordinator._model.get_device("d1")
    assert device is not None
    device.is_on = False
    coordinator._reserve_pending_start(device)
    call_count = 0

    def get_state(_entity_id):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return _state("off")
        return _state("on")

    coordinator.hass.states.get.side_effect = get_state
    assert await coordinator._turn_on_device(device) is True

    assert coordinator._last_observed_state[device.device_id] is True
    assert device.ownership is Ownership.PLANNER


@pytest.mark.asyncio
async def test_policy_hard_interlock_sheds_on_first_over_limit_report():
    coordinator = _coordinator(policy=PolicyConfig.from_mapping({}))
    device = coordinator._model.get_device("d1")
    coordinator.hass.states.get.side_effect = lambda entity_id: (
        _state("on", {})
        if entity_id == "binary_sensor.grid"
        else _state("9500", {"unit_of_measurement": "W"})
        if entity_id == "sensor.load"
        else _state("on", {})
        if entity_id == device.entity_id
        else _state("off", {})
    )
    coordinator._perform_emergency_all_stop = AsyncMock()

    await coordinator._evaluate()

    coordinator._perform_emergency_all_stop.assert_awaited_once()
    assert coordinator.reason_code == ReasonCode.HARD_INTERLOCK.value


@pytest.mark.asyncio
async def test_hard_interlock_all_stop_attempts_every_device_after_failure():
    coordinator = _coordinator(policy=PolicyConfig.from_mapping({}))
    for device in coordinator._model.all_devices():
        device.is_on = True
    coordinator._turn_off_device = AsyncMock(side_effect=[False, True])

    await coordinator._perform_emergency_all_stop()

    assert coordinator._turn_off_device.await_count == 2


@pytest.mark.asyncio
async def test_emergency_all_stop_save_failure_retains_journal_safety_block():
    coordinator = _coordinator(policy=PolicyConfig.from_mapping({}))
    for device in coordinator._model.all_devices():
        device.is_on = True
    coordinator._turn_off_device = AsyncMock(return_value=True)
    coordinator._store.async_save.side_effect = OSError("journal unavailable")

    await coordinator._perform_emergency_all_stop()

    assert coordinator._turn_off_device.await_count == 2
    assert coordinator._journal_dirty is True
    assert coordinator._journal_persistence_blocked is True
    assert coordinator.status == STATUS_SAFETY_BLOCKED


@pytest.mark.asyncio
async def test_observe_hard_interlock_all_stop_is_diagnostic_only():
    coordinator = _coordinator(
        policy=PolicyConfig.from_mapping({}),
        execution_mode="observe",
    )
    for device in coordinator._model.all_devices():
        device.is_on = True
    coordinator._turn_off_device = AsyncMock()

    await coordinator._perform_emergency_all_stop()

    coordinator._turn_off_device.assert_not_awaited()
    assert "Observe" in coordinator._last_action


@pytest.mark.asyncio
async def test_current_load_hard_limit_sheds_even_when_average_is_low():
    coordinator = _coordinator(max_load=5000, safety_reserve=0, hysteresis=0)
    device = coordinator._model.get_device("d1")
    device.is_on = True
    coordinator._model.get_device("d2").is_on = False
    coordinator.hass.states.get.side_effect = lambda entity_id: (
        _state("on", {})
        if entity_id == "binary_sensor.grid"
        else _state("10000", {"unit_of_measurement": "W"})
        if entity_id == "sensor.load"
        else _state("on" if entity_id == device.entity_id else "off", {})
    )
    for _ in range(9):
        coordinator._append_load_sample(1000)
    coordinator._perform_shedding = AsyncMock()

    await coordinator._evaluate()

    assert coordinator.status == STATUS_LOAD_SHEDDING
    coordinator._perform_shedding.assert_awaited_once_with(10000.0)


@pytest.mark.asyncio
async def test_hard_limit_sheds_pending_start_before_other_load():
    coordinator = _coordinator(max_load=5000, safety_reserve=0, hysteresis=0)
    pending_device = coordinator._model.get_device("d1")
    other_device = coordinator._model.get_device("d2")
    pending_device.is_on = True
    other_device.is_on = True
    coordinator._reserve_pending_start(pending_device)
    coordinator._pending_start.phase = "waiting_load_telemetry"
    coordinator._pending_start.on_confirmed_reported_at = time.time() - 1

    states = {
        "switch.d1": _state("on", {}),
        "switch.d2": _state("on", {}),
    }

    async def service_call(domain, service, data, blocking=True):
        if service == "turn_off":
            states[data["entity_id"]] = _state("off", {})

    coordinator.hass.states.get.side_effect = lambda entity_id: states.get(
        entity_id, _state("off", {})
    )
    coordinator.hass.services.async_call.side_effect = service_call

    await coordinator._perform_shedding(6000)

    assert coordinator.hass.services.async_call.await_args_list[0].args[2][
        "entity_id"
    ] == "switch.d1"
    assert coordinator.pending_start_power == 1000
    assert "d1" in coordinator._recovery_blocked


@pytest.mark.asyncio
async def test_current_load_equal_to_hard_limit_does_not_start():
    coordinator = _coordinator(max_load=5000, safety_reserve=0, hysteresis=0)

    def get_state(entity_id):
        if entity_id == "binary_sensor.grid":
            return _state("on", {})
        if entity_id == "sensor.load":
            return _state("5000", {"unit_of_measurement": "W"})
        return _state("off", {})

    coordinator.hass.states.get.side_effect = get_state
    await coordinator._evaluate()

    assert coordinator.status == "monitoring"
    coordinator.hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_delayed_activation_keeps_specific_fault_reason_after_emergency_stop():
    coordinator = _coordinator()
    device = coordinator._model.get_device("d1")
    device.is_on = None
    coordinator._recovery_blocked.add(device.device_id)
    coordinator._safety_fault_reason = "old_reason"
    coordinator.hass.states.get.side_effect = lambda entity_id: _state("on")
    coordinator._turn_off_device = AsyncMock(return_value=True)

    await coordinator._refresh_device_states()

    assert coordinator._fault_reasons[device.device_id] == "delayed_activation"
    coordinator._turn_off_device.assert_awaited_once_with(device, emergency=True)


@pytest.mark.asyncio
async def test_pending_start_timeout_enters_recovery_and_stops_device():
    coordinator = _coordinator()
    device = coordinator._model.get_device("d1")
    device.is_on = True
    pending = coordinator._reserve_pending_start(device)
    pending.phase = "waiting_load_telemetry"
    pending.on_confirmed_reported_at = time.time() - 10
    pending.telemetry_deadline_monotonic = time.monotonic() - 1

    states = {
        "binary_sensor.grid": _state("on", {}),
        "sensor.load": _state("1000", {"unit_of_measurement": "W"}),
        "switch.d1": _state("on", {}),
        "switch.d2": _state("off", {}),
    }

    async def service_call(domain, service, data, blocking=True):
        if service == "turn_off":
            states[data["entity_id"]] = _state("off", {})

    coordinator.hass.states.get.side_effect = states.get
    coordinator.hass.services.async_call.side_effect = service_call

    await coordinator._evaluate()

    assert device.is_on is None
    assert "d1" in coordinator._recovery_blocked
    assert coordinator.hass.services.async_call.await_args_list[0].args[1] == "turn_off"


@pytest.mark.asyncio
async def test_evaluator_error_fails_closed_and_attempts_emergency_handling():
    coordinator = _coordinator()
    coordinator._evaluate = AsyncMock(side_effect=RuntimeError("unexpected"))
    coordinator._handle_grid_loss = AsyncMock()
    coordinator._persist_fault_state_if_dirty = AsyncMock()
    coordinator._notify_faults = AsyncMock()
    coordinator._retry_fault_notification_dismissals = AsyncMock()

    await coordinator._evaluate_safely()

    assert coordinator.status == STATUS_SAFETY_BLOCKED
    assert coordinator.load_sensor_valid is False
    assert coordinator.load_sensor_reason == "evaluation_error"
    coordinator._handle_grid_loss.assert_awaited_once()
    coordinator._persist_fault_state_if_dirty.assert_awaited_once()
    coordinator._notify_faults.assert_awaited_once()
    coordinator._retry_fault_notification_dismissals.assert_awaited_once()


@pytest.mark.asyncio
async def test_evaluation_blocks_without_usable_sample_and_hard_interlock_bypasses_dwell():
    coordinator = _coordinator()
    coordinator._refresh_device_states = AsyncMock()
    coordinator._expire_pending_start_if_needed = AsyncMock()
    coordinator._read_load_sensor = MagicMock(return_value=1000.0)
    coordinator._load_sensor_valid = True
    coordinator._accept_load_report = MagicMock(return_value=False)
    coordinator.hass.states.get.return_value = _state("on", {})
    await coordinator._evaluate()
    assert coordinator.status == STATUS_SAFETY_BLOCKED
    assert coordinator.load_sensor_reason == "no_usable_sample"

    interlock = _coordinator(policy=PolicyConfig.from_mapping({"hard_interlock": 9000}))
    interlock._refresh_device_states = AsyncMock()
    interlock._expire_pending_start_if_needed = AsyncMock()
    interlock._read_load_sensor = MagicMock(return_value=9000.0)
    interlock._load_sensor_valid = True
    interlock._load_reported_at = time.time()
    interlock._load_samples.append(9000.0)
    interlock._accept_load_report = MagicMock(return_value=False)
    interlock.hass.states.get.return_value = _state("on", {})
    interlock._perform_emergency_all_stop = AsyncMock()
    await interlock._evaluate()
    assert interlock.status == STATUS_LOAD_SHEDDING
    interlock._perform_emergency_all_stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_forced_evaluations_are_serialized():
    coordinator = _coordinator()
    active = 0
    maximum = 0

    async def fake_evaluate():
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0)
        active -= 1

    coordinator._evaluate_safely = fake_evaluate
    await asyncio.gather(
        coordinator.async_force_evaluate(),
        coordinator.async_force_evaluate(),
    )

    assert maximum == 1


@pytest.mark.asyncio
async def test_invalid_persisted_safety_storage_rejects_auto_arm():
    coordinator = _coordinator()
    coordinator.mode = "off"
    coordinator.restore_device_runtime(
        set(),
        {"d1", "d2"},
        fault_reasons={
            "d1": ReasonCode.PERSISTED_RUNTIME_INVALID.value,
            "d2": ReasonCode.PERSISTED_RUNTIME_INVALID.value,
        },
        storage_invalid=True,
    )

    with pytest.raises(ValueError, match="safety storage is invalid"):
        await coordinator.async_set_mode("auto")

    assert coordinator.mode == "off"
    assert coordinator.safety_storage_invalid is True


@pytest.mark.asyncio
async def test_explicit_arm_requires_post_arm_load_report_after_restart():
    """A pre-arm snapshot must not authorize a physical start after restart."""
    coordinator = _coordinator()
    coordinator._startup_safe = True
    coordinator._turn_on_device = AsyncMock(return_value=True)

    states = {
        "binary_sensor.grid": _state("on", {}),
        "sensor.load": _state("1000", {"unit_of_measurement": "W"}),
        "switch.d1": _state("on", {}),
        "switch.d2": _state("off", {}),
    }
    coordinator.hass.states.get.side_effect = states.get

    # First refresh observes the restored physical state while still disarmed.
    await coordinator._evaluate()

    await coordinator.async_set_mode("auto")

    coordinator._turn_on_device.assert_not_awaited()
    assert coordinator.pending_start_power == 0
    assert coordinator.status == STATUS_SAFETY_BLOCKED

    # The first distinct report after arming only completes reconciliation.
    states["sensor.load"] = _state("1000", {"unit_of_measurement": "W"})
    await coordinator._evaluate()
    coordinator._turn_on_device.assert_not_awaited()
    assert coordinator.pending_start_power == 0

    # A later report is the first one allowed to authorize admission.
    states["sensor.load"] = _state("1000", {"unit_of_measurement": "W"})
    await coordinator._evaluate()
    coordinator._turn_on_device.assert_awaited_once_with(
        coordinator._model.get_device("d2")
    )


@pytest.mark.asyncio
async def test_observe_start_intent_persists_terminal_journal_before_return():
    """Observe intent is durable and never reaches a physical service sink."""
    store = RuntimeStore(_storage_fake())
    await store.async_load()
    coordinator = _coordinator(store=store, execution_mode="observe")

    result = await coordinator.async_request_start(
        "d1",
        source="dashboard",
        actor_id="user-1",
        context_id="ctx-1",
    )

    assert result is False
    coordinator.hass.services.async_call.assert_not_awaited()
    history = store.audit_history()
    assert len(history) == 1
    assert history[0]["action_id"].startswith("intent-")
    assert history[0]["phase"] == "observe_only"
    assert history[0]["result"] == "observe_only"
    assert history[0]["source"] == "dashboard"
    assert history[0]["actor_id"] == "user-1"
    assert history[0]["context_id"] == "ctx-1"


@pytest.mark.asyncio
async def test_physical_stop_journal_reuses_one_action_id_across_lifecycle():
    """Prepared, dispatched, and confirmed stop states upsert one record."""
    store = RuntimeStore(_storage_fake())
    await store.async_load()
    coordinator = _coordinator(store=store)
    device = coordinator._model.get_device("d1")
    assert device is not None
    device.is_on = True
    coordinator._confirm_device_state = AsyncMock(return_value=True)

    result = await coordinator._turn_off_device(
        device,
        source="grid_loss",
        actor_id="system",
        context_id="ctx-grid",
        emergency=True,
    )
    assert result is True
    assert await coordinator._persist_runtime_if_dirty() is True

    history = store.audit_history()
    assert len(history) == 1
    assert history[0]["action_id"].startswith("stop-")
    assert history[0]["phase"] == "confirmed"
    assert history[0]["result"] == "confirmed"
    assert history[0]["source"] == "grid_loss"
    assert history[0]["emergency"] is True


@pytest.mark.asyncio
async def test_restart_blocks_non_lifo_on_with_restored_planner_ownership():
    coordinator = _coordinator(policy=DEFAULT_POLICY)
    d1 = coordinator._model.get_device("d1")
    assert d1 is not None

    coordinator._policy_engine.runtime.phase = PolicyPhase.RECOVERY_WAIT
    coordinator._policy_engine.runtime.shed_stack = [
        ShedStackEntry(
            device_id="d1",
            operation_id="shed-1",
            pre_state=True,
            snapshot={"switch.d1": {"state": "on"}},
            load_generation=1,
            reason_code=ReasonCode.SHED_FAST_OVERLOAD,
        ),
        ShedStackEntry(
            device_id="d2",
            operation_id="shed-2",
            pre_state=True,
            snapshot={"switch.d2": {"state": "on"}},
            load_generation=2,
            reason_code=ReasonCode.SHED_FAST_OVERLOAD,
        ),
    ]
    d1.ownership = Ownership.PLANNER
    d1.is_on = None
    coordinator.hass.states.get.side_effect = lambda entity_id: (
        _state("on") if entity_id == "switch.d1" else _state("off")
    )
    coordinator._turn_off_device = AsyncMock(return_value=True)

    await coordinator._refresh_device_states()

    coordinator._turn_off_device.assert_awaited_once_with(d1)
    assert "d1" in coordinator._faulted
    assert d1.ownership is Ownership.EXTERNAL


def test_startup_treats_prepared_action_as_ambiguous_quarantine():
    store = RuntimeStore(_storage_fake())
    store.record_action(
        {
            "action_id": "start-ambiguous",
            "operation_id": "9",
            "device_id": "d1",
            "action": "turn_on",
            "phase": "prepared",
            "result": "prepared",
        }
    )
    coordinator = _coordinator(store=store)

    coordinator.restore_action_journal(store.unresolved_actions())

    assert coordinator.action_journal_invalid is False
    assert coordinator.safety_storage_invalid is True
    assert coordinator._journal_persistence_blocked is True
    assert "d1" in coordinator._faulted
    assert "d1" in coordinator._recovery_blocked
    assert coordinator._model.get_device("d1").is_on is None
    assert coordinator._fault_reasons["d1"] == "persisted_action_prepared_ambiguous"
    assert store.audit_history()[0]["phase"] == "failed"
    assert store.audit_history()[0]["reason"] == "persisted_action_prepared_ambiguous"



def test_coordinator_restore_and_projection_paths_fail_closed():
    coordinator = _coordinator()
    coordinator.restore_fault_notification_state(
        {"d1": "a" * 200, "unknown": "ignored", "bad": 1},
        {"d2": "pending", "unknown": "ignored", "bad": 1},
    )
    assert coordinator._fault_notification_fingerprints == {"d1": "a" * 128}
    assert coordinator._fault_notification_pending_fingerprints == {"d2": "pending"}

    coordinator.restore_action_journal(
        [
            {"device_id": "unknown", "action_id": "x", "phase": "prepared"},
            {"device_id": "d1", "action_id": "x", "phase": "invalid"},
            {"device_id": "d1", "action_id": "prepared-1", "phase": "prepared"},
            {"device_id": "d2", "action_id": "dispatched-1", "phase": "dispatched"},
        ],
        journal_invalid=True,
    )
    assert coordinator.action_journal_invalid is True
    assert coordinator.safety_storage_invalid is True
    assert coordinator._faulted == {"d1", "d2"}
    assert coordinator._recovery_blocked == {"d1", "d2"}
    assert coordinator._fault_reasons["d1"] == "persisted_action_prepared_ambiguous"
    assert coordinator._fault_reasons["d2"] == "persisted_action_dispatched_unresolved"

    coordinator.restore_device_runtime(
        ["d1", "unknown"],
        ["d2", "unknown"],
        fault_reasons={"d1": "fault", "unknown": "ignored", "bad": 1},
        storage_invalid=False,
    )
    assert coordinator.safety_storage_invalid is False
    assert coordinator._model.get_device("d1").is_on is None
    assert coordinator._model.get_device("d2").is_on is None

    coordinator._store.audit_history.return_value = "invalid"
    coordinator._store.unresolved_actions.return_value = {"invalid": True}
    data = coordinator._build_data()
    assert data["audit_history"] == []
    assert data["journal_unresolved_count"] == 0


@pytest.mark.asyncio
async def test_coordinator_dirty_persistence_retains_failure_flags():
    coordinator = _coordinator()
    assert await coordinator._persist_runtime_if_dirty() is True
    coordinator._journal_dirty = True
    coordinator._save_runtime_snapshot = MagicMock(side_effect=RuntimeError("save"))
    assert await coordinator._persist_runtime_if_dirty() is False
    assert coordinator._journal_persistence_blocked is True

    coordinator._save_runtime_snapshot = MagicMock()
    coordinator._store.async_save = AsyncMock()
    assert await coordinator._persist_runtime_if_dirty() is True
    assert coordinator._journal_dirty is False


def test_mode_setter_rejects_invalid_and_invalid_storage_auto_arm():
    coordinator = _coordinator()
    with pytest.raises(ValueError, match="Unsupported mode"):
        coordinator.mode = "invalid"
    coordinator._safety_storage_invalid = True
    with pytest.raises(ValueError, match="safety storage"):
        coordinator.mode = MODE_AUTO
    coordinator.mode = MODE_OFF
    coordinator._store.set_mode.assert_called_with(MODE_OFF)


def test_startup_quarantines_dispatched_action_until_reconciliation():
    store = RuntimeStore(_storage_fake())
    store.record_action(
        {
            "action_id": "start-2",
            "operation_id": "8",
            "device_id": "d1",
            "action": "turn_on",
            "phase": "dispatched",
            "result": "dispatched",
        }
    )
    coordinator = _coordinator(store=store)

    coordinator.restore_action_journal(store.unresolved_actions())

    assert coordinator.action_journal_invalid is False
    assert coordinator.safety_storage_invalid is True
    assert coordinator._model.get_device("d1").is_on is None
    assert coordinator._fault_reasons["d1"] == "persisted_action_dispatched_unresolved"


# ── Remaining coordinator lifecycle/error boundaries ────────────────


def _prepare_waiting_pending(coordinator, device):
    coordinator._load_reported_at = 100.0
    coordinator._load_generation = 0
    pending = coordinator._reserve_pending_start(device)
    pending.phase = "waiting_load_telemetry"
    pending.on_confirmed_reported_at = 150.0
    coordinator._load_reported_at = 200.0
    coordinator._load_generation = 1
    return pending


def test_coordinator_typed_properties_expose_controller_state():
    coordinator = _coordinator(policy=DEFAULT_POLICY)
    assert coordinator.policy is coordinator._policy
    assert coordinator.shed_stack == []
    assert coordinator.startup_safe is False
    coordinator._startup_safe = True
    assert coordinator.startup_safe is True


@pytest.mark.asyncio
async def test_pending_reconciliation_handles_missing_and_changed_targets():
    coordinator = _coordinator()
    device = coordinator._model.get_device("d1")
    assert device is not None
    _prepare_waiting_pending(coordinator, device)
    coordinator._model._devices.pop(device.device_id)

    await coordinator._reconcile_pending_start()

    assert coordinator.status == STATUS_SAFETY_BLOCKED
    assert coordinator.pending_start_power == 0

    changed = _coordinator(policy=DEFAULT_POLICY)
    target = changed._model.get_device("d1")
    other = changed._model.get_device("d2")
    assert target is not None and other is not None
    _prepare_waiting_pending(changed, target)
    pending_entry = ShedStackEntry(
        device_id=target.device_id,
        operation_id="restore-1",
        pre_state=True,
        snapshot={"switch.d1": {"state": "on"}},
        load_generation=1,
    )
    changed._pending_restore_entry = pending_entry
    changed._policy_engine.runtime.shed_stack = [
        ShedStackEntry(
            device_id=other.device_id,
            operation_id="restore-2",
            pre_state=True,
            snapshot={"switch.d2": {"state": "on"}},
            load_generation=1,
        )
    ]

    await changed._reconcile_pending_start()

    assert changed.status == STATUS_SAFETY_BLOCKED
    assert "stack target changed" in changed.last_action


@pytest.mark.asyncio
async def test_pending_restore_pop_retries_after_persistence_failure():
    backing = _storage_fake()
    store = RuntimeStore(backing)
    await store.async_load()
    coordinator = _coordinator(store=store, policy=DEFAULT_POLICY)
    device = coordinator._model.get_device("d1")
    assert device is not None
    _prepare_waiting_pending(coordinator, device)
    entry = ShedStackEntry(
        device_id=device.device_id,
        operation_id="restore-1",
        pre_state=True,
        snapshot={"switch.d1": {"state": "on"}},
        load_generation=1,
    )
    coordinator._pending_restore_entry = entry
    coordinator._policy_engine.runtime.shed_stack = [entry]
    coordinator._save_runtime_snapshot = MagicMock()
    store.async_save = AsyncMock(side_effect=[OSError("disk full"), None])

    await coordinator._reconcile_pending_start()
    assert coordinator.status == STATUS_SAFETY_BLOCKED
    assert coordinator._pending_restore_entry is entry
    assert coordinator._policy_engine.runtime.shed_stack == [entry]

    await coordinator._reconcile_pending_start()
    assert coordinator._pending_restore_entry is None
    assert coordinator.pending_start_power == 0


@pytest.mark.asyncio
async def test_recovery_reconciliation_fail_closed_then_clears_quarantine():
    missing = _coordinator()
    device = missing._model.get_device("d1")
    assert device is not None
    pending = _prepare_waiting_pending(missing, device)
    pending.phase = "recovery_blocked"
    pending.rollback_off_reported_at = 150.0
    missing._model._devices.pop(device.device_id)
    await missing._reconcile_pending_start()
    assert missing.status == STATUS_SAFETY_BLOCKED

    blocked = _coordinator()
    device = blocked._model.get_device("d1")
    assert device is not None
    pending = _prepare_waiting_pending(blocked, device)
    pending.phase = "recovery_blocked"
    pending.rollback_off_reported_at = 150.0
    blocked._recovery_blocked.add(device.device_id)
    blocked.hass.states.get.return_value = _state("on", {})
    blocked._load_samples.append(1000.0)
    blocked._load_sensor_valid = True
    await blocked._reconcile_pending_start()
    assert blocked._pending_start is pending

    invalid_power = _coordinator()
    device = invalid_power._model.get_device("d1")
    assert device is not None
    device.power_sensor_id = "sensor.d1_power"
    pending = _prepare_waiting_pending(invalid_power, device)
    pending.phase = "recovery_blocked"
    pending.rollback_off_reported_at = 150.0
    invalid_power._recovery_blocked.add(device.device_id)
    invalid_power.hass.states.get.side_effect = lambda entity_id: (
        _state("off", {}) if entity_id == "switch.d1" else _state("bad", {"unit_of_measurement": "W"})
    )
    invalid_power._load_samples.append(1000.0)
    invalid_power._load_sensor_valid = True
    invalid_power._read_current_device_power_for_clear = MagicMock(
        side_effect=ValueError("bad power")
    )
    await invalid_power._reconcile_pending_start()
    assert invalid_power._pending_start is pending

    cleared = _coordinator()
    device = cleared._model.get_device("d1")
    assert device is not None
    device.is_on = False
    pending = _prepare_waiting_pending(cleared, device)
    pending.phase = "recovery_blocked"
    pending.rollback_off_reported_at = 150.0
    cleared._recovery_blocked.add(device.device_id)
    cleared.hass.states.get.return_value = _state("off", {})
    cleared._load_samples.append(1000.0)
    cleared._load_sensor_valid = True
    cleared._dismiss_fault_notification = AsyncMock(return_value=False)

    await cleared._reconcile_pending_start()

    assert cleared._pending_start is None
    assert device.device_id not in cleared._recovery_blocked
    assert device.device_id in cleared._fault_notifications_pending_dismissal


@pytest.mark.asyncio
async def test_pending_expiry_handles_removed_device_fail_closed():
    coordinator = _coordinator()
    device = coordinator._model.get_device("d1")
    assert device is not None
    pending = coordinator._reserve_pending_start(device)
    pending.telemetry_deadline_monotonic = time.monotonic() - 1
    coordinator._model._devices.pop(device.device_id)

    await coordinator._expire_pending_start_if_needed()

    assert coordinator.status == STATUS_SAFETY_BLOCKED
    assert coordinator.pending_start_power == 0


@pytest.mark.parametrize("raw", ["bad", float("nan"), "101"])
def test_threshold_grid_safety_rejects_non_numeric_or_out_of_range_soc(raw):
    coordinator = _coordinator(
        grid_loss_mode=GRID_LOSS_MODE_THRESHOLD,
        battery_soc_sensor="sensor.battery_soc",
        battery_threshold=20,
    )
    coordinator.hass.states.get.return_value = _state(
        raw, {"unit_of_measurement": "%"}
    )
    assert coordinator.grid_ok is False


@pytest.mark.asyncio
async def test_refresh_keeps_recovery_block_unknown_and_expires_external_ownership():
    blocked = _coordinator()
    device = blocked._model.get_device("d1")
    assert device is not None
    blocked._recovery_blocked.add(device.device_id)
    blocked.hass.states.get.return_value = _state("off", {})
    await blocked._refresh_device_states()
    assert device.is_on is None

    expired = _coordinator()
    device = expired._model.get_device("d1")
    assert device is not None
    device.ownership = Ownership.EXTERNAL
    device.ownership_until = time.time() - 1
    expired.hass.states.get.return_value = _state("off", {})
    await expired._refresh_device_states()
    assert device.ownership is Ownership.PLANNER
    assert device.ownership_until is None


@pytest.mark.asyncio
async def test_grid_loss_handles_confirmed_off_pending_and_persistence_failure():
    skipped = _coordinator()
    for device in skipped._model.all_devices():
        device.is_on = False
    skipped.hass.states.get.return_value = _state("off", {})
    await skipped._handle_grid_loss()
    skipped.hass.services.async_call.assert_not_awaited()

    pending = _coordinator()
    device = pending._model.get_device("d1")
    assert device is not None
    device.is_on = False
    pending._load_reported_at = 100.0
    pending._reserve_pending_start(device)
    pending._turn_off_device = AsyncMock(return_value=True)
    pending._last_confirmed_reported_at[device.device_id] = 200.0
    pending.hass.states.get.return_value = _state("off", {})
    await pending._handle_grid_loss()
    assert pending._pending_start is not None
    assert pending._pending_start.phase == "recovery_blocked"

    failed = _coordinator()
    device = failed._model.get_device("d1")
    assert device is not None
    device.is_on = True
    failed.hass.states.get.return_value = _state("on", {})
    failed._turn_off_device = AsyncMock(return_value=False)
    failed._store.async_save = AsyncMock(side_effect=OSError("disk full"))
    await failed._handle_grid_loss()
    assert failed.status == STATUS_SAFETY_BLOCKED
    assert failed._fault_state_dirty is True


@pytest.mark.asyncio
async def test_manual_override_notification_failure_is_non_fatal():
    coordinator = _coordinator()
    device = coordinator._model.get_device("d1")
    assert device is not None
    device.is_on = True
    coordinator._grid_loss_expected_off.add(device.device_id)
    coordinator.hass.services.async_call.side_effect = OSError("notify down")

    await coordinator._notify_manual_overrides()

    assert device.device_id not in coordinator._manual_override_notified


@pytest.mark.asyncio
async def test_emergency_stop_skips_confirmed_off_and_marks_pending_stop():
    coordinator = _coordinator()
    d1 = coordinator._model.get_device("d1")
    d2 = coordinator._model.get_device("d2")
    assert d1 is not None and d2 is not None
    d1.is_on = False
    d2.is_on = False
    coordinator._load_reported_at = 100.0
    coordinator._reserve_pending_start(d1)
    d1.is_on = True
    coordinator.hass.states.get.return_value = _state("off", {})
    coordinator._turn_off_device = AsyncMock(return_value=True)

    await coordinator._perform_emergency_all_stop()

    coordinator._turn_off_device.assert_awaited_once_with(d1, emergency=True)
    assert coordinator._pending_start is not None
    assert coordinator._pending_start.phase == "recovery_blocked"


@pytest.mark.asyncio
async def test_policy_shedding_records_stack_and_failure_quarantines():
    success = _coordinator(policy=DEFAULT_POLICY)
    device = success._model.get_device("d1")
    assert device is not None
    device.is_on = True
    for other in success._model.all_devices():
        if other is not device:
            other.is_on = False
    success.hass.states.get.return_value = _state("on", {})
    success._last_policy_decision = SimpleNamespace(reason_code=ReasonCode.SHED_FAST_OVERLOAD)
    success._turn_off_device = AsyncMock(return_value=True)
    await success._perform_shedding(7000.0)
    assert success.shed_stack[-1].device_id == device.device_id
    success._store.save_policy_runtime.assert_called()

    failed = _coordinator(policy=DEFAULT_POLICY)
    device = failed._model.get_device("d1")
    assert device is not None
    device.is_on = True
    for other in failed._model.all_devices():
        if other is not device:
            other.is_on = False
    failed._turn_off_device = AsyncMock(return_value=False)
    await failed._perform_shedding(7000.0)
    assert device.device_id in failed._faulted
    assert failed.status == STATUS_SAFETY_BLOCKED


@pytest.mark.asyncio
async def test_adding_load_guard_and_recovery_target_filters():
    guarded = _coordinator()
    guarded._last_admission_generation = guarded._load_generation
    await guarded._perform_adding(1000.0, 5000.0)

    no_target = _coordinator(policy=DEFAULT_POLICY)
    device = no_target._model.get_device("d1")
    assert device is not None
    device.is_on = False
    no_target._policy_engine.runtime.shed_stack = [
        ShedStackEntry(device.device_id, "op", True, {}, 1)
    ]
    no_target._policy_engine.next_restore_target = MagicMock(return_value=None)
    await no_target._perform_adding(1000.0, 5000.0)
    assert no_target._pending_start is None

    blocked = _coordinator(policy=DEFAULT_POLICY)
    device = blocked._model.get_device("d1")
    assert device is not None
    device.is_on = False
    blocked._recovery_blocked.add(device.device_id)
    blocked._policy_engine.runtime.shed_stack = [
        ShedStackEntry(device.device_id, "op", True, {}, 1)
    ]
    await blocked._perform_adding(1000.0, 5000.0)
    assert blocked._pending_start is None

    failed_restore = _coordinator(policy=DEFAULT_POLICY)
    device = failed_restore._model.get_device("d1")
    assert device is not None
    device.is_on = False
    entry = ShedStackEntry(device.device_id, "op", True, {}, 1)
    failed_restore._policy_engine.runtime.shed_stack = [entry]
    failed_restore._turn_on_device = AsyncMock(return_value=False)
    await failed_restore._perform_adding(1000.0, 5000.0)
    assert failed_restore._pending_restore_entry is None


@pytest.mark.asyncio
async def test_confirmation_generation_mismatch_and_rollback_exception_are_safe():
    coordinator = _coordinator()
    device = coordinator._model.get_device("d1")
    assert device is not None
    coordinator._active_operations[device.device_id] = 1
    assert not await coordinator._confirm_device_state(
        device,
        "on",
        operation_id=2,
        command_issued_at=time.time(),
        pre_reported_at=None,
    )

    rollback = _coordinator()
    device = rollback._model.get_device("d1")
    assert device is not None
    rollback._reserve_pending_start(device)
    rollback._turn_off_device = AsyncMock(side_effect=RuntimeError("relay"))
    rollback._persist_fault_state_if_dirty = AsyncMock()
    assert await rollback._rollback_failed_start(device) is False
    assert rollback._pending_start is not None
    assert rollback._pending_start.phase == "recovery_blocked"


@pytest.mark.asyncio
async def test_request_start_observe_unknown_and_guard_matrix():
    observe = _coordinator(execution_mode="observe")
    with pytest.raises(ValueError, match="unknown"):
        await observe.async_request_start("missing")
    assert await observe.async_request_start("d1") is False
    assert "Observe: start intent" in observe.last_action

    guard_names = [
        "planner_off",
        "post_arm",
        "execution_reconciliation",
        "pending_start",
        "post_shed",
        "generation_consumed",
        "bad_grid",
        "bad_load",
        "missing_current",
        "device_on",
        "faulted",
        "paused",
        "recovery_not_ready",
        "external",
        "manual",
        "manual_active",
        "wrong_restore_target",
        "capacity",
        "solar",
    ]
    for guard_name in guard_names:
        coordinator = _coordinator(policy=DEFAULT_POLICY, execution_mode="live")
        device = coordinator._model.get_device("d1")
        other = coordinator._model.get_device("d2")
        assert device is not None and other is not None
        coordinator._mode = "auto"
        coordinator._startup_safe = False
        coordinator._load_sensor_valid = True
        coordinator._load_reported_at = time.time()
        coordinator._load_samples.append(1000.0)
        coordinator._load_generation = 1
        coordinator._last_admission_generation = 0
        coordinator.hass.states.get.return_value = _state("on", {})
        device.is_on = False
        device.ownership = Ownership.PLANNER
        coordinator._solar_forecast_ok = MagicMock(return_value=True)

        if guard_name == "planner_off":
            coordinator._mode = "off"
        elif guard_name == "post_arm":
            coordinator._post_arm_reconciliation_required = True
        elif guard_name == "execution_reconciliation":
            coordinator._execution_mode_reconciliation_required = True
        elif guard_name == "pending_start":
            coordinator._reserve_pending_start(device)
        elif guard_name == "post_shed":
            coordinator._policy_engine.runtime.pending_post_shed_generation = 2
        elif guard_name == "generation_consumed":
            coordinator._last_admission_generation = coordinator._load_generation
        elif guard_name == "bad_grid":
            coordinator.hass.states.get.return_value = _state("off", {})
        elif guard_name == "bad_load":
            coordinator._load_sensor_valid = False
        elif guard_name == "missing_current":
            coordinator._load_samples.clear()
        elif guard_name == "device_on":
            device.is_on = True
        elif guard_name == "faulted":
            coordinator._faulted.add(device.device_id)
        elif guard_name == "paused":
            device.pause_until = time.time() + 30
        elif guard_name == "recovery_not_ready":
            coordinator._policy_engine.runtime.shed_stack = [
                ShedStackEntry(other.device_id, "op", True, {}, 1)
            ]
            coordinator._last_policy_decision = SimpleNamespace(recovery_ready=False)
        elif guard_name == "external":
            device.ownership = Ownership.EXTERNAL
            device.ownership_until = time.time() + 30
        elif guard_name == "manual":
            device.ownership = Ownership.MANUAL
        elif guard_name == "manual_active":
            coordinator._policy_engine.runtime.phase = PolicyPhase.SHEDDING
        elif guard_name == "wrong_restore_target":
            coordinator._policy_engine.runtime.shed_stack = [
                ShedStackEntry(other.device_id, "op", True, {}, 1)
            ]
            coordinator._last_policy_decision = SimpleNamespace(recovery_ready=True)
        elif guard_name == "capacity":
            device.expected_power = 100000
        elif guard_name == "solar":
            coordinator._solar_forecast_ok.return_value = False

        with pytest.raises(ValueError):
            await coordinator.async_request_start(device.device_id)

    admitted = _coordinator(policy=DEFAULT_POLICY, execution_mode="live")
    device = admitted._model.get_device("d1")
    assert device is not None
    admitted._mode = "auto"
    admitted._load_sensor_valid = True
    admitted._load_reported_at = time.time()
    admitted._load_samples.append(1000.0)
    admitted._load_generation = 1
    admitted.hass.states.get.return_value = _state("on", {})
    device.is_on = False
    admitted._solar_forecast_ok = MagicMock(return_value=True)
    admitted._turn_on_device = AsyncMock(return_value=False)
    assert await admitted.async_request_start(device.device_id) is False
    admitted._turn_on_device.assert_awaited_once()


@pytest.mark.asyncio
async def test_request_stop_guards_pause_and_persistence_failure():
    coordinator = _coordinator()
    with pytest.raises(ValueError, match="unknown"):
        await coordinator.async_request_stop("missing")
    device = coordinator._model.get_device("d1")
    assert device is not None
    device.is_on = False
    assert await coordinator.async_request_stop(device.device_id) is False

    stopped = _coordinator(execution_mode="live")
    device = stopped._model.get_device("d1")
    assert device is not None
    device.is_on = True
    stopped._turn_off_device = AsyncMock(return_value=True)
    assert await stopped.async_request_stop(device.device_id) is True
    assert device.pause_until is not None

    failed = _coordinator(execution_mode="live")
    device = failed._model.get_device("d1")
    assert device is not None
    device.is_on = True
    failed._turn_off_device = AsyncMock(return_value=False)
    failed._persist_runtime_if_dirty = AsyncMock(return_value=False)
    with pytest.raises(RuntimeError, match="journal"):
        await failed.async_request_stop(device.device_id)


@pytest.mark.asyncio
async def test_clear_quarantine_requires_fresh_off_load_and_power_proof():
    unknown = _coordinator()
    with pytest.raises(ValueError, match="unknown"):
        await unknown.async_clear_quarantine("missing")

    clean = _coordinator()
    assert await clean.async_clear_quarantine("d1") is False

    cases = ["pending", "on", "load", "current", "high"]
    for case in cases:
        coordinator = _coordinator(policy=DEFAULT_POLICY)
        device = coordinator._model.get_device("d1")
        assert device is not None
        coordinator._faulted.add(device.device_id)
        coordinator._load_sensor_valid = True
        coordinator._load_reported_at = time.time()
        coordinator._load_samples.append(1000.0)
        coordinator.hass.states.get.return_value = _state("off", {})
        if case == "pending":
            coordinator._reserve_pending_start(device)
        elif case == "on":
            coordinator.hass.states.get.return_value = _state("on", {})
        elif case == "load":
            coordinator._load_sensor_valid = False
        elif case == "current":
            coordinator._load_samples.clear()
        elif case == "high":
            coordinator._load_samples.clear()
            coordinator._load_samples.extend([7000.0])
        with pytest.raises(ValueError):
            await coordinator.async_clear_quarantine(device.device_id)

    successful = _coordinator(policy=DEFAULT_POLICY)
    device = successful._model.get_device("d1")
    assert device is not None
    successful._faulted.add(device.device_id)
    successful._load_sensor_valid = True
    successful._load_reported_at = time.time()
    successful._load_samples.append(1000.0)
    successful.hass.states.get.return_value = _state("off", {})
    successful._dismiss_fault_notification = AsyncMock(return_value=True)
    assert await successful.async_clear_quarantine(device.device_id) is True
    assert device.device_id not in successful._faulted


@pytest.mark.asyncio
async def test_turn_on_climate_and_readback_failure_paths_are_fail_closed():
    climate = _coordinator(execution_mode="live")
    device = climate._model.get_device("d1")
    assert device is not None
    device.actuator_entity_ids = ("climate.kitchen",)
    device.snapshot = {"climate.kitchen": {"attributes": {"hvac_mode": "cool"}}}
    device.is_on = False
    climate._load_reported_at = 100.0
    pending = climate._reserve_pending_start(device)
    climate._persist_runtime_if_dirty = AsyncMock(return_value=True)
    climate._confirm_device_state = AsyncMock(return_value=True)
    climate._last_confirmed_reported_at[device.device_id] = 200.0
    climate.hass.states.get.return_value = _state("off", {})
    assert await climate._turn_on_device(device) is True
    assert any(call.args[:2] == ("climate", "set_hvac_mode") for call in climate.hass.services.async_call.await_args_list)
    assert pending.phase == "waiting_load_telemetry"

    lost = _coordinator(execution_mode="live")
    device = lost._model.get_device("d1")
    assert device is not None
    lost._load_reported_at = 100.0
    lost._reserve_pending_start(device)
    lost._persist_runtime_if_dirty = AsyncMock(return_value=True)
    lost._confirm_device_state = AsyncMock(side_effect=lambda *args, **kwargs: (setattr(lost, "_pending_start", None) or True))
    lost._rollback_failed_start = AsyncMock(return_value=False)
    assert await lost._turn_on_device(device) is False

    marker_missing = _coordinator(execution_mode="live")
    device = marker_missing._model.get_device("d1")
    assert device is not None
    marker_missing._load_reported_at = 100.0
    marker_missing._reserve_pending_start(device)
    marker_missing._persist_runtime_if_dirty = AsyncMock(return_value=True)
    marker_missing._confirm_device_state = AsyncMock(return_value=True)
    marker_missing._rollback_failed_start = AsyncMock(return_value=False)
    assert await marker_missing._turn_on_device(device) is False

    command_error = _coordinator(execution_mode="live")
    device = command_error._model.get_device("d1")
    assert device is not None
    command_error._load_reported_at = 100.0
    command_error._reserve_pending_start(device)
    command_error._persist_runtime_if_dirty = AsyncMock(return_value=True)
    command_error._rollback_failed_start = AsyncMock(return_value=False)
    command_error.hass.services.async_call.side_effect = RuntimeError("relay")
    assert await command_error._turn_on_device(device) is False


@pytest.mark.asyncio
async def test_turn_off_climate_and_observe_event_failures_are_safe():
    coordinator = _coordinator(execution_mode="live")
    device = coordinator._model.get_device("d1")
    assert device is not None
    device.actuator_entity_ids = ("climate.kitchen",)
    device.is_on = True
    coordinator._persist_runtime_if_dirty = AsyncMock(return_value=True)
    coordinator._confirm_device_state = AsyncMock(return_value=True)
    coordinator.hass.states.get.return_value = _state("on", {})
    assert await coordinator._turn_off_device(device) is True
    assert any(call.args[:2] == ("climate", "set_hvac_mode") for call in coordinator.hass.services.async_call.await_args_list)

    observe = _coordinator()
    observe._persist_runtime_if_dirty = AsyncMock(return_value=False)
    assert await observe._record_observe_only_action(action="turn_on", reason="observe") is False
    observe.hass.bus.async_fire.side_effect = RuntimeError("bus unavailable")
    observe._emit_event("power_orchestrator.test", {})


@pytest.mark.asyncio
async def test_evaluation_success_paths_and_load_sample_pruning():
    execution = _coordinator()
    execution._refresh_device_states = AsyncMock()
    execution._expire_pending_start_if_needed = AsyncMock()
    execution._read_load_sensor = MagicMock(return_value=1000.0)
    execution._load_sensor_valid = True
    issued = time.time() - 2
    execution._execution_mode_reconciliation_required = True
    execution._execution_mode_transition_issued_at = issued
    execution._execution_mode_transition_generation = 0
    execution._load_reported_at = time.time()
    execution._load_generation = 1
    execution._load_samples.append(1000.0)
    execution._last_confirmed_reported_at["d1"] = time.time()
    execution.hass.states.get.return_value = _state("on", {})
    await execution._evaluate()
    assert execution._execution_mode_reconciliation_required is False
    assert execution.status == "monitoring"

    legacy_current = _coordinator(execution_mode="observe")
    legacy_current._refresh_device_states = AsyncMock()
    legacy_current._expire_pending_start_if_needed = AsyncMock()
    legacy_current._read_load_sensor = MagicMock(return_value=7000.0)
    legacy_current._load_sensor_valid = True
    legacy_current._load_reported_at = time.time()
    legacy_current.hass.states.get.return_value = _state("on", {})
    await legacy_current._evaluate()
    assert legacy_current.status == "observe"

    legacy_average = _coordinator(execution_mode="observe")
    legacy_average._refresh_device_states = AsyncMock()
    legacy_average._expire_pending_start_if_needed = AsyncMock()
    legacy_average._read_load_sensor = MagicMock(return_value=1000.0)
    legacy_average._load_sensor_valid = True
    legacy_average._load_reported_at = time.time()
    legacy_average._load_samples.extend([11000.0, 1000.0])
    legacy_average._accept_load_report = MagicMock(return_value=False)
    legacy_average.hass.states.get.return_value = _state("on", {})
    await legacy_average._evaluate()
    assert legacy_average.status == "observe"

    recovery = _coordinator(policy=DEFAULT_POLICY)
    recovery._refresh_device_states = AsyncMock()
    recovery._expire_pending_start_if_needed = AsyncMock()
    recovery._read_load_sensor = MagicMock(return_value=1000.0)
    recovery._load_sensor_valid = True
    recovery._load_reported_at = time.time()
    recovery._load_samples.append(1000.0)
    recovery._policy_engine.runtime.shed_stack = [
        ShedStackEntry("d1", "op", True, {}, 1)
    ]
    recovery._last_policy_decision = SimpleNamespace(recovery_ready=False)
    recovery.hass.states.get.return_value = _state("on", {})
    await recovery._evaluate()
    assert recovery.status == "recovery_wait"

    pruning = _coordinator()
    pruning._averaging_period = 1
    pruning._load_samples.append(1.0)
    pruning._load_sample_times.append(time.monotonic() - 10)
    pruning._append_load_sample(2.0)
    assert list(pruning._load_samples) == [2.0]


@pytest.mark.asyncio
async def test_mode_and_execution_mode_persistence_boundaries():
    coordinator = _coordinator()
    coordinator._evaluate_safely = AsyncMock()
    coordinator.async_set_updated_data = MagicMock()
    await coordinator.async_set_mode("off")
    assert coordinator.mode == "off"
    assert coordinator.startup_safe is True

    with pytest.raises(ValueError, match="execution mode"):
        await coordinator.async_set_execution_mode("invalid")
    with pytest.raises(ValueError, match="explicit confirmation"):
        await coordinator.async_set_execution_mode("live")

    observe = _coordinator(execution_mode="observe")
    observe._persist_runtime_if_dirty = AsyncMock(return_value=False)
    with pytest.raises(RuntimeError, match="persisted"):
        await observe.async_request_start("d1")


@pytest.mark.asyncio
async def test_evaluate_safely_handles_evaluator_and_emergency_errors():
    coordinator = _coordinator()
    coordinator._evaluate = AsyncMock(side_effect=RuntimeError("evaluator"))
    coordinator._handle_grid_loss = AsyncMock(side_effect=RuntimeError("stop"))
    coordinator._persist_fault_state_if_dirty = AsyncMock()
    coordinator._notify_faults = AsyncMock()
    coordinator._retry_fault_notification_dismissals = AsyncMock()
    await coordinator._evaluate_safely()
    assert coordinator.status == STATUS_SAFETY_BLOCKED
    assert coordinator.load_sensor_reason == "evaluation_error"


@pytest.mark.asyncio
async def test_policy_evaluation_observe_mode_and_pending_shed_fence_are_fail_closed():
    policy = PolicyConfig(
        thresholds=(ThresholdTier("test", 100.0, 0.0, ReasonCode.SHED_FAST_OVERLOAD),)
    )
    coordinator = _coordinator(policy=policy, execution_mode="observe")
    coordinator._refresh_device_states = AsyncMock()
    coordinator._expire_pending_start_if_needed = AsyncMock()
    coordinator._read_load_sensor = MagicMock(return_value=7000.0)
    coordinator._load_sensor_valid = True
    coordinator._load_reported_at = time.time()
    coordinator.hass.states.get.return_value = _state("on", {})

    await coordinator._evaluate()

    assert coordinator.status == "observe"
    coordinator.hass.services.async_call.assert_not_awaited()
    assert "no physical command" in coordinator.last_action

    fenced = _coordinator(policy=policy)
    fenced._refresh_device_states = AsyncMock()
    fenced._expire_pending_start_if_needed = AsyncMock()
    fenced._read_load_sensor = MagicMock(return_value=7000.0)
    fenced._load_sensor_valid = True
    fenced._load_reported_at = time.time()
    fenced._policy_engine.runtime.pending_post_shed_generation = 99
    fenced.hass.states.get.return_value = _state("on", {})

    await fenced._evaluate()

    assert fenced.status == "recovery_wait"
    assert "newer aggregate-load" in fenced.last_action


@pytest.mark.asyncio
async def test_evaluation_waits_for_execution_mode_and_post_arm_reconciliation():
    for latch in ("execution", "arm"):
        coordinator = _coordinator()
        coordinator._refresh_device_states = AsyncMock()
        coordinator._expire_pending_start_if_needed = AsyncMock()
        coordinator._read_load_sensor = MagicMock(return_value=1000.0)
        coordinator._load_sensor_valid = True
        coordinator._load_reported_at = time.time()
        coordinator._load_samples.append(1000.0)
        coordinator.hass.states.get.return_value = _state("on", {})
        if latch == "execution":
            coordinator._execution_mode_reconciliation_required = True
            coordinator._execution_mode_transition_issued_at = time.time() + 10
            coordinator._execution_mode_transition_generation = 0
        else:
            coordinator._post_arm_reconciliation_required = True
            coordinator._arm_issued_at = time.time() + 10
            coordinator._arm_load_generation = 0

        await coordinator._evaluate()

        assert coordinator.status == STATUS_SAFETY_BLOCKED
        assert "reconciliation" in coordinator.last_action or "aggregate load" in coordinator.last_action


@pytest.mark.asyncio
async def test_authorized_external_start_is_reconciled_without_stealing_ownership():
    coordinator = _coordinator()
    device = coordinator._model.get_device("d1")
    assert device is not None
    marker = time.time()
    state = _state("on", {})
    state.last_reported = datetime.fromtimestamp(marker, tz=timezone.utc)
    marker = state.last_reported.timestamp()
    coordinator.hass.states.get.return_value = state
    coordinator._authorization_leases[device.device_id] = AuthorizationLease(
        device_id=device.device_id,
        operation_id="op-1",
        allowed_state="on",
        expires_at=marker + 30,
        reported_at=marker,
    )

    await coordinator._handle_external_start(device)

    assert device.ownership is Ownership.PLANNER
    assert device.ownership_until is None
    assert device.device_id not in coordinator._faulted


@pytest.mark.asyncio
async def test_manual_start_during_shedding_is_quarantined_and_compensated():
    coordinator = _coordinator()
    device = coordinator._model.get_device("d1")
    assert device is not None
    device.is_on = True
    coordinator._policy_engine.runtime.phase = PolicyPhase.SHEDDING
    coordinator._turn_off_device = AsyncMock(return_value=True)

    await coordinator._handle_external_start(device)

    assert device.device_id in coordinator._faulted
    assert device.ownership is Ownership.EXTERNAL
    assert coordinator.status == STATUS_SAFETY_BLOCKED
    coordinator._turn_off_device.assert_awaited_once_with(device)


@pytest.mark.asyncio
async def test_device_power_telemetry_reasons_are_fail_closed():
    cases = [
        (None, "W", "unavailable_or_stale"),
        ("3", "V", "unsupported_unit"),
        (object(), "W", "non_numeric"),
        (float("nan"), "W", "invalid_value"),
        ("-1", "W", "negative_value"),
        ("3.5", "W", "ok"),
    ]
    for raw, unit, expected_reason in cases:
        coordinator = _coordinator()
        device = coordinator._model.get_device("d1")
        assert device is not None
        device.power_sensor_id = "sensor.d1_power"
        power_state = None if raw is None else _state(raw, {"unit_of_measurement": unit})
        coordinator.hass.states.get.side_effect = lambda entity_id, ps=power_state: (
            _state("off", {}) if entity_id == "switch.d1" else ps
        )

        await coordinator._refresh_device_states()

        assert device.measured_power_reason == expected_reason
        assert device.measured_power_valid is (expected_reason == "ok")


@pytest.mark.asyncio
async def test_fault_notification_revision_paths_are_idempotent():
    coordinator = _coordinator()
    device = coordinator._model.get_device("d1")
    assert device is not None
    coordinator._faulted.add(device.device_id)
    reason = coordinator._fault_notification_reason(device.device_id)
    fingerprint = coordinator._fault_notification_fingerprint(device.device_id, reason)
    coordinator._fault_notification_pending_fingerprints[device.device_id] = fingerprint
    coordinator._fault_notifications_pending_dismissal.add(device.device_id)

    await coordinator._notify_faults()

    assert device.device_id in coordinator._fault_notifications_sent
    coordinator.hass.services.async_call.assert_not_awaited()

    coordinator._fault_notification_fingerprints.pop(device.device_id)
    coordinator._fault_notifications_sent.add(device.device_id)
    await coordinator._notify_faults()
    coordinator.hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_emergency_all_stop_marks_failed_devices_safety_blocked():
    coordinator = _coordinator()
    device = coordinator._model.get_device("d1")
    assert device is not None
    device.is_on = True
    coordinator.hass.states.get.side_effect = lambda entity_id: _state("off", {})
    coordinator._turn_off_device = AsyncMock(side_effect=RuntimeError("relay unavailable"))

    await coordinator._perform_emergency_all_stop()

    assert coordinator.status == STATUS_SAFETY_BLOCKED
    assert device.device_id in coordinator._faulted
    assert device.is_on is None
