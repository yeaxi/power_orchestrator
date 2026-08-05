"""Lifecycle, migration, and service-boundary tests for the stop-only integration."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import power_orchestrator as integration
from power_orchestrator.const import DOMAIN, MODE_AUTO, MODE_OFF


def _hass_with_services(*, runtime=None):
    services = SimpleNamespace(
        has_service=MagicMock(return_value=False),
        async_register=MagicMock(),
        async_remove=MagicMock(),
    )
    data = {DOMAIN: {"entry-1": runtime}} if runtime is not None else {DOMAIN: {}}
    return SimpleNamespace(data=data, services=services)


def test_normalize_devices_rejects_duplicates_and_preserves_only_shedding_fields() -> None:
    devices = integration._normalize_devices(
        [
            {
                "device_id": " d1 ",
                "name": "Load 1",
                "entity": "switch.load_1",
                "expected_power": "1200",
                "power_sensor": "sensor.load_1",
                "actuators": ["light.load_1", "invalid"],
                "priority": 2,
                "shed_priority": 1,
                "only_from_solar": True,
            },
            {"device_id": "d1", "entity": "switch.other", "expected_power": 1},
            {"device_id": "bad", "entity": "sensor.not_a_control", "expected_power": 1},
            "not-a-record",
        ]
    )
    assert devices == [
        {
            "device_id": "d1",
            "name": "Load 1",
            "entity": "switch.load_1",
            "expected_power": 1200,
            "power_sensor": "sensor.load_1",
            "priority": 2,
            "shed_priority": 1,
            "actuators": ["light.load_1"],
        }
    ]


@pytest.mark.asyncio
async def test_setup_and_migration_initialize_registry_and_drop_unknown_fields() -> None:
    hass = SimpleNamespace(data={})
    assert await integration.async_setup(hass, {}) is True
    assert DOMAIN in hass.data
    assert f"{DOMAIN}_lifecycle" in hass.data

    updater = MagicMock()
    hass.config_entries = SimpleNamespace(async_update_entry=updater)
    entry = SimpleNamespace(
        entry_id="entry-1",
        version=1,
        minor_version=0,
        data={
            "load_sensor": "sensor.load",
            "obsolete_activation_field": True,
            "devices": [{"device_id": "d1", "entity": "switch.d1", "legacy": "remove"}],
        },
        options={
            "load_sensor": "sensor.load",
            "solar_forecast_entry": "legacy-entry",
            "solar_power": "sensor.pv",
            "devices": [
                {
                    "device_id": "d1",
                    "entity": "switch.d1",
                    "only_from_solar": True,
                }
            ],
        },
    )
    assert await integration.async_migrate_entry(hass, entry) is True
    updated = updater.call_args.kwargs
    assert "obsolete_activation_field" not in updated["data"]
    assert "legacy" not in updated["data"]["devices"][0]
    assert "solar_forecast_entry" not in updated["options"]
    assert "solar_power" not in updated["options"]
    assert "only_from_solar" not in updated["options"]["devices"][0]
    assert updated["version"] == 2
    assert updated["minor_version"] == 1


@pytest.mark.asyncio
async def test_setup_entry_is_singleton_and_cleans_failed_setup() -> None:
    active = SimpleNamespace(coordinator=object())
    hass = SimpleNamespace(data={DOMAIN: {"active": active}})
    entry = SimpleNamespace(entry_id="entry-2", runtime_data=None)
    assert await integration.async_setup_entry(hass, entry) is False

    hass = SimpleNamespace(data={DOMAIN: {"entry-2": object()}})
    entry.runtime_data = None
    with patch.object(integration, "_async_setup_entry_impl", side_effect=RuntimeError("boom")):
        assert await integration.async_setup_entry(hass, entry) is False
    assert entry.runtime_data is None
    assert "entry-2" not in hass.data[DOMAIN]


def test_update_listener_remover_is_owned_by_entry_unload() -> None:
    remove = MagicMock()
    entry = SimpleNamespace(
        add_update_listener=MagicMock(return_value=remove),
        async_on_unload=MagicMock(),
    )

    integration._register_entry_update_listener(entry)

    entry.add_update_listener.assert_called_once_with(integration._async_update_listener)
    entry.async_on_unload.assert_called_once_with(remove)


def test_listener_remover_is_idempotent() -> None:
    remove = MagicMock()
    wrapped = integration._idempotent_remover(remove)

    wrapped()
    wrapped()

    remove.assert_called_once_with()


@pytest.mark.asyncio
async def test_unload_persists_runtime_and_unregisters_services() -> None:
    coordinator = SimpleNamespace(async_persist_runtime=AsyncMock())
    runtime = SimpleNamespace(coordinator=coordinator, repair_listener_remove=MagicMock())
    entry = SimpleNamespace(entry_id="entry-1", runtime_data=runtime)
    config_entries = SimpleNamespace(async_unload_platforms=AsyncMock(return_value=True))
    hass = _hass_with_services(runtime=runtime)
    hass.config_entries = config_entries
    result = await integration.async_unload_entry(hass, entry)
    assert result is True
    coordinator.async_persist_runtime.assert_awaited_once()
    config_entries.async_unload_platforms.assert_awaited_once()
    runtime.repair_listener_remove.assert_called_once()
    assert entry.runtime_data is None
    assert hass.services.async_remove.call_count == 6


@pytest.mark.asyncio
async def test_service_registration_exposes_only_safe_handlers() -> None:
    coordinator = SimpleNamespace(
        async_force_evaluate=AsyncMock(),
        async_set_mode=AsyncMock(),
        async_request_stop=AsyncMock(),
        async_clear_quarantine=AsyncMock(),
        async_set_execution_mode=AsyncMock(),
        async_authorize_shedding=AsyncMock(),
    )
    runtime = SimpleNamespace(coordinator=coordinator)
    hass = _hass_with_services(runtime=runtime)
    await integration._register_services(hass)
    registered = {
        call.args[1]: call.args[2] for call in hass.services.async_register.call_args_list
    }
    assert set(registered) == {
        "force_evaluate",
        "set_mode",
        "request_stop",
        "clear_quarantine",
        "set_execution_mode",
        "authorize_shedding",
    }

    await registered["force_evaluate"](SimpleNamespace(data={}))
    await registered["set_mode"](SimpleNamespace(data={"mode": MODE_AUTO}))
    call = SimpleNamespace(
        data={"device_id": "d1", "source": "test"}, context=SimpleNamespace(user_id="u", id="c")
    )
    await registered["request_stop"](call)
    await registered["clear_quarantine"](call)
    await registered["set_execution_mode"](
        SimpleNamespace(data={"execution_mode": "observe", "confirm_live": False})
    )
    await registered["authorize_shedding"](
        SimpleNamespace(
            data={"device_ids": ["d1", "d2"], "confirm_takeover": True},
            context=SimpleNamespace(user_id="u", id="c"),
        )
    )
    coordinator.async_force_evaluate.assert_awaited_once()
    coordinator.async_set_mode.assert_awaited_once_with(MODE_AUTO)
    coordinator.async_request_stop.assert_awaited_once_with(
        "d1", source="test", actor_id="u", context_id="c"
    )
    coordinator.async_clear_quarantine.assert_awaited_once_with(
        "d1", source="test", actor_id="u", context_id="c"
    )
    coordinator.async_set_execution_mode.assert_awaited_once_with("observe", confirm_live=False)
    coordinator.async_authorize_shedding.assert_awaited_once_with(
        ["d1", "d2"], confirm_takeover=True
    )


@pytest.mark.asyncio
async def test_service_registration_short_circuits_when_already_registered() -> None:
    hass = _hass_with_services()
    hass.services.has_service.return_value = True
    await integration._register_services(hass)
    hass.services.async_register.assert_not_called()


def test_service_source_and_translated_errors_fail_closed() -> None:
    with pytest.raises(Exception):
        integration._service_source(SimpleNamespace(data={"source": ""}, context=None))
    error = integration._translated_error(Exception, "test_error", reason="x")
    assert isinstance(error, Exception)


def test_repair_helpers_and_unregister_are_bounded() -> None:
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(coordinator=SimpleNamespace(_quarantined={"d1"}))
    )
    assert integration._repair_device_ids(SimpleNamespace(), entry) == {"d1"}
    hass = _hass_with_services()
    integration._unregister_services(hass)
    assert hass.services.async_remove.call_count == 6
    assert MODE_OFF != MODE_AUTO


def test_repair_issue_sync_tracks_faults_and_removes_stale_persistent_issues() -> None:
    coordinator = SimpleNamespace(data={"faulted_devices": ["d1"], "quarantined_devices": ["d1"]})
    runtime = SimpleNamespace(coordinator=coordinator)
    entry = SimpleNamespace(entry_id="entry-1", runtime_data=runtime)
    hass = _hass_with_services(runtime=runtime)
    registry = SimpleNamespace(issues={(DOMAIN, "quarantine_old"): object()})
    create_issue = MagicMock()
    delete_issue = MagicMock()
    issue_registry = types.ModuleType("homeassistant.helpers.issue_registry")
    issue_registry.IssueSeverity = SimpleNamespace(ERROR="error")
    issue_registry.async_get = MagicMock(return_value=registry)
    issue_registry.async_create_issue = create_issue
    issue_registry.async_delete_issue = delete_issue
    with patch.dict(sys.modules, {"homeassistant.helpers.issue_registry": issue_registry}):
        integration._sync_repair_issues(hass, entry)

    issue_id = create_issue.call_args.args[2]
    assert issue_id.startswith("quarantine_")
    delete_issue.assert_called_once_with(hass, DOMAIN, "quarantine_old")

    coordinator.data = {"faulted_devices": [], "quarantined_devices": []}
    empty_registry = SimpleNamespace(issues={})
    delete_cleared = MagicMock()
    issue_registry.async_get = MagicMock(return_value=empty_registry)
    issue_registry.async_create_issue = MagicMock()
    issue_registry.async_delete_issue = delete_cleared
    with patch.dict(sys.modules, {"homeassistant.helpers.issue_registry": issue_registry}):
        integration._sync_repair_issues(hass, entry)
    delete_cleared.assert_called_once_with(hass, DOMAIN, issue_id)
