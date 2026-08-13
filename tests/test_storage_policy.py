"""Policy persistence contract tests."""
from __future__ import annotations

from power_orchestrator.policy import policy_for_tests, PolicyEngine, PolicyPhase, ReasonCode
from power_orchestrator.storage import RuntimeStore


class Backend:
    async def async_load(self):
        return None

    async def async_save(self, data):
        self.data = data


def test_policy_runtime_persists_only_shedding_fence() -> None:
    store = RuntimeStore(Backend())
    engine = PolicyEngine(policy_for_tests((6500.0, 300.0), (7000.0, 30.0), (8000.0, 5.0)))
    engine.runtime.phase = PolicyPhase.WAITING_LOAD_RECONCILIATION
    engine.runtime.active_tier = "custom_1"
    engine.runtime.tier_started_at = 10.0
    engine.runtime.tier_since = {"custom_1": 10.0}
    engine.runtime.restore_since = 20.0
    engine.append_shed(operation_id="op", load_generation=5, reason_code=ReasonCode.SHED_SUSTAINED_OVERLOAD)
    store.save_policy_runtime(engine)
    raw = store.snapshot()["policy_runtime"]
    assert "shed_stack" not in raw
    assert "restore_target" not in raw
    assert "tier_started_at" not in raw
    assert "tier_since" not in raw
    assert "restore_since" not in raw
    restored = PolicyEngine(policy_for_tests((6500.0, 300.0), (7000.0, 30.0), (8000.0, 5.0)))
    store.restore_policy_runtime(restored)
    assert restored.runtime.pending_post_shed_generation == 5
    assert restored.runtime.active_tier is None
    assert restored.runtime.tier_started_at is None
    assert restored.runtime.tier_since == {}
    assert restored.runtime.restore_since is None


def test_policy_runtime_persists_restore_fence() -> None:
    store = RuntimeStore(Backend())
    engine = PolicyEngine(policy_for_tests((6500.0, 300.0), (7000.0, 30.0), (8000.0, 5.0)))
    engine.append_restore(operation_id="op-r", load_generation=7)
    engine.set_post_restore_fence(123.0)
    store.save_policy_runtime(engine)
    raw = store.snapshot()["policy_runtime"]
    assert raw["pending_post_restore_generation"] == 7
    assert raw["pending_post_restore_after_reported_at"] == 123.0

    restored = PolicyEngine(policy_for_tests((6500.0, 300.0), (7000.0, 30.0), (8000.0, 5.0)))
    store.restore_policy_runtime(restored)
    assert restored.runtime.pending_post_restore_generation == 7
    assert restored.runtime.pending_post_restore_after_reported_at == 123.0
    assert restored.runtime.pending_restore_operation_id == "op-r"
    # The restore barrier still blocks until a newer aggregate report arrives.
    assert restored.can_restore_again(7) is False
    assert restored.can_restore_again(8) is True
