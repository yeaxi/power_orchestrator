"""Tests for storage.py — no HA dependencies."""
import sys, os, json, tempfile
import math
import time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components"))

import pytest
from power_orchestrator.storage import RuntimeStore
from power_orchestrator.policy import DEFAULT_POLICY, Ownership, PolicyEngine, ReasonCode
from power_orchestrator.power_model import ManagedDevice, PowerModel


class FakeStore:
    """Mock HA Store."""
    def __init__(self):
        self._data = None

    async def async_load(self):
        return self._data

    async def async_save(self, data):
        self._data = data


def make_model():
    m = PowerModel()
    m.add_device(ManagedDevice(device_id="d1", name="D1", entity_id="switch.d1", expected_power=1000, priority=1))
    m.add_device(ManagedDevice(device_id="d2", name="D2", entity_id="switch.d2", expected_power=2000, priority=2))
    return m


@pytest.mark.asyncio
async def test_store_empty_start():
    store = RuntimeStore(FakeStore())
    await store.async_load()
    assert store._data == {}


@pytest.mark.asyncio
async def test_store_restore_pause_timestamps():
    import time
    fake = FakeStore()
    fake._data = {
        "pause_timestamps": {
            "d1": time.time() + 12345,
            "d2": time.time() + 67890,
        }
    }
    store = RuntimeStore(fake)
    await store.async_load()
    model = make_model()
    store.restore_pause_timestamps(model)
    assert model.get_device("d1").pause_until > time.time()
    assert model.get_device("d2").pause_until > time.time()


@pytest.mark.asyncio
async def test_storage_non_object_load_fails_closed():
    fake = FakeStore()
    fake._data = ["corrupt"]
    store = RuntimeStore(fake)
    await store.async_load()
    assert store.snapshot() == {}
    assert store.action_journal_invalid is False


@pytest.mark.parametrize(
    "duration",
    ["bad", float("nan"), float("inf"), -1],
)
def test_storage_set_pause_rejects_invalid_duration(duration):
    store = RuntimeStore(FakeStore())
    store.set_pause("d1", duration)
    assert store.snapshot() == {"pause_timestamps": {}} or store.snapshot() == {}


def test_storage_pause_restore_removes_invalid_and_unknown_entries():
    now = time.time()
    store = RuntimeStore(FakeStore())
    store._data = {
        "pause_timestamps": {
            "d1": now + 10,
            "d2": True,
            "d3": math.nan,
            "d4": now - 1,
            "unknown": now + 10,
        }
    }
    model = make_model()
    store.restore_pause_timestamps(model, max_pause_seconds="bad")
    assert model.get_device("d1").pause_until is not None
    assert set(store._data["pause_timestamps"]) == {"d1"}

    store._data["pause_timestamps"] = {"d1": now + 10}
    store.restore_pause_timestamps(model, max_pause_seconds=-1)
    assert set(store._data["pause_timestamps"]) == {"d1"}


def test_storage_mode_allowlists_and_snapshot_restore():
    store = RuntimeStore(FakeStore())
    assert store.restore_mode() is None
    store.set_mode("armed")
    assert store.snapshot() == {}
    store._data["mode"] = "armed"
    assert store.restore_mode() == "off"
    store.set_mode("auto")
    assert store.restore_mode() == "auto"

    assert store.restore_execution_mode() is None
    store.set_execution_mode("armed")
    assert store.restore_execution_mode() is None
    store.set_execution_mode("live")
    snapshot = store.snapshot()
    store.set_execution_mode("observe")
    store.restore_snapshot(snapshot)
    assert store.restore_execution_mode() == "live"
    store.clear_execution_mode()
    assert store.restore_execution_mode() is None


def test_storage_save_device_runtime_normalizes_ownership_and_reasons():
    model = make_model()
    model.get_device("d1").ownership = Ownership.EXTERNAL
    model.get_device("d1").ownership_until = time.time() - 1
    model.get_device("d2").ownership = "invalid"
    store = RuntimeStore(FakeStore())
    store.save_device_runtime(
        model,
        faulted_devices={"d1", "unknown"},
        recovery_blocked_devices={"d2"},
        fault_reasons={"d1": "x" * 300, "unknown": "drop", "empty": ""},
    )
    records = store._data["device_runtime"]["devices"]
    assert records["d1"]["ownership"] == Ownership.PLANNER.value
    assert records["d1"]["ownership_until"] is None
    assert records["d2"]["ownership"] == Ownership.UNKNOWN.value
    assert store._data["device_runtime"]["faulted_devices"] == ["d1"]
    assert len(store._data["device_runtime"]["fault_reasons"]["d1"]) == 160


def test_storage_restore_device_runtime_filters_invalid_records_and_leases():
    model = make_model()
    model.get_device("d1").is_on = True
    model.get_device("d2").is_on = True
    store = RuntimeStore(FakeStore())
    store._data = {
        "device_runtime": {
            "schema_version": 1,
            "devices": {
                "d1": {"ownership": "future", "ownership_until": None},
                "d2": {"ownership": "external", "ownership_until": time.time() - 1},
                "unknown": {"ownership": "external", "ownership_until": time.time() + 1},
                "bad": "not-a-record",
            },
            "faulted_devices": ["d1", "unknown", 42],
            "recovery_blocked_devices": ["d2"],
            "fault_reasons": {"d1": "reason"},
        }
    }
    faulted, blocked = store.restore_device_runtime(model)
    assert faulted == {"d1"}
    assert blocked == {"d2"}
    assert model.get_device("d1").ownership is Ownership.UNKNOWN
    assert model.get_device("d2").ownership is Ownership.PLANNER
    assert model.get_device("d1").is_on is None
    assert model.get_device("d2").is_on is None

    store._data["device_runtime"]["fault_reasons"] = "invalid"
    assert store.restore_fault_reasons(model) == {
        "d1": "persisted_runtime_invalid",
        "d2": "persisted_runtime_invalid",
    }


def test_storage_fault_notification_state_is_schema_and_model_bound():
    model = make_model()
    store = RuntimeStore(FakeStore())
    store.save_fault_notification_state(
        {"d1": "active", "unknown": "drop", "": "drop"},
        {"d2": "pending", "bad": ""},
    )
    assert store.restore_fault_notification_state(model) == (
        {"d1": "active"},
        {"d2": "pending"},
    )
    store._data["fault_notifications"]["schema_version"] = 999
    assert store.restore_fault_notification_state(model) == ({}, {})
    store._data["fault_notifications"] = {"schema_version": 1, "active": []}
    assert store.restore_fault_notification_state(model) == ({}, {})


def test_storage_policy_restore_invalid_enums_and_stack_records():
    store = RuntimeStore(FakeStore())
    store._data = {
        "policy_runtime": {
            "phase": "future",
            "last_telemetry_validity": "future",
            "last_reason_code": "future",
            "manual_start_blocked_count": -1,
            "decision_sequence": -1,
            "shed_stack": [
                "not-a-record",
                {"device_id": "unknown", "operation_id": "op", "pre_state": True, "snapshot": {"x": 1}, "load_generation": 1},
                {"device_id": "d1", "operation_id": "op-1", "pre_state": True, "snapshot": {"x": 1}, "load_generation": 1, "reason_code": ReasonCode.SHED_FAST_OVERLOAD.value},
                {"device_id": "d1", "operation_id": "op-2", "pre_state": True, "snapshot": {"x": 1}, "load_generation": 2, "reason_code": ReasonCode.SHED_FAST_OVERLOAD.value},
                {"device_id": "d2", "operation_id": "op-3", "pre_state": True, "snapshot": {}, "load_generation": 1, "reason_code": ReasonCode.SHED_FAST_OVERLOAD.value},
                {"device_id": "d2", "operation_id": "op-4", "pre_state": True, "snapshot": {"x": 1}, "load_generation": 1, "reason_code": "future"},
            ],
        }
    }
    engine = PolicyEngine(DEFAULT_POLICY)
    store.restore_policy_runtime(engine, make_model())
    assert engine.runtime.phase.value == "fault"
    assert engine.runtime.last_telemetry_validity.value == "invalid"
    assert engine.runtime.last_reason_code.value == ReasonCode.FAULT.value
    assert engine.runtime.manual_start_blocked_count == 0
    assert engine.runtime.decision_sequence == 0
    assert [entry.device_id for entry in engine.runtime.shed_stack] == ["d1"]


def test_storage_action_normalization_rejects_invalid_and_generates_legacy_ids():
    store = RuntimeStore(FakeStore())
    store.record_action({"action_id": "", "phase": "confirmed"})
    store.record_action({"phase": "impossible", "result": "impossible"})
    store._data["audit_history"] = [
        {"operation_id": "legacy", "timestamp": "bad", "phase": None, "result": "confirmed"}
    ]
    history = store.audit_history()
    assert len(history) == 1
    assert history[0]["action_id"].startswith("legacy-0-")
    assert history[0]["phase"] == "confirmed"


def test_storage_malformed_envelopes_fail_closed_without_restoring_state():
    model = make_model()
    store = RuntimeStore(FakeStore())

    store._data["pause_timestamps"] = ["not-a-mapping"]
    store.restore_pause_timestamps(model)
    assert store._data["pause_timestamps"] == {}

    store._data.pop("device_runtime", None)
    assert store.restore_device_runtime(model) == (set(), set())
    assert store.restore_fault_reasons(model) == {}

    store._data["fault_notifications"] = "invalid"
    assert store.restore_fault_notification_state(model) == ({}, {})

    store._data["policy_runtime"] = "invalid"
    engine = PolicyEngine(DEFAULT_POLICY)
    store.restore_policy_runtime(engine, model)
    assert engine.runtime.shed_stack == []

    store._data["policy_runtime"] = {"shed_stack": "invalid", "phase": "startup"}
    store.restore_policy_runtime(engine, model)
    assert engine.runtime.shed_stack == []


@pytest.mark.asyncio
async def test_store_set_pause():
    import time
    fake = FakeStore()
    fake._data = {}  # Initialize to empty dict
    store = RuntimeStore(fake)
    await store.async_load()
    store.set_pause("d1", 60)
    saved = fake._data
    assert saved is not None
    assert "pause_timestamps" in saved
    assert "d1" in saved["pause_timestamps"]
    assert saved["pause_timestamps"]["d1"] > time.time()


@pytest.mark.asyncio
async def test_store_clear_pause():
    fake = FakeStore()
    fake._data = {"pause_timestamps": {"d1": 12345.0}}
    store = RuntimeStore(fake)
    await store.async_load()
    store.clear_pause("d1")
    assert "d1" not in fake._data["pause_timestamps"]


@pytest.mark.asyncio
async def test_store_persist_and_restore():
    import time
    fake = FakeStore()
    store = RuntimeStore(fake)
    await store.async_load()

    store.set_pause("d1", 120)
    await store.async_save()

    # Simulate restart
    store2 = RuntimeStore(fake)
    await store2.async_load()
    model = make_model()
    store2.restore_pause_timestamps(model)
    assert model.get_device("d1").pause_until is not None
    assert model.get_device("d1").pause_until > time.time()