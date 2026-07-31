"""Policy runtime persistence and audit contracts."""
from __future__ import annotations

import copy
import time

import pytest

from power_orchestrator.policy import (
    DEFAULT_POLICY,
    Ownership,
    PolicyEngine,
    ReasonCode,
    ShedStackEntry,
)
from power_orchestrator.power_model import ManagedDevice, PowerModel
from power_orchestrator.storage import RuntimeStore


class FakeStore:
    def __init__(self, data=None):
        self._data = data

    async def async_load(self):
        return copy.deepcopy(self._data)

    async def async_save(self, data):
        self._data = copy.deepcopy(data)


@pytest.mark.asyncio
async def test_runtime_store_round_trips_stack_and_audit():
    fake = FakeStore()
    store = RuntimeStore(fake)
    await store.async_load()
    engine = PolicyEngine(DEFAULT_POLICY)
    engine.append_shed(
        ShedStackEntry(
            device_id="bathroom",
            operation_id="op-1",
            pre_state=True,
            snapshot={"switch.bathroom": {"state": "on"}},
            load_generation=7,
            reason_code=ReasonCode.SHED_FAST_OVERLOAD,
        )
    )
    store.save_policy_runtime(engine)
    store.record_action(
        {
            "operation_id": "op-1",
            "device_id": "bathroom",
            "result": "confirmed",
        }
    )
    await store.async_save()

    restored_store = RuntimeStore(fake)
    await restored_store.async_load()
    restored_engine = PolicyEngine(DEFAULT_POLICY)
    restored_store.restore_policy_runtime(restored_engine)

    assert restored_engine.runtime.shed_stack[0].device_id == "bathroom"
    assert restored_engine.runtime.shed_stack[0].reason_code is ReasonCode.SHED_FAST_OVERLOAD
    assert restored_store.audit_history()[0]["operation_id"] == "op-1"


def test_device_runtime_round_trips_fault_reasons():
    model = PowerModel()
    model.add_device(ManagedDevice("d1", "Device 1", "switch.d1", expected_power=1000))
    store = RuntimeStore(FakeStore())
    store.save_device_runtime(
        model,
        faulted_devices={"d1"},
        fault_reasons={"d1": "relay_readback_timeout", "unknown": "drop"},
    )

    assert store.restore_fault_reasons(model) == {
        "d1": "relay_readback_timeout"
    }


def test_record_action_replay_is_idempotent_and_updates_one_lifecycle_record():
    store = RuntimeStore(FakeStore())
    store.record_action(
        {
            "action_id": "action-1",
            "phase": "prepared",
            "result": "prepared",
        }
    )
    store.record_action(
        {
            "action_id": "action-1",
            "phase": "confirmed",
            "result": "confirmed",
        }
    )

    history = store.audit_history()

    assert len(history) == 1
    assert history[0]["action_id"] == "action-1"
    assert history[0]["phase"] == "confirmed"
    assert history[0]["result"] == "confirmed"


def test_unknown_future_action_schema_is_not_accepted():
    store = RuntimeStore(FakeStore())
    store.record_action(
        {
            "action_id": "future-1",
            "event_schema": 999,
            "action": "turn_on",
        }
    )

    assert store.audit_history() == []


def test_action_journal_is_versioned_bounded_and_scalar_only():
    store = RuntimeStore(FakeStore())
    store._data = {
        "audit_history": [
            {"operation_id": str(index), "nested": {"unsafe": True}}
            for index in range(105)
        ]
    }

    history = store.audit_history()

    assert len(history) == 100
    assert history[0]["operation_id"] == "5"
    assert history[-1]["event_schema"] == 1
    assert history[-1]["event_type"] == "power_orchestrator.action"
    assert history[-1]["policy_phase"] == "unknown"
    assert "nested" not in history[-1]



def test_runtime_store_round_trips_execution_mode():
    store = RuntimeStore(FakeStore())
    store.set_execution_mode("observe")

    assert store.restore_execution_mode() == "observe"

    store.set_execution_mode("live")
    assert store.restore_execution_mode() == "live"



@pytest.mark.asyncio
async def test_runtime_store_persists_observe_mode_across_reload_and_rejects_invalid_mode():
    fake = FakeStore()
    store = RuntimeStore(fake)
    await store.async_load()
    store.set_execution_mode("observe")
    await store.async_save()

    restored = RuntimeStore(fake)
    await restored.async_load()
    assert restored.restore_execution_mode() == "observe"

    restored._data["execution_mode"] = "armed"
    assert restored.restore_execution_mode() is None


def test_malformed_device_runtime_fails_closed_for_every_configured_device():
    model = PowerModel()
    model.add_device(ManagedDevice("d1", "Device 1", "switch.d1", expected_power=1000))
    model.add_device(ManagedDevice("d2", "Device 2", "switch.d2", expected_power=1000))
    store = RuntimeStore(FakeStore())
    store._data = {"device_runtime": "corrupt"}

    faulted, recovery_blocked = store.restore_device_runtime(model)

    assert faulted == set()
    assert recovery_blocked == {"d1", "d2"}
    assert store.restore_fault_reasons(model) == {
        "d1": ReasonCode.PERSISTED_RUNTIME_INVALID.value,
        "d2": ReasonCode.PERSISTED_RUNTIME_INVALID.value,
    }


def test_unknown_future_device_runtime_schema_fails_closed():
    model = PowerModel()
    model.add_device(ManagedDevice("d1", "Device 1", "switch.d1", expected_power=1000))
    store = RuntimeStore(FakeStore())
    store._data = {
        "device_runtime": {
            "schema_version": 999,
            "devices": {},
            "faulted_devices": [],
            "recovery_blocked_devices": [],
        }
    }

    faulted, recovery_blocked = store.restore_device_runtime(model)

    assert faulted == set()
    assert recovery_blocked == {"d1"}
    assert store.safety_storage_invalid is True


def test_runtime_store_rejects_malformed_stack_records():
    store = RuntimeStore(FakeStore())
    store._data = {"policy_runtime": {"shed_stack": [{"device_id": ""}, "bad"]}}
    engine = PolicyEngine(DEFAULT_POLICY)

    store.restore_policy_runtime(engine)

    assert engine.runtime.shed_stack == []
    assert store.audit_history() == []


def test_post_shed_barrier_requires_report_newer_than_off_marker():
    engine = PolicyEngine(DEFAULT_POLICY)
    engine.append_shed(
        ShedStackEntry(
            device_id="d1",
            operation_id="op-1",
            pre_state=True,
            snapshot={},
            load_generation=1,
            reason_code=ReasonCode.SHED_FAST_OVERLOAD,
        )
    )
    engine.set_post_shed_fence(10.0)

    assert engine.reconcile_shed(2, reported_at=10.0) is False
    assert engine.reconcile_shed(2, reported_at=11.0) is True



def test_restored_post_shed_barrier_requires_only_a_new_runtime_report():
    store = RuntimeStore(FakeStore())
    store._data = {
        "policy_runtime": {
            "pending_post_shed_generation": 42,
            "last_shed_load_generation": 42,
            "pending_post_shed_after_reported_at": 100.0,
            "pending_operation_id": "op-42",
        }
    }
    engine = PolicyEngine(DEFAULT_POLICY)

    store.restore_policy_runtime(engine)

    assert engine.runtime.pending_post_shed_generation == 0
    assert engine.runtime.pending_post_shed_after_reported_at == 100.0
    assert engine.runtime.pending_operation_id == "op-42"
    assert engine.reconcile_shed(1, reported_at=100.0) is False
    assert engine.reconcile_shed(1, reported_at=101.0) is True


def test_device_runtime_round_trips_fault_quarantine_and_ownership():
    model = PowerModel()
    device = ManagedDevice(
        "d1",
        "Device 1",
        "switch.d1",
        expected_power=1000,
        ownership=Ownership.EXTERNAL,
        ownership_until=time.time() + 60,
    )
    model.add_device(device)
    store = RuntimeStore(FakeStore())
    store.save_device_runtime(
        model,
        faulted_devices={"d1"},
        recovery_blocked_devices={"d1"},
    )

    restored_model = PowerModel()
    restored_device = ManagedDevice("d1", "Device 1", "switch.d1", expected_power=1000)
    restored_model.add_device(restored_device)
    faulted, recovery_blocked = store.restore_device_runtime(restored_model)

    assert faulted == {"d1"}
    assert recovery_blocked == {"d1"}
    assert restored_device.ownership is Ownership.EXTERNAL
    assert restored_device.ownership_until is not None
