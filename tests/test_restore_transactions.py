"""Durable transition evidence at the evaluation and recovery interfaces."""

from __future__ import annotations

import asyncio
import copy
import time

import pytest

import power_orchestrator.const as constants
from power_orchestrator.coordinator import CoordinatorConfig, PowerOrchestratorCoordinator
from power_orchestrator.policy import policy_for_tests
from power_orchestrator.power_model import ManagedDevice, PowerModel
from power_orchestrator.storage import RuntimeStore
from tests.test_coordinator import CopyingStoreBackend


class PhaseStore(CopyingStoreBackend):
    """Inject failure by journal meaning, observing actual durable snapshots."""

    def __init__(self):
        super().__init__()
        self.fail_phase = None
        self.after_save = None
        self.attempts = []

    async def async_save(self, data):
        candidate = copy.deepcopy(data)
        self.attempts.append(candidate)
        journal = candidate.get("audit_history", [])
        event = journal[-1] if journal else {}
        if event.get("action") == "turn_on" and self.fail_phase is not None and (
            event.get("phase") == self.fail_phase
            or event.get("outcome_reason") == "action_persistence_failed"
        ):
            raise RuntimeError("injected transition save failure")
        await super().async_save(candidate)
        await asyncio.sleep(0)
        if self.after_save is not None:
            self.after_save(event)


async def make_control(hass, backend):
    store = RuntimeStore(backend)
    await store.async_load()
    model = PowerModel()
    for device_id in ("d1", "d2"):
        model.add_device(ManagedDevice(device_id, device_id, f"switch.{device_id}", expected_power=1000))
    control = PowerOrchestratorCoordinator(
        hass, model, store,
        CoordinatorConfig(
            load_sensor="sensor.load", averaging_period=10, pause_period=0,
            grid_loss_mode="grid_loss_sensor", grid_loss_sensor="binary_sensor.grid",
            policy=policy_for_tests((5000.0, 0.0)), entry_id="transactions-test",
        ),
    )
    return control, store


@pytest.fixture
async def restore_setup(hass, monkeypatch):
    monkeypatch.setattr(constants, "RESTORE_SAFE_CAPACITY_DWELL_S", 0.0)
    commands = []
    backend = PhaseStore()
    hass.states.async_set("sensor.load", "1000", {"unit_of_measurement": "W"})
    hass.states.async_set("binary_sensor.grid", "on")
    hass.states.async_set("switch.d1", "on")
    hass.states.async_set("switch.d2", "off")

    async def command(call):
        commands.append((call.service, call.data["entity_id"]))
        # Every physical call observes durable dispatch intent, never just a
        # prepared record. Terminal confirmation is still absent at this point.
        assert backend.data["audit_history"][-1]["phase"] == "dispatched"
        hass.states.async_set(call.data["entity_id"], "on" if call.service == "turn_on" else "off")

    hass.services.async_register("switch", "turn_on", command)
    hass.services.async_register("switch", "turn_off", command)
    control, store = await make_control(hass, backend)
    control.mode = "auto"
    control._model.get_device("d1").is_on = True
    assert await control.async_request_stop("d1", source="owner")
    assert backend.data["restore_tickets"][0]["device_id"] == "d1"
    assert backend.data["audit_history"][-1]["phase"] == "confirmed"
    commands.clear()
    backend.attempts.clear()
    return hass, control, store, backend, commands


@pytest.mark.asyncio
async def test_restore_atomically_commits_confirmation_ticket_order_and_load_fence(restore_setup):
    _hass, control, store, backend, commands = restore_setup
    await control.async_refresh()
    assert commands == [("turn_on", "switch.d1")]
    assert control.data["pending_restore_ids"] == []
    assert control.data["restore_barrier_pending"] is True
    terminal = next(
        value for value in backend.attempts
        if value["audit_history"][-1].get("action") == "turn_on"
        and value["audit_history"][-1].get("phase") == "confirmed"
    )
    assert terminal["restore_tickets"] == []
    assert terminal["pending_restore"] == []
    assert terminal["policy_runtime"]["pending_restore_operation_id"] == terminal["audit_history"][-1]["operation_id"]
    assert terminal["policy_runtime"]["pending_post_restore_generation"] is not None
    turn_on = [value["audit_history"][-1] for value in backend.attempts if value["audit_history"][-1]["action"] == "turn_on"]
    assert {event["decision_reason"] for event in turn_on} == {"restore_headroom_available"}
    assert all(event["input_snapshot"] == turn_on[0]["input_snapshot"] for event in turn_on)
    assert store.unresolved_actions() == []
    await control.async_refresh()
    assert commands == [("turn_on", "switch.d1")]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["prepared", "dispatched", "confirmed"])
async def test_save_failure_never_regrants_restore_after_later_save_and_reload(restore_setup, phase):
    hass, control, _store, backend, commands = restore_setup
    backend.fail_phase = phase
    await control.async_refresh()
    expected = [("turn_on", "switch.d1")] if phase == "confirmed" else []
    assert commands == expected
    assert "d1" in control.data["quarantined_devices"]
    assert control.data["pending_restore_ids"] == []
    assert control.data["journal_persistence_blocked"] is True
    last_durable = copy.deepcopy(backend.data)
    if phase == "confirmed":
        assert last_durable["audit_history"][-1]["phase"] == "dispatched"
        # Reload the actual last durable bytes, before repairing persistence.
        reload_backend = CopyingStoreBackend()
        reload_backend.data = last_durable
        restarted, _ = await make_control(hass, reload_backend)
        await restarted.async_recover_runtime({})
        await restarted.async_refresh()
        assert "d1" in restarted.data["quarantined_devices"]
        assert commands == expected
    backend.fail_phase = None
    await control.async_refresh()
    assert commands == expected
    assert backend.data["restore_tickets"] == []
    assert backend.data["audit_history"][-1]["outcome_reason"] == "action_persistence_failed"
    restarted, _ = await make_control(hass, backend)
    await restarted.async_recover_runtime({})
    await restarted.async_refresh()
    assert "d1" in restarted.data["quarantined_devices"]
    assert restarted.data["pending_restore_ids"] == []
    assert commands == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["prepared", "dispatched"])
@pytest.mark.parametrize("withdrawal", ["telemetry", "safety", "pause", "expiry", "cancel", "intent_expiry"])
async def test_permission_is_fresh_after_each_durable_barrier(restore_setup, phase, withdrawal):
    hass, control, _store, backend, commands = restore_setup

    def withdraw(event):
        if event.get("action") != "turn_on" or event.get("phase") != phase:
            return
        if withdrawal == "telemetry":
            hass.states.async_set("sensor.load", "unavailable")
        elif withdrawal == "safety":
            hass.states.async_set("binary_sensor.grid", "off")
        elif withdrawal == "pause":
            control._model.get_device("d1").pause_until = time.time() + 600
        elif withdrawal == "cancel":
            control._remove_pending_restore("d1")
        elif withdrawal == "expiry":
            from dataclasses import replace
            ticket = control._restore_tickets["d1"]
            control._restore_tickets["d1"] = replace(ticket, expires_at=ticket.created_at + 0.0001)
        else:
            from power_orchestrator.requests import RestoreIntent
            control._intents.set("d1", RestoreIntent("manual_observed", True, time.time() - 1))

    backend.after_save = withdraw
    await control.async_refresh()
    assert commands == []
    rejected = [event for event in backend.data["audit_history"] if event["action"] == "turn_on"]
    assert rejected[-1]["phase"] == "rejected"
    assert rejected[-1]["outcome_reason"] == "restore_permission_withdrawn"


@pytest.mark.asyncio
async def test_restore_preserves_reverse_shed_order_and_pause_then_cancel(restore_setup):
    hass, control, _store, backend, commands = restore_setup
    hass.states.async_set("switch.d2", "on")
    control._model.get_device("d2").is_on = True
    assert await control.async_request_stop("d2", source="owner")
    commands.clear()
    control._model.get_device("d2").pause_until = time.time() + 600
    await control.async_refresh()
    assert commands == [("turn_on", "switch.d1")]
    assert control.data["pending_restore_ids"] == ["d2"]
    assert await control.async_cancel_restore("d2") is True
    assert backend.data["restore_tickets"] == []
    assert backend.data["pending_restore"] == []
    assert commands == [("turn_on", "switch.d1")]


@pytest.mark.asyncio
async def test_cancelled_dispatch_retires_proof_and_survives_reload_without_compensation(restore_setup):
    hass, control, _store, backend, commands = restore_setup
    started = asyncio.Event()
    release = asyncio.Event()

    async def uncertain_command(call):
        commands.append((call.service, call.data["entity_id"]))
        assert backend.data["audit_history"][-1]["phase"] == "dispatched"
        hass.states.async_set(call.data["entity_id"], "on")
        started.set()
        await release.wait()

    hass.services.async_register("switch", "turn_on", uncertain_command)
    update = asyncio.create_task(control.async_refresh())
    await asyncio.wait_for(started.wait(), 5)
    update.cancel()
    with pytest.raises(asyncio.CancelledError):
        await update
    release.set()
    await hass.async_block_till_done()
    assert backend.data["restore_tickets"] == []
    assert backend.data["audit_history"][-1]["outcome_reason"] == "action_cancelled"
    assert backend.data["device_runtime"]["quarantined_devices"] == ["d1"]
    restarted, _ = await make_control(hass, backend)
    await restarted.async_recover_runtime({})
    await restarted.async_refresh()
    assert "d1" in restarted.data["quarantined_devices"]
    assert restarted.data["pending_restore_ids"] == []
    assert commands == [("turn_on", "switch.d1")]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["prepared", "dispatched"])
@pytest.mark.parametrize("withdrawal", ["telemetry", "safety"])
async def test_stop_rechecks_permission_after_each_durable_barrier(restore_setup, phase, withdrawal):
    hass, control, _store, backend, commands = restore_setup
    hass.states.async_set("switch.d2", "on")
    control._model.get_device("d2").is_on = True

    def withdraw(event):
        if event.get("action") == "turn_off" and event.get("phase") == phase:
            entity = "sensor.load" if withdrawal == "telemetry" else "binary_sensor.grid"
            hass.states.async_set(entity, "unavailable")

    backend.after_save = withdraw
    assert await control.async_request_stop("d2", source="owner") is False
    assert commands == []
    assert backend.data["audit_history"][-1]["phase"] == "rejected"
    assert backend.data["audit_history"][-1]["outcome_reason"] == "off_permission_withdrawn"
    assert [ticket["device_id"] for ticket in backend.data["restore_tickets"]] == ["d1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["readback", "service"])
async def test_failed_physical_restore_remains_quarantined_across_reload(restore_setup, failure):
    hass, control, _store, backend, commands = restore_setup

    async def failing_command(call):
        commands.append((call.service, call.data["entity_id"]))
        if failure == "service":
            raise RuntimeError("synthetic external command failure")
        # Successful service receipt without a causal ON report is unresolved.

    hass.services.async_register("switch", "turn_on", failing_command)
    await control.async_refresh()
    assert commands == [("turn_on", "switch.d1")]
    assert "d1" in control.data["quarantined_devices"]
    assert backend.data["audit_history"][-1]["phase"] == "failed"
    restarted, _ = await make_control(hass, backend)
    await restarted.async_recover_runtime({})
    await restarted.async_refresh()
    assert "d1" in restarted.data["quarantined_devices"]
    assert commands == [("turn_on", "switch.d1")]


@pytest.mark.asyncio
@pytest.mark.parametrize("reconfiguration", ["command", "domain", "missing_identity"])
async def test_retained_ticket_never_authorizes_a_reconfigured_actuator_on_reload(restore_setup, reconfiguration):
    hass, _control, _store, backend, commands = restore_setup
    if reconfiguration == "domain":
        backend.data["restore_tickets"][0]["restore_state"]["domain"] = "climate"
    elif reconfiguration == "missing_identity":
        backend.data["restore_tickets"][0]["restore_state"].pop("entity_id")
    restarted, _ = await make_control(hass, backend)
    if reconfiguration == "command":
        device = restarted._model.get_device("d1")
        device.command_entity_id = "switch.d2"
        device.readback_entity_ids = ("switch.d2",)
    await restarted.async_recover_runtime({})
    await restarted.async_refresh()
    assert commands == []
    assert restarted.data["pending_restore_ids"] == ["d1"]
    assert backend.data["audit_history"][-1]["phase"] == "rejected"
    assert backend.data["audit_history"][-1]["outcome_reason"] == "restore_permission_withdrawn"


@pytest.mark.asyncio
async def test_emergency_fallback_rechecks_telemetry_between_each_physical_member(restore_setup):
    hass, control, _store, backend, commands = restore_setup
    device = control._model.get_device("d2")
    device.command_entity_id = "climate.d2"
    device.readback_entity_ids = ("switch.d2", "switch.backup")
    device.emergency_off_entity_ids = ("switch.d2", "switch.backup")
    device.is_on = True
    hass.states.async_set("climate.d2", "heat")
    hass.states.async_set("switch.d2", "on")
    hass.states.async_set("switch.backup", "on")
    hass.states.async_set("binary_sensor.grid", "off")

    async def failed_normal_stop(call):
        commands.append((call.service, call.data["entity_id"]))

    async def first_fallback(call):
        commands.append((call.service, call.data["entity_id"]))
        hass.states.async_set(call.data["entity_id"], "off")
        hass.states.async_set("binary_sensor.grid", "unavailable")

    hass.services.async_register("climate", "set_hvac_mode", failed_normal_stop)
    hass.services.async_register("switch", "turn_off", first_fallback)
    assert await control.async_request_stop("d2", source="owner") is False
    assert commands == [("set_hvac_mode", "climate.d2"), ("turn_off", "switch.d2")]
    assert backend.data["device_runtime"]["quarantined_devices"] == ["d2"]
    assert hass.states.get("switch.backup").state == "on"


@pytest.mark.asyncio
async def test_battery_stop_commits_pause_in_confirmed_terminal_snapshot(restore_setup):
    hass, control, _store, backend, commands = restore_setup
    device = control._model.get_device("d2")
    device.is_on = True
    device.battery_min_soc = 40
    control._battery_soc_sensor = "sensor.soc"
    control._pause_period = 600
    hass.states.async_set("sensor.soc", "30", {"unit_of_measurement": "%"})
    hass.states.async_set("binary_sensor.grid", "off")
    hass.states.async_set("switch.d2", "on")
    assert await control._stop_on_low_battery(device) is True
    terminal = next(
        snapshot for snapshot in backend.attempts
        if snapshot["audit_history"][-1].get("source") == "battery_minimum"
        and snapshot["audit_history"][-1].get("phase") == "confirmed"
    )
    assert terminal["pause_timestamps"]["d2"] == device.pause_until
    assert device.pause_until > time.time()
    assert "d2" not in {ticket["device_id"] for ticket in terminal["restore_tickets"]}
    assert commands == [("turn_off", "switch.d2")]
