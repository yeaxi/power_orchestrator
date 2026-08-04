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
