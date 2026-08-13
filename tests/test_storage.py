"""Runtime persistence tests for the load-shedding controller."""
from __future__ import annotations

import time

import pytest

from power_orchestrator.policy import DEFAULT_POLICY, Ownership, PolicyEngine, ReasonCode
from power_orchestrator.power_model import ManagedDevice, PowerModel
from power_orchestrator.storage import RuntimeStore


class FakeStore:
    def __init__(self, data=None):
        self._data = data

    async def async_load(self):
        return self._data

    async def async_save(self, data):
        self._data = data


def make_model() -> PowerModel:
    model = PowerModel()
    model.add_device(ManagedDevice("d1", "One", "switch.one", expected_power=1000))
    model.add_device(ManagedDevice("d2", "Two", "switch.two", expected_power=2000))
    return model


@pytest.mark.asyncio
async def test_empty_store_and_mode_round_trip() -> None:
    backend = FakeStore()
    store = RuntimeStore(backend)
    await store.async_load()
    assert store.restore_mode() is None
    store.set_mode("auto")
    await store.async_save()
    restored = RuntimeStore(FakeStore(backend._data))
    await restored.async_load()
    assert restored.restore_mode() == "auto"


@pytest.mark.asyncio
async def test_pending_restore_round_trip_preserves_order_and_filters_devices() -> None:
    backend = FakeStore()
    store = RuntimeStore(backend)
    await store.async_load()
    store.save_pending_restore(["d1", "missing", "d2", "d1"])
    await store.async_save()

    restored = RuntimeStore(FakeStore(backend._data))
    await restored.async_load()

    assert restored.restore_pending_restore(make_model()) == ["d1", "d2"]


@pytest.mark.asyncio
async def test_malformed_pending_restore_fails_closed() -> None:
    store = RuntimeStore(FakeStore({"pending_restore": "d1"}))
    await store.async_load()

    assert store.restore_pending_restore(make_model()) == []


@pytest.mark.asyncio
async def test_legacy_recovery_blocked_devices_migrate_to_quarantine() -> None:
    backend = FakeStore(
        {
            "device_runtime": {
                "schema_version": 1,
                "devices": {},
                "faulted_devices": [],
                "recovery_blocked_devices": ["d1"],
                "fault_reasons": {"d1": "relay_readback_timeout"},
            }
        }
    )
    store = RuntimeStore(backend)
    await store.async_load()
    faulted, quarantined = store.restore_device_runtime(make_model())
    assert faulted == set()
    assert quarantined == {"d1"}
    migrated = store.snapshot()["device_runtime"]
    assert migrated["schema_version"] == 2
    assert migrated["quarantined_devices"] == ["d1"]


def test_pause_and_device_runtime_are_bounded() -> None:
    backend = FakeStore()
    store = RuntimeStore(backend)
    model = make_model()
    store.set_pause("d1", time.time() + 30)
    store.restore_pause_timestamps(model)
    assert model.get_device("d1").pause_until is not None
    model.get_device("d1").ownership = Ownership.EXTERNAL
    model.get_device("d1").ownership_until = time.time() + 30
    store.save_device_runtime(model, faulted_devices={"d2"}, quarantined_devices={"d1"}, fault_reasons={"d2": "readback"})
    faulted, quarantined = store.restore_device_runtime(model)
    assert faulted == {"d2"}
    assert quarantined == {"d1"}
    assert store.restore_fault_reasons(model) == {"d2": "readback"}


def test_malformed_device_runtime_fails_closed() -> None:
    store = RuntimeStore(FakeStore())
    store._data = {"device_runtime": {"schema_version": 99}}
    faulted, quarantined = store.restore_device_runtime(make_model())
    assert faulted == set()
    assert quarantined == {"d1", "d2"}
    assert store.safety_storage_invalid


def test_policy_runtime_round_trip_ignores_removed_reenable_state() -> None:
    store = RuntimeStore(FakeStore())
    engine = PolicyEngine(DEFAULT_POLICY)
    engine.append_shed(operation_id="op-1", load_generation=2, reason_code=ReasonCode.SHED_FAST_OVERLOAD)
    store.save_policy_runtime(engine)
    restored = PolicyEngine(DEFAULT_POLICY)
    store.restore_policy_runtime(restored, make_model())
    assert restored.runtime.pending_post_shed_generation == 2
    assert not hasattr(restored.runtime, "shed_stack")


def test_action_journal_is_bounded_and_deduplicated() -> None:
    store = RuntimeStore(FakeStore())
    store.record_action({"action_id": "a1", "action": "turn_off", "phase": "prepared", "device_id": "d1"})
    store.record_action({"action_id": "a1", "action": "turn_off", "phase": "confirmed", "result": "confirmed"})
    assert store.unresolved_actions() == []
    assert store.audit_history()[0]["phase"] == "confirmed"
    store.record_action({"action_id": "bad"})
    assert len(store.audit_history()) == 1
