"""Pure tests for the automatic safe-capacity restore window."""
from __future__ import annotations

import math

import power_orchestrator.const as const_module
from power_orchestrator.policy import (
    PolicyEngine,
    ReasonCode,
    policy_for_tests,
)


def _engine() -> PolicyEngine:
    return PolicyEngine(policy_for_tests((5000.0, 0.0)))


def test_safe_capacity_fails_closed_on_invalid_load() -> None:
    engine = _engine()
    for bad in (math.nan, -1.0, True):
        decision = engine.observe_restore_safe_capacity(
            bad, candidate_expected_w=500.0, lowest_limit_w=5000.0, now=10.0
        )
        assert decision.triggered is False
        assert engine.runtime.restore_since is None


def test_safe_capacity_resets_when_projected_not_strictly_below_lowest() -> None:
    engine = _engine()
    engine.observe_restore_safe_capacity(
        4000.0, candidate_expected_w=500.0, lowest_limit_w=5000.0, now=0.0
    )
    assert engine.runtime.restore_since == 0.0
    decision = engine.observe_restore_safe_capacity(
        4500.0, candidate_expected_w=500.0, lowest_limit_w=5000.0, now=5.0
    )
    assert decision.triggered is False
    assert decision.reason_code is ReasonCode.RESTORE_BLOCKED_OVERLOAD
    assert engine.runtime.restore_since is None


def test_safe_capacity_triggers_only_after_full_dwell(monkeypatch) -> None:
    monkeypatch.setattr(const_module, "RESTORE_SAFE_CAPACITY_DWELL_S", 60.0)
    engine = _engine()
    first = engine.observe_restore_safe_capacity(
        3000.0, candidate_expected_w=500.0, lowest_limit_w=5000.0, now=0.0
    )
    assert first.triggered is False
    mid = engine.observe_restore_safe_capacity(
        3000.0, candidate_expected_w=500.0, lowest_limit_w=5000.0, now=59.0
    )
    assert mid.triggered is False
    matured = engine.observe_restore_safe_capacity(
        3000.0, candidate_expected_w=500.0, lowest_limit_w=5000.0, now=60.0
    )
    assert matured.triggered is True
    assert matured.reason_code is ReasonCode.RESTORE_HEADROOM_AVAILABLE


def test_restore_fence_blocks_until_causal_newer_report() -> None:
    engine = _engine()
    engine.append_restore(operation_id="op-1", load_generation=5)
    assert engine.runtime.pending_post_restore_generation == 5
    assert engine.can_restore_again(5) is False
    assert engine.can_restore_again(6) is True
    engine.set_post_restore_fence(100.0)
    assert engine.reconcile_restore(5, reported_at=200.0) is False
    assert engine.reconcile_restore(6, reported_at=100.0) is False
    assert engine.reconcile_restore(6, reported_at=200.0) is True
    assert engine.runtime.pending_post_restore_generation is None


def test_matured_overload_resets_restore_window(monkeypatch) -> None:
    monkeypatch.setattr(const_module, "RESTORE_SAFE_CAPACITY_DWELL_S", 60.0)
    engine = _engine()
    engine.observe_restore_safe_capacity(
        3000.0, candidate_expected_w=500.0, lowest_limit_w=5000.0, now=0.0
    )
    assert engine.runtime.restore_since == 0.0
    engine.observe_load(5100.0, now=1.0)
    assert engine.runtime.restore_since is None


def test_physical_shed_resets_restore_window(monkeypatch) -> None:
    monkeypatch.setattr(const_module, "RESTORE_SAFE_CAPACITY_DWELL_S", 60.0)
    engine = _engine()
    engine.observe_restore_safe_capacity(
        3000.0, candidate_expected_w=500.0, lowest_limit_w=5000.0, now=0.0
    )
    engine.append_shed(
        operation_id="op",
        load_generation=1,
        reason_code=ReasonCode.SHED_CUSTOM_THRESHOLD,
    )
    assert engine.runtime.restore_since is None
