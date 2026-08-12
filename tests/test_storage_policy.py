"""Policy persistence contract tests."""
from __future__ import annotations

from power_orchestrator.policy import DEFAULT_POLICY, PolicyEngine, PolicyPhase, ReasonCode
from power_orchestrator.storage import RuntimeStore


class Backend:
    async def async_load(self):
        return None

    async def async_save(self, data):
        self.data = data


def test_policy_runtime_persists_only_shedding_fence() -> None:
    store = RuntimeStore(Backend())
    engine = PolicyEngine(DEFAULT_POLICY)
    engine.runtime.phase = PolicyPhase.WAITING_LOAD_RECONCILIATION
    engine.append_shed(operation_id="op", load_generation=5, reason_code=ReasonCode.SHED_SUSTAINED_OVERLOAD)
    store.save_policy_runtime(engine)
    raw = store.snapshot()["policy_runtime"]
    assert "shed_stack" not in raw
    assert "restore_target" not in raw
    restored = PolicyEngine(DEFAULT_POLICY)
    store.restore_policy_runtime(restored)
    assert restored.runtime.pending_post_shed_generation == 5


def test_policy_runtime_persists_restore_fence() -> None:
    store = RuntimeStore(Backend())
    engine = PolicyEngine(DEFAULT_POLICY)
    engine.append_restore(operation_id="op-r", load_generation=7)
    engine.set_post_restore_fence(123.0)
    store.save_policy_runtime(engine)
    raw = store.snapshot()["policy_runtime"]
    assert raw["pending_post_restore_generation"] == 7
    assert raw["pending_post_restore_after_reported_at"] == 123.0

    restored = PolicyEngine(DEFAULT_POLICY)
    store.restore_policy_runtime(restored)
    assert restored.runtime.pending_post_restore_generation == 7
    assert restored.runtime.pending_post_restore_after_reported_at == 123.0
    assert restored.runtime.pending_restore_operation_id == "op-r"
    # The restore barrier still blocks until a newer aggregate report arrives.
    assert restored.can_restore_again(7) is False
    assert restored.can_restore_again(8) is True
