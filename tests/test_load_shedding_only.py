"""Regression contract tests for the load-shedding-only controller."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from power_orchestrator import async_setup_entry
from power_orchestrator.const import (
    CONF_GRID_LOSS_MODE,
    CONF_LOAD_SENSOR,
    GRID_LOSS_MODE_SENSOR,
    MODE_AUTO,
    MODE_OFF,
)


ROOT = Path(__file__).parents[1]
INTEGRATION = ROOT / "custom_components" / "power_orchestrator"


@pytest.mark.asyncio
@pytest.mark.parametrize(("storage_invalid", "expected_mode"), [(False, MODE_AUTO), (True, MODE_OFF)])
async def test_persisted_mode_is_restored_only_when_safety_storage_is_valid(
    storage_invalid: bool,
    expected_mode: str,
) -> None:
    """A persisted planner mode is the restart contract for the auto/off switch."""
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.bus.async_listen = MagicMock(return_value="unsubscribe")
    entry = SimpleNamespace(
        entry_id="entry-auto-restart",
        data={
            CONF_LOAD_SENSOR: "sensor.load",
            CONF_GRID_LOSS_MODE: GRID_LOSS_MODE_SENSOR,
        },
        options={},
        async_on_unload=MagicMock(),
        add_update_listener=MagicMock(return_value="update-unsubscribe"),
    )
    runtime_store = MagicMock()
    runtime_store.async_load = AsyncMock()
    runtime_store.safety_storage_invalid = storage_invalid
    runtime_store.restore_mode.return_value = MODE_AUTO
    runtime_store.restore_execution_mode.return_value = None
    runtime_store.restore_device_runtime.return_value = (set(), set())
    runtime_store.restore_fault_reasons.return_value = {}
    runtime_store.restore_fault_notification_state.return_value = ({}, {})
    runtime_store.unresolved_actions.return_value = []
    runtime_store.action_journal_invalid = False
    coordinator = MagicMock()
    coordinator.async_config_entry_first_refresh = AsyncMock()

    with (
        patch("power_orchestrator.Store"),
        patch("power_orchestrator.RuntimeStore", return_value=runtime_store),
        patch("power_orchestrator.PowerOrchestratorCoordinator", return_value=coordinator),
        patch("power_orchestrator._register_services", new=AsyncMock()),
    ):
        assert await async_setup_entry(hass, entry) is True

    assert coordinator.mode == expected_mode


@pytest.mark.asyncio
async def test_restored_auto_mode_cannot_remain_in_observe_execution() -> None:
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.bus.async_listen = MagicMock(return_value="unsubscribe")
    entry = SimpleNamespace(
        entry_id="entry-auto-live-boundary",
        data={
            CONF_LOAD_SENSOR: "sensor.load",
            CONF_GRID_LOSS_MODE: GRID_LOSS_MODE_SENSOR,
        },
        options={},
        async_on_unload=MagicMock(),
        add_update_listener=MagicMock(return_value="update-unsubscribe"),
    )
    runtime_store = MagicMock()
    runtime_store.async_load = AsyncMock()
    runtime_store.async_save = AsyncMock()
    runtime_store.safety_storage_invalid = False
    runtime_store.restore_mode.return_value = MODE_AUTO
    runtime_store.restore_execution_mode.return_value = "observe"
    runtime_store.restore_device_runtime.return_value = (set(), set())
    runtime_store.restore_fault_reasons.return_value = {}
    runtime_store.restore_fault_notification_state.return_value = ({}, {})
    runtime_store.unresolved_actions.return_value = []
    runtime_store.action_journal_invalid = False
    coordinator = MagicMock()
    coordinator.async_config_entry_first_refresh = AsyncMock()
    coordinator._save_runtime_snapshot = MagicMock()
    coordinator.execution_mode = "observe"
    coordinator.mode = MODE_AUTO

    with (
        patch("power_orchestrator.Store"),
        patch("power_orchestrator.RuntimeStore", return_value=runtime_store),
        patch("power_orchestrator.PowerOrchestratorCoordinator", return_value=coordinator),
        patch("power_orchestrator._register_services", new=AsyncMock()),
    ):
        assert await async_setup_entry(hass, entry) is True

    runtime_store.set_execution_mode.assert_called_with("live")
    runtime_store.async_save.assert_awaited()


def test_integration_has_no_pv_admission_or_normal_start_surface() -> None:
    """PV priority and normal load starts must not remain in production code."""
    production_files = (
        "__init__.py",
        "config_flow.py",
        "const.py",
        "coordinator.py",
        "power_model.py",
        "policy.py",
        "services.yaml",
        "strings.json",
        "translations/en.json",
        "translations/uk.json",
    )
    source = "\n".join((INTEGRATION / name).read_text() for name in production_files)

    forbidden_tokens = (
        "only_from_solar",
        "solar_forecast",
        "Forecast.Solar",
        "current_power_forecast_w",
        "request_start",
        "async_request_start",
        "_perform_adding",
        "turn_on",
    )
    for token in forbidden_tokens:
        assert token not in source, token

    assert not (INTEGRATION / "forecast.py").exists()


def test_config_flow_has_no_pv_or_forecast_fields() -> None:
    """The onboarding/options contract should only describe shedding inputs."""
    source = (INTEGRATION / "config_flow.py").read_text()
    for field in ("solar_power", "solar_forecast", "solar_forecast_entry"):
        assert field not in source
