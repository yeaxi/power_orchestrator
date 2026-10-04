"""Durable runtime reconstruction through the lifecycle recovery interface."""

from __future__ import annotations

import time

import pytest

from power_orchestrator.const import NOTIFY_TELEMETRY_ID
from power_orchestrator.coordinator import CoordinatorConfig, PowerOrchestratorCoordinator
from power_orchestrator.policy import policy_for_tests
from power_orchestrator.power_model import ManagedDevice, PowerModel
from power_orchestrator.requests import RestoreTicket
from power_orchestrator.storage import RuntimeStore
from tests.test_coordinator import CopyingStoreBackend


@pytest.fixture
def recovery_hass(hass):
    """Use actual local HA states and commands; recovery must never call them."""
    commands = []
    dismissals = []

    async def command(call):
        commands.append((call.service, call.data["entity_id"]))

    async def dismiss(call):
        dismissals.append(call.data["notification_id"])

    hass.services.async_register("switch", "turn_on", command)
    hass.services.async_register("switch", "turn_off", command)
    hass.services.async_register("persistent_notification", "dismiss", dismiss)
    hass.states.async_set("sensor.load", "1000", {"unit_of_measurement": "W"})
    hass.states.async_set("binary_sensor.grid", "on")
    for device_id in ("d1", "d2", "d3"):
        hass.states.async_set(f"switch.{device_id}", "off")
    return hass, commands, dismissals


async def loaded_control(hass, backend):
    store = RuntimeStore(backend)
    await store.async_load()
    model = PowerModel()
    for device_id in ("d1", "d2", "d3"):
        model.add_device(ManagedDevice(device_id, device_id, f"switch.{device_id}", expected_power=1000))
    control = PowerOrchestratorCoordinator(
        hass, model, store,
        CoordinatorConfig(
            load_sensor="sensor.load", averaging_period=10, pause_period=60,
            grid_loss_mode="grid_loss_sensor", grid_loss_sensor="binary_sensor.grid",
            policy=policy_for_tests((5000.0, 300.0)), entry_id="recovery-test",
        ),
    )
    return control, store


def ticket(device_id, now, *, expired=False):
    return RestoreTicket(
        device_id=device_id, cause="overload", operation_id=f"shed-{device_id}",
        created_at=now - 60, expires_at=now - 1 if expired else now + 600,
        restore_state={"state": "on"}, intent_sources=("owner",),
    ).to_dict()


@pytest.mark.asyncio
async def test_recovery_migrates_legacy_mode_and_requests_and_keeps_only_owned_live_tickets(recovery_hass):
    hass, commands, _ = recovery_hass
    now = time.time()
    backend = CopyingStoreBackend()
    backend.data = {
        "mode": "auto", "execution_mode": "observe",
        "pause_timestamps": {"d2": now + 120},
        "pending_restore": ["d3"],
        "run_requests": {
            "d1": {"source": "owner", "active": True, "expires_at": now + 600},
            "d2": {"source": "owner", "active": True, "expires_at": now + 600},
            "d3": {"source": "inactive", "active": False, "expires_at": now + 600},
        },
        "restore_tickets": [ticket("d2", now), ticket("d1", now), ticket("d3", now, expired=True)],
        "policy_runtime": {
            "phase": "waiting_post_shed_load", "pending_post_shed_generation": 99,
            "pending_post_shed_after_reported_at": now + 60,
            "pending_operation_id": "previous-shed",
        },
    }
    control, store = await loaded_control(hass, backend)
    await control.async_recover_runtime({})
    assert control.mode == "observe"
    assert commands == []
    assert "execution_mode" not in backend.data
    assert "run_requests" not in backend.data
    await control.async_config_entry_first_refresh()
    assert control.data["pending_restore_ids"] == ["d2", "d1"]
    assert [value["device_id"] for value in control.data["restore_tickets"]] == ["d2", "d1"]
    assert [value["source"] for value in control.data["restore_intents"]] == ["owner", "owner"]
    assert control.data["shed_barrier_pending"] is True
    assert commands == []
    assert store.snapshot()["pause_timestamps"]["d2"] == now + 120


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_state", [
    {"device_runtime": "bad"},
    {"restore_intents": "bad"},
    {"restore_tickets": "bad"},
    {"telemetry_fault": {"latched": "bad"}},
    {"safety_storage_invalid": "bad"},
])
async def test_corruption_is_seeded_once_and_stays_blocked_after_snapshot_save_and_reload(recovery_hass, bad_state):
    hass, commands, _ = recovery_hass
    backend = CopyingStoreBackend()
    backend.data = {"mode": "auto", **bad_state}
    for _ in range(2):
        control, _store = await loaded_control(hass, backend)
        await control.async_recover_runtime({})
        assert control.mode == "off"
        assert control.safety_storage_invalid is True
        assert commands == []
        await control.async_config_entry_first_refresh()
        await control.async_persist_runtime()
        assert control.data["safety_storage_invalid"] is True
        assert control.data["physical_commands_allowed"] is False
        assert control.data["restore_commands_allowed"] is False
        assert backend.data["safety_storage_invalid"] is True
    assert commands == []


@pytest.mark.asyncio
async def test_unfinished_action_quarantine_survives_recovery_save_and_reload(recovery_hass):
    hass, commands, _ = recovery_hass
    backend = CopyingStoreBackend()
    backend.data = {
        "mode": "observe",
        "audit_history": [{
            "action_id": "ambiguous", "action": "turn_on", "device_id": "d1",
            "phase": "dispatched", "result": "dispatched", "decision_reason": "restore_safe",
        }],
    }
    for _ in range(2):
        control, _store = await loaded_control(hass, backend)
        await control.async_recover_runtime({})
        assert control.action_journal_invalid is True
        assert commands == []
        await control.async_config_entry_first_refresh()
        assert control.data["quarantined_devices"] == ["d1"]
        assert control.data["faulted_devices"] == ["d1"]
        assert control.data["journal_unresolved_count"] == 1
        assert control.data["fault_reasons"]["d1"] == "persisted_runtime_invalid"
        await control.async_persist_runtime()
    assert commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize("latched", [False, True])
async def test_telemetry_fault_restores_notification_dismissal_without_replaying_commands(recovery_hass, latched):
    hass, commands, dismissals = recovery_hass
    notification_id = f"{NOTIFY_TELEMETRY_ID}_recovery-test"
    backend = CopyingStoreBackend()
    backend.data = {
        "mode": "observe",
        "telemetry_fault": {"latched": latched, "reason": "load_unavailable" if latched else None, "emergency_handled": latched},
        "fault_notifications": {
            "schema_version": 1, "active": {notification_id: "load_unavailable"}, "pending_dismissal": {},
        },
    }
    control, _store = await loaded_control(hass, backend)
    await control.async_recover_runtime({})
    assert dismissals == []
    assert commands == []
    await control.async_config_entry_first_refresh()
    assert dismissals == [notification_id]
    assert control.data["telemetry_fault_latched"] is False
    assert control.data["telemetry_fault_reason"] is None
    assert backend.data["fault_notifications"]["active"] == {}
    assert commands == []


@pytest.mark.asyncio
async def test_mode_save_failure_falls_back_to_observe_and_exposes_persistence_failure(recovery_hass):
    hass, commands, _ = recovery_hass
    backend = CopyingStoreBackend()
    backend.data = {"mode": "auto"}
    backend.fail_on = set(range(1, 100))
    control, _store = await loaded_control(hass, backend)
    await control.async_recover_runtime({})
    assert control.mode == "observe"
    assert control.last_action == "Mode persistence failed; defaulting to observe"
    assert commands == []
    await control.async_config_entry_first_refresh()
    assert control.data["journal_persistence_blocked"] is True
    assert control.data["mode"] == "observe"
    assert control.data["physical_commands_allowed"] is False
    assert backend.data == {"mode": "auto"}
    assert commands == []


@pytest.mark.asyncio
async def test_reconfiguration_selects_observe_even_with_durable_auto(recovery_hass):
    hass, commands, _ = recovery_hass
    backend = CopyingStoreBackend()
    backend.data = {"mode": "auto"}
    control, _store = await loaded_control(hass, backend)
    await control.async_recover_runtime({}, reconfiguration_required=True)
    await control.async_config_entry_first_refresh()
    assert control.data["mode"] == "observe"
    assert control.data["reconfiguration_required"] is True
    assert control.data["physical_commands_allowed"] is False
    assert commands == []


@pytest.mark.asyncio
async def test_native_store_adapter_round_trip_restores_quarantine_and_selected_mode(recovery_hass):
    from homeassistant.helpers.storage import Store
    from power_orchestrator.const import STORAGE_VERSION

    hass, commands, _ = recovery_hass
    key = "power_orchestrator_recovery_native_store"
    backend = Store(hass, STORAGE_VERSION, key)
    await backend.async_save({
        "mode": "observe",
        "audit_history": [{
            "action_id": "native-ambiguous", "action": "turn_off", "device_id": "d2",
            "phase": "prepared", "result": "prepared",
        }],
    })
    for _ in range(2):
        control, _store = await loaded_control(hass, Store(hass, STORAGE_VERSION, key))
        await control.async_recover_runtime({})
        assert commands == []
        await control.async_config_entry_first_refresh()
        assert control.data["mode"] == "observe"
        assert control.data["quarantined_devices"] == ["d2"]
        assert control.data["action_journal_invalid"] is True
        await control.async_persist_runtime()
    assert commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["future_ticket", "negative_ticket", "overlong_ticket", "overlong_intent", "overlong_legacy_intent"])
async def test_native_store_time_corruption_remains_sticky_across_once_seeded_reload(recovery_hass, corruption):
    from homeassistant.helpers.storage import Store
    from power_orchestrator.const import STORAGE_VERSION
    from power_orchestrator.requests import MAX_INTENT_TTL_S

    hass, commands, _ = recovery_hass
    now = time.time()
    proof = ticket("d1", now)
    intent = {"source": "owner", "active": True, "expires_at": now + 600}
    payload = {"mode": "auto", "restore_tickets": [proof], "restore_intents": [{"device_id": "d1", **intent}]}
    if corruption == "future_ticket":
        proof.update(created_at=now + 3600, expires_at=now + 7200)
    elif corruption == "negative_ticket":
        proof.update(created_at=-1, expires_at=600)
    elif corruption == "overlong_ticket":
        proof["expires_at"] = proof["created_at"] + MAX_INTENT_TTL_S + 1
    elif corruption == "overlong_intent":
        payload["restore_intents"][0]["expires_at"] = now + MAX_INTENT_TTL_S + 60
    else:
        payload.pop("restore_intents")
        payload["run_requests"] = {"d1": {**intent, "expires_at": now + MAX_INTENT_TTL_S + 60}}
    key = f"power_orchestrator_time_corruption_{corruption}"
    await Store(hass, STORAGE_VERSION, key).async_save(payload)
    for _ in range(2):
        control, _store = await loaded_control(hass, Store(hass, STORAGE_VERSION, key))
        await control.async_recover_runtime({})
        assert control.mode == "off"
        assert control.safety_storage_invalid is True
        await control.async_config_entry_first_refresh()
        assert control.data["physical_commands_allowed"] is False
        assert control.data["restore_commands_allowed"] is False
        await control.async_persist_runtime()
        durable = await Store(hass, STORAGE_VERSION, key).async_load()
        assert durable["safety_storage_invalid"] is True
    assert commands == []


@pytest.mark.asyncio
async def test_recovered_deadlines_accept_creation_now_and_maximum_lifetime_and_retire_expiry_now(recovery_hass):
    from unittest.mock import patch
    from power_orchestrator.requests import MAX_INTENT_TTL_S

    hass, commands, _ = recovery_hass
    now = time.time()
    backend = CopyingStoreBackend()
    live = ticket("d1", now)
    live.update(created_at=now, expires_at=now + MAX_INTENT_TTL_S)
    expired = ticket("d2", now)
    expired["expires_at"] = now
    backend.data = {
        "mode": "observe", "restore_tickets": [live, expired],
        "restore_intents": [
            {"device_id": "d1", "source": "owner", "active": True, "expires_at": now + MAX_INTENT_TTL_S},
            {"device_id": "d2", "source": "owner", "active": True, "expires_at": now},
        ],
    }
    with patch("power_orchestrator.storage.time.time", return_value=now):
        control, _store = await loaded_control(hass, backend)
        await control.async_recover_runtime({})
        assert control.safety_storage_invalid is False
        await control.async_config_entry_first_refresh()
    assert control.data["pending_restore_ids"] == ["d1"]
    assert [intent["device_id"] for intent in control.data["restore_intents"]] == ["d1"]
    assert control.data["safety_storage_invalid"] is False
    assert commands == []
