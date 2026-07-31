"""Tests for coordinator logic — mock HA dependencies."""
from datetime import datetime, timezone
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components"))

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from power_orchestrator.power_model import ManagedDevice, PowerModel
from power_orchestrator.coordinator import PowerOrchestratorCoordinator
from power_orchestrator.const import (
    MODE_AUTO, MODE_OFF, GRID_LOSS_MODE_SENSOR, GRID_LOSS_MODE_THRESHOLD,
    STATUS_MONITORING, STATUS_LOAD_SHEDDING, STATUS_GRID_LOSS, STATUS_ADDING_LOAD,
)


def make_mock_hass():
    hass = MagicMock()
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    hass.services.async_call = AsyncMock()
    return hass


def make_mock_store():
    store = MagicMock()
    store.set_pause = MagicMock()
    store.clear_pause = MagicMock()
    store.async_save = AsyncMock()
    return store


def make_model():
    m = PowerModel()
    m.add_device(ManagedDevice(device_id="high", name="High", entity_id="switch.high", expected_power=1000, priority=1))
    m.add_device(ManagedDevice(device_id="mid", name="Mid", entity_id="switch.mid", expected_power=2000, priority=2))
    m.add_device(ManagedDevice(device_id="low", name="Low", entity_id="switch.low", expected_power=3000, priority=3))
    return m


def make_coordinator(hass=None, model=None, **kwargs):
    if hass is None:
        hass = make_mock_hass()
    if model is None:
        model = make_model()
    store = make_mock_store()
    c = PowerOrchestratorCoordinator(
        hass=hass,
        model=model,
        store=store,
        load_sensor=kwargs.get("load_sensor", "sensor.load"),
        max_load=kwargs.get("max_load", 5000),
        averaging_period=kwargs.get("averaging_period", 10),
        safety_reserve=kwargs.get("safety_reserve", 200),
        hysteresis=kwargs.get("hysteresis", 200),
        pause_period=kwargs.get("pause_period", 60),
        grid_loss_mode=kwargs.get("grid_loss_mode", GRID_LOSS_MODE_SENSOR),
        grid_loss_sensor=kwargs.get("grid_loss_sensor", None),
        battery_threshold=kwargs.get("battery_threshold", None),
        battery_soc_sensor=kwargs.get("battery_soc_sensor", None),
        solar_forecast_entity=kwargs.get("solar_forecast_entity", None),
        solar_production_entity=kwargs.get("solar_production_entity", None),
    )
    # Legacy unit fixtures model an explicitly armed coordinator; startup-safe
    # behavior is covered by dedicated setup/latch tests.
    c._startup_safe = False
    c.mode = MODE_AUTO
    c._state_is_fresh = lambda state: True
    c._load_sensor_valid = True

    async def confirm(device, expected_state, *, operation_id, command_issued_at, pre_reported_at):
        c._last_confirmed_reported_at[device.device_id] = command_issued_at + 0.001
        return True

    c._confirm_device_state = AsyncMock(side_effect=confirm)
    return c


# ── Mode tests ─────────────────────────────────────────────────────


def test_default_mode():
    c = make_coordinator()
    assert c.mode == MODE_AUTO


def test_set_mode():
    c = make_coordinator()
    c.mode = MODE_OFF
    assert c.mode == MODE_OFF
    assert "Mode changed to off" in c.last_action or "off" in c.last_action


@pytest.mark.asyncio
async def test_async_set_mode_save_failure_forces_off():
    c = make_coordinator()
    c.mode = MODE_OFF
    c._store.async_save = AsyncMock(side_effect=OSError("disk full"))

    with pytest.raises(OSError):
        await c.async_set_mode(MODE_AUTO)

    assert c.mode == MODE_OFF
    c._store.set_mode.assert_called_with(MODE_OFF)


@pytest.mark.asyncio
async def test_startup_safe_latch_blocks_start_until_explicit_arm():
    c = make_coordinator()
    c._startup_safe = True
    c.mode = MODE_AUTO
    device = c._model.get_device("high")
    device.is_on = False

    assert await c._turn_on_device(device) is False
    c.hass.services.async_call.assert_not_awaited()

    c._startup_safe = False
    c._reserve_pending_start(device)
    assert await c._turn_on_device(device) is True
    c.hass.services.async_call.assert_awaited_once()


# ── Grid OK tests ──────────────────────────────────────────────────


def test_grid_ok_no_sensor():
    """Missing grid loss sensor is unsafe and blocks operation."""
    c = make_coordinator()
    assert c.grid_ok is False


def test_grid_ok_sensor_on():
    """Binary sensor ON = grid OK."""
    c = make_coordinator(grid_loss_sensor="binary_sensor.grid")
    hass = c.hass
    state = MagicMock()
    state.state = "on"
    hass.states.get.return_value = state
    assert c.grid_ok is True


def test_grid_ok_sensor_off():
    """Binary sensor OFF = grid lost."""
    c = make_coordinator(grid_loss_sensor="binary_sensor.grid")
    hass = c.hass
    state = MagicMock()
    state.state = "off"
    hass.states.get.return_value = state
    assert c.grid_ok is False


def test_grid_ok_threshold_above():
    c = make_coordinator(
        grid_loss_mode=GRID_LOSS_MODE_THRESHOLD,
        battery_soc_sensor="sensor.battery_soc",
        battery_threshold=20,
    )
    state = MagicMock()
    state.state = "45"
    state.attributes = {"unit_of_measurement": "%"}
    c.hass.states.get.return_value = state
    assert c.grid_ok is True


def test_grid_ok_threshold_below():
    c = make_coordinator(
        grid_loss_mode=GRID_LOSS_MODE_THRESHOLD,
        battery_soc_sensor="sensor.battery_soc",
        battery_threshold=20,
    )
    state = MagicMock()
    state.state = "15"
    c.hass.states.get.return_value = state
    assert c.grid_ok is False


# ── Solar forecast tests ───────────────────────────────────────────


def test_solar_forecast_ok_no_forecast():
    """No forecast entity = can't enable solar-only devices."""
    c = make_coordinator()
    d = ManagedDevice(device_id="d1", name="D1", entity_id="switch.d1", expected_power=2000, only_from_solar=True)
    assert c._solar_forecast_ok(d) is False


def test_solar_forecast_ok_not_required():
    """only_from_solar=False = always OK."""
    c = make_coordinator()
    d = ManagedDevice(device_id="d1", name="D1", entity_id="switch.d1", expected_power=2000, only_from_solar=False)
    assert c._solar_forecast_ok(d) is True


def test_solar_forecast_ok_sufficient():
    c = make_coordinator(solar_forecast_entity="sensor.forecast")
    state = MagicMock()
    state.state = "3.5"  # 3.5 kW
    state.attributes = {"unit_of_measurement": "kW"}
    state.last_reported = datetime.now(timezone.utc)
    state.last_updated = datetime.now(timezone.utc)
    c.hass.states.get.return_value = state
    d = ManagedDevice(device_id="d1", name="D1", entity_id="switch.d1", expected_power=2000, only_from_solar=True)
    # forecast 3.5kW >= 2.0kW
    assert c._solar_forecast_ok(d) is True


def test_solar_forecast_ok_insufficient():
    c = make_coordinator(solar_forecast_entity="sensor.forecast")
    state = MagicMock()
    state.state = "1.2"  # 1.2 kW
    c.hass.states.get.return_value = state
    d = ManagedDevice(device_id="d1", name="D1", entity_id="switch.d1", expected_power=2000, only_from_solar=True)
    # forecast 1.2kW < 2.0kW
    assert c._solar_forecast_ok(d) is False


# ── Load sensor tests ──────────────────────────────────────────────


def test_read_load_sensor():
    c = make_coordinator()
    state = MagicMock()
    state.state = "3500"
    state.attributes = {"unit_of_measurement": "W"}
    c.hass.states.get.return_value = state
    assert c._read_load_sensor() == 3500.0


def test_read_load_sensor_unavailable():
    c = make_coordinator()
    c.hass.states.get.return_value = None
    assert c._read_load_sensor() == 0.0


# ── Average load tests ──────────────────────────────────────────────


def test_average_load_empty():
    c = make_coordinator()
    assert c.average_load is None
    assert c.current_load is None


def test_invalid_load_properties_are_unknown_not_zero():
    c = make_coordinator()
    c._load_samples.extend([3000])
    c._load_sensor_valid = False
    c._load_sensor_reason = "unavailable_or_stale"

    assert c.current_load is None
    assert c.average_load is None
    assert c.available_capacity is None


def test_average_load_with_samples():
    c = make_coordinator()
    c._load_samples.extend([1000, 2000, 3000])
    assert c.average_load == 2000.0
    assert c.current_load == 3000.0


# ── Available capacity tests ────────────────────────────────────────


def test_available_capacity():
    c = make_coordinator(max_load=5000, safety_reserve=200)
    c._load_samples.extend([3000])
    # capacity = 5000 - 3000 - 200 = 1800
    assert c.available_capacity == 1800.0


def test_available_capacity_negative():
    c = make_coordinator(max_load=5000, safety_reserve=200)
    c._load_samples.extend([6000])
    assert c.available_capacity == 0.0


# ── Device control tests ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_turn_on_device():
    c = make_coordinator()
    d = c._model.get_device("high")
    d.is_on = False
    c._reserve_pending_start(d)
    await c._turn_on_device(d)
    c.hass.services.async_call.assert_called_with(
        "switch", "turn_on", {"entity_id": "switch.high"}, blocking=True
    )
    assert d.is_on is True


@pytest.mark.asyncio
async def test_turn_off_device():
    c = make_coordinator()
    d = c._model.get_device("high")
    d.is_on = True
    await c._turn_off_device(d)
    c.hass.services.async_call.assert_called_with(
        "switch", "turn_off", {"entity_id": "switch.high"}, blocking=True
    )
    assert d.is_on is False


# ── Force evaluate ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_force_evaluate():
    c = make_coordinator(grid_loss_sensor="binary_sensor.grid")

    def get_state(entity_id):
        if entity_id == "binary_sensor.grid":
            s = MagicMock()
            s.state = "on"
            return s
        if entity_id == "sensor.load":
            s = MagicMock()
            s.state = "1000"
            s.attributes = {"unit_of_measurement": "W"}
            return s
        s = MagicMock()
        s.state = "off"
        return s

    c.hass.states.get.side_effect = get_state
    await c.async_force_evaluate()
    assert c.status in (STATUS_MONITORING, STATUS_ADDING_LOAD)


# ── Add load tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_perform_adding_enough_capacity():
    c = make_coordinator(max_load=5000, safety_reserve=200, hysteresis=200)
    c._load_samples.extend([1000])  # load = 1000
    c._model.get_device("high").is_on = False
    c._model.get_device("mid").is_on = False
    c._model.get_device("low").is_on = False

    # capacity = 5000 - 200 - 1000 - 200 = 3600
    # high device expected 1000W, fits
    await c._perform_adding(avg_load=1000, capacity=3600)
    assert c._model.get_device("high").is_on is True  # highest priority turned on


@pytest.mark.asyncio
async def test_perform_adding_not_enough_capacity():
    c = make_coordinator(max_load=5000, safety_reserve=200, hysteresis=200)
    c._load_samples.extend([4500])  # load = 4500
    c._model.get_device("high").is_on = False
    c._model.get_device("mid").is_on = False
    c._model.get_device("low").is_on = False

    # capacity = 5000 - 200 - 4500 - 200 = 100
    # high device expected 1000W, doesn't fit
    await c._perform_adding(avg_load=4500, capacity=100)
    assert c._model.get_device("high").is_on is False  # not turned on


@pytest.mark.asyncio
async def test_perform_adding_only_solar_blocked():
    c = make_coordinator(max_load=5000, solar_forecast_entity="sensor.forecast")
    state = MagicMock()
    state.state = "0.5"  # 0.5 kW forecast, not enough
    c.hass.states.get.return_value = state
    c._load_samples.extend([1000])

    high = c._model.get_device("high")
    high.only_from_solar = True
    high.is_on = False

    # capacity = 5000 - 200 - 1000 - 200 = 3600, but solar rule blocks
    await c._perform_adding(avg_load=1000, capacity=3600)
    assert high.is_on is False  # blocked by solar rule


# ── Load shedding tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_perform_shedding_overload():
    c = make_coordinator(max_load=5000)
    c._model.get_device("high").is_on = True
    c._model.get_device("mid").is_on = True
    c._model.get_device("low").is_on = True

    await c._perform_shedding(avg_load=6000)
    # Lowest priority should be turned off
    assert c._model.get_device("low").is_on is False  # lowest priority
    # Others should still be on (one at a time)
    assert c._model.get_device("high").is_on is True
    assert c._model.get_device("mid").is_on is True


@pytest.mark.asyncio
async def test_perform_shedding_only_one():
    c = make_coordinator(max_load=5000)
    c._model.get_device("high").is_on = True
    c._model.get_device("mid").is_on = False
    c._model.get_device("low").is_on = False

    await c._perform_shedding(avg_load=6000)
    # Only high is on, it should be turned off
    assert c._model.get_device("high").is_on is False


# ── Grid loss handling tests ────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_grid_loss_turns_off_all():
    c = make_coordinator()
    c._model.get_device("high").is_on = True
    c._model.get_device("mid").is_on = True
    c._model.get_device("low").is_on = True

    await c._handle_grid_loss()
    assert c._model.get_device("high").is_on is False
    assert c._model.get_device("mid").is_on is False
    assert c._model.get_device("low").is_on is False


# ── Full evaluation tests ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluate_monitoring():
    """Normal load near limit = monitoring (no capacity to add)."""
    c = make_coordinator(
        grid_loss_sensor="binary_sensor.grid",
        max_load=5000,
        safety_reserve=200,
        hysteresis=200,
    )

    def get_state(entity_id):
        if entity_id == "binary_sensor.grid":
            s = MagicMock()
            s.state = "on"
            return s
        if entity_id == "sensor.load":
            s = MagicMock()
            s.state = "4800"
            s.attributes = {"unit_of_measurement": "W"}
            return s
        for device in c._model.all_devices():
            if entity_id == device.entity_id:
                s = MagicMock()
                s.state = "off"
                return s
        return None
    c.hass.states.get.side_effect = get_state

    await c._evaluate()
    assert c.status == STATUS_MONITORING


@pytest.mark.asyncio
async def test_evaluate_grid_loss():
    """Grid off = grid loss status."""
    c = make_coordinator(grid_loss_sensor="binary_sensor.grid")
    # Grid sensor = off
    grid_state = MagicMock()
    grid_state.state = "off"
    c.hass.states.get.return_value = grid_state

    await c._evaluate()
    assert c.status == STATUS_GRID_LOSS


@pytest.mark.asyncio
async def test_evaluate_load_shedding():
    """Over limit = load shedding."""
    c = make_coordinator(
        grid_loss_sensor="binary_sensor.grid",
        max_load=5000,
        averaging_period=1,
    )
    # Use side_effect to return different values for different entities
    def get_state(entity_id):
        if entity_id == "binary_sensor.grid":
            s = MagicMock()
            s.state = "on"
            return s
        if entity_id == "sensor.load":
            s = MagicMock()
            s.state = "6000"
            s.attributes = {"unit_of_measurement": "W"}
            return s
        for device in c._model.all_devices():
            if entity_id == device.entity_id:
                s = MagicMock()
                s.state = "on"
                return s
        return None
    c.hass.states.get.side_effect = get_state

    await c._evaluate()
    assert c.status == STATUS_LOAD_SHEDDING