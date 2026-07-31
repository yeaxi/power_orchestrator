"""Forecast.Solar estimated-power resolution and solar-only admission tests."""
from datetime import datetime, timedelta, timezone
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components"))

from power_orchestrator.config_flow import (
    PowerOrchestratorConfigFlow,
    _discover_energy,
)
from power_orchestrator.coordinator import PowerOrchestratorCoordinator
from power_orchestrator.forecast import (
    current_power_forecast_w,
    resolve_current_power_forecast_entity,
)
from power_orchestrator.power_model import ManagedDevice, PowerModel


NOW = datetime(2026, 7, 30, 12, 30, tzinfo=timezone.utc)


def make_state(
    value,
    unit=None,
    *,
    age=timedelta(minutes=10),
    reported_at=NOW,
    last_updated=None,
):
    attributes = {}
    if unit is not None:
        attributes["unit_of_measurement"] = unit
    last_reported = (
        reported_at - age if reported_at is not None else None
    )
    return SimpleNamespace(
        state=str(value),
        attributes=attributes,
        last_reported=last_reported,
        last_updated=last_updated,
    )


def test_current_power_forecast_w_accepts_power_units():
    assert current_power_forecast_w(make_state(2000, "W"), now=NOW) == 2000.0
    assert current_power_forecast_w(make_state(2, "kW"), now=NOW) == 2000.0


@pytest.mark.parametrize("unit", ["Wh", "kWh", "V", None])
def test_current_power_forecast_w_rejects_energy_or_missing_units(unit):
    assert current_power_forecast_w(make_state(2000, unit), now=NOW) is None


@pytest.mark.parametrize(
    "state",
    [
        None,
        make_state("unknown", "W"),
        make_state("unavailable", "W"),
        make_state("not-a-number", "W"),
        make_state("nan", "W"),
        make_state("inf", "W"),
        make_state(-1, "W"),
        make_state(2000, "W", age=timedelta(minutes=75, seconds=1)),
        make_state(2000, "W", reported_at=None),
        make_state(
            2000,
            "W",
            age=timedelta(0),
            reported_at=datetime(2026, 7, 30, 12, 0),
        ),
        make_state(
            2000,
            "W",
            age=timedelta(0),
            reported_at=NOW + timedelta(seconds=1),
        ),
    ],
)
def test_current_power_forecast_w_fails_closed_for_invalid_or_stale_state(state):
    assert current_power_forecast_w(state, now=NOW) is None


def test_current_power_forecast_w_requires_same_clock_hour():
    previous_hour = NOW.replace(hour=11, minute=59)
    state = make_state(2000, "W", age=timedelta(0), reported_at=previous_hour)
    assert current_power_forecast_w(state, now=NOW) is None


def test_current_power_forecast_w_uses_last_reported_not_last_updated():
    state = make_state(
        2000,
        "W",
        last_updated=NOW - timedelta(hours=4),
    )
    assert current_power_forecast_w(state, now=NOW) == 2000.0


def _registry_entry(
    entity_id,
    entry_id="forecast-entry",
    name="",
    *,
    unique_id=None,
    platform="forecast_solar",
    disabled_by=None,
):
    return SimpleNamespace(
        entity_id=entity_id,
        config_entry_id=entry_id,
        original_name=name,
        translation_key="",
        unique_id=unique_id or f"{entry_id}_power_production_now",
        platform=platform,
        disabled_by=disabled_by,
    )


def test_resolve_current_power_forecast_entity_allows_renamed_exact_entity():
    hass = MagicMock()
    registry = SimpleNamespace(
        entities={
            "renamed": _registry_entry(
                "sensor.my_estimated_power",
                name="Energy current hour",
            ),
            "energy": _registry_entry(
                "sensor.energy_current_hour",
                name="Energy current hour",
                unique_id="forecast-entry_energy_current_hour",
            ),
            "next": _registry_entry(
                "sensor.power_production_next_hour",
                name="Next hour power",
                unique_id="forecast-entry_power_production_next_hour",
            ),
            "daily": _registry_entry(
                "sensor.energy_production_today",
                name="Power production now",
                unique_id="forecast-entry_energy_production_today",
            ),
        }
    )
    with patch(
        "power_orchestrator.forecast.async_get_entity_registry",
        return_value=registry,
    ):
        assert (
            resolve_current_power_forecast_entity(hass, "forecast-entry")
            == "sensor.my_estimated_power"
        )


def test_resolve_current_power_forecast_entity_rejects_wrong_identity_and_disabled():
    hass = MagicMock()
    registry = SimpleNamespace(
        entities={
            "other_entry": _registry_entry(
                "sensor.other_now",
                entry_id="other-entry",
            ),
            "wrong_platform": _registry_entry(
                "sensor.wrong_platform",
                platform="some_other_integration",
            ),
            "disabled": _registry_entry(
                "sensor.disabled_now",
                disabled_by="user",
            ),
            "arbitrary": _registry_entry(
                "sensor.solar_forecast_today",
                unique_id="forecast-entry_energy_production_today",
            ),
        }
    )
    with patch(
        "power_orchestrator.forecast.async_get_entity_registry",
        return_value=registry,
    ):
        assert resolve_current_power_forecast_entity(hass, "forecast-entry") is None


def test_resolve_current_power_forecast_entity_rejects_ambiguous_registry():
    hass = MagicMock()
    registry = SimpleNamespace(
        entities={
            "first": _registry_entry("sensor.first_now"),
            "second": _registry_entry("sensor.second_now"),
        }
    )
    with patch(
        "power_orchestrator.forecast.async_get_entity_registry",
        return_value=registry,
    ):
        assert resolve_current_power_forecast_entity(hass, "forecast-entry") is None


@pytest.mark.asyncio
async def test_energy_discovery_wires_exact_estimated_power_entity():
    hass = MagicMock()
    hass.entity_registry = SimpleNamespace(
        entities={
            "current": _registry_entry("sensor.renamed_estimated_power"),
            "energy": _registry_entry(
                "sensor.energy_current_hour",
                unique_id="forecast-entry_energy_current_hour",
            ),
        }
    )
    states = {
        "sensor.renamed_estimated_power": make_state(2000, "W"),
        "sensor.energy_current_hour": make_state(2, "kWh"),
    }
    hass.states.get.side_effect = states.get

    async def mock_get_manager(_hass):
        manager = MagicMock()
        manager.data = {
            "energy_sources": [
                {
                    "type": "solar",
                    "stat_rate": "sensor.pv_power",
                    "config_entry_solar_forecast": ["forecast-entry"],
                }
            ],
            "device_consumption": [],
        }
        return manager

    with patch("homeassistant.components.energy.async_get_manager", mock_get_manager):
        result = await _discover_energy(hass)

    assert result["solar_forecast_entry"] == "forecast-entry"
    assert result["solar_forecast_entity"] == "sensor.renamed_estimated_power"


def _current_hour_runtime_state(value, unit):
    now = datetime.now().astimezone()
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    return make_state(value, unit, age=timedelta(0), reported_at=hour_start)


def _make_runtime(forecast_entity="sensor.forecast", state=None):
    hass = MagicMock()
    hass.states.get.return_value = state
    hass.services.async_call = AsyncMock()
    model = PowerModel()
    model.add_device(
        ManagedDevice(
            device_id="d1",
            name="Solar device",
            entity_id="switch.d1",
            expected_power=2000,
            only_from_solar=True,
        )
    )
    store = MagicMock()
    return PowerOrchestratorCoordinator(
        hass=hass,
        model=model,
        store=store,
        load_sensor="sensor.load",
        max_load=5000,
        averaging_period=10,
        safety_reserve=200,
        hysteresis=200,
        pause_period=60,
        grid_loss_mode="grid_loss_sensor",
        grid_loss_sensor=None,
        battery_threshold=None,
        battery_soc_sensor=None,
        solar_forecast_entity=forecast_entity,
        solar_production_entity="sensor.pv_power",
    )


@pytest.mark.parametrize(
    ("unit", "value", "expected"),
    [
        ("W", 2000, True),
        ("kW", 2, True),
        ("W", 1999, False),
        ("Wh", 2000, False),
        ("kWh", 2, False),
    ],
)
def test_only_from_solar_runtime_uses_estimated_power_forecast(
    unit, value, expected
):
    coordinator = _make_runtime(
        state=_current_hour_runtime_state(value, unit)
    )
    device = coordinator._model.get_device("d1")
    assert coordinator._solar_forecast_ok(device) is expected


def test_only_from_solar_runtime_actual_pv_cannot_rescue_invalid_forecast():
    forecast_state = _current_hour_runtime_state(10000, "Wh")
    actual_pv_state = _current_hour_runtime_state(10000, "W")
    coordinator = _make_runtime(state=forecast_state)
    coordinator.hass.states.get.side_effect = lambda entity_id: (
        forecast_state if entity_id == "sensor.forecast" else actual_pv_state
    )
    assert coordinator._solar_forecast_ok(coordinator._model.get_device("d1")) is False


@pytest.mark.asyncio
async def test_setup_re_resolves_entity_and_ignores_stale_persisted_id():
    from power_orchestrator import async_setup_entry

    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    entry = SimpleNamespace(
        entry_id="orchestrator-entry",
        data={
            "devices": [],
            "solar_forecast_entry": "forecast-entry",
            "solar_forecast_entity": "sensor.old_entity_id",
        },
        async_on_unload=MagicMock(),
    )
    runtime_store = MagicMock()
    runtime_store.async_load = AsyncMock()
    coordinator = MagicMock()
    coordinator.async_config_entry_first_refresh = AsyncMock()

    with (
        patch("power_orchestrator.Store"),
        patch("power_orchestrator.RuntimeStore", return_value=runtime_store),
        patch(
            "power_orchestrator._resolve_forecast_entity",
            return_value="sensor.renamed_estimated_power",
        ) as resolver,
        patch(
            "power_orchestrator.PowerOrchestratorCoordinator",
            return_value=coordinator,
        ) as coordinator_factory,
        patch("power_orchestrator._register_services", new=AsyncMock()),
    ):
        assert await async_setup_entry(hass, entry) is True

    resolver.assert_called_once_with(hass, "forecast-entry")
    assert coordinator_factory.call_args.kwargs["solar_forecast_entity"] == (
        "sensor.renamed_estimated_power"
    )



def test_setup_resolver_uses_shared_exact_entity_helper():
    from power_orchestrator import _resolve_forecast_entity

    hass = MagicMock()
    with patch(
        "power_orchestrator._resolve_forecast_entity_shared",
        return_value="sensor.renamed_estimated_power",
    ) as resolver:
        assert (
            _resolve_forecast_entity(hass, "forecast-entry")
            == "sensor.renamed_estimated_power"
        )
    resolver.assert_called_once_with(hass, "forecast-entry")


@pytest.mark.asyncio
async def test_config_flow_uses_ha_2026_config_entry_selector_key():
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    with patch(
        "power_orchestrator.config_flow._discover_energy",
        new=AsyncMock(
            return_value={
                "grid_power": None,
                "solar_power": None,
                "solar_forecast_entity": None,
                "solar_forecast_entry": None,
                "battery_soc": None,
                "battery_power": None,
                "devices": [],
            }
        ),
    ):
        result = await flow.async_step_user()

    key = next(key for key in result["data_schema"].schema if str(key) == "solar_forecast")
    config = result["data_schema"].schema[key].config
    assert getattr(config, "integration") == "forecast_solar"
