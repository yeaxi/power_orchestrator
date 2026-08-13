"""Tests for deterministic overload policy."""
from __future__ import annotations

from power_orchestrator.policy import (
    PolicyConfig,
    PolicyEngine,
    PolicyPhase,
    ReasonCode,
    TelemetryValidity,
    ThresholdTier,
    policy_for_tests,
)


def _engine(*pairs: tuple[float, float]) -> PolicyEngine:
    return PolicyEngine(policy_for_tests(*pairs))


def test_policy_requires_non_empty_thresholds() -> None:
    policy = policy_for_tests((6500.0, 300.0), (7000.0, 30.0), (8000.0, 5.0))
    assert [tier.limit_w for tier in policy.thresholds] == [6500.0, 7000.0, 8000.0]
    assert policy.lowest_limit_w == 6500.0
    assert not hasattr(policy, "hard_interlock_w")
    assert not hasattr(policy, "hysteresis_w")
    assert not hasattr(policy, "safety_reserve_w")


def test_policy_dwell_triggers_only_after_threshold_duration() -> None:
    engine = _engine((6500.0, 300.0), (7000.0, 30.0), (8000.0, 5.0))
    assert not engine.observe_load(8100, now=0).triggered
    decision = engine.observe_load(8100, now=5.1)
    assert decision.triggered
    assert decision.reason_code is ReasonCode.SHED_CUSTOM_THRESHOLD
    assert engine.runtime.phase is PolicyPhase.SHEDDING
    assert engine.runtime.restore_since is None


def test_zero_dwell_top_tier_triggers_immediately() -> None:
    engine = _engine((5000.0, 60.0), (8000.0, 0.0))
    decision = engine.observe_load(8100, now=0)
    assert decision.triggered
    assert decision.active_tier == "custom_2"


def test_policy_invalid_telemetry_fails_closed_and_resets_dwell() -> None:
    engine = _engine((8000.0, 5.0))
    engine.observe_load(8100, now=0)
    decision = engine.observe_invalid_load(ReasonCode.TELEMETRY_STALE, now=1)
    assert not decision.triggered
    assert engine.runtime.phase is PolicyPhase.FAULT
    assert engine.runtime.last_telemetry_validity is TelemetryValidity.STALE
    assert engine.runtime.tier_since == {}
    assert engine.runtime.restore_since is None


def test_policy_post_shed_barrier_requires_newer_causal_report() -> None:
    engine = _engine((8000.0, 5.0))
    engine.append_shed(
        operation_id="op-1",
        load_generation=3,
        reason_code=ReasonCode.SHED_CUSTOM_THRESHOLD,
    )
    assert not engine.can_shed_again(3)
    engine.set_post_shed_fence(10.0)
    assert not engine.reconcile_shed(4, reported_at=10.0)
    assert engine.reconcile_shed(4, reported_at=11.0)
    assert engine.can_shed_again(4)


def test_policy_mapping_rejects_bad_thresholds() -> None:
    policy = PolicyConfig.from_mapping(
        {"thresholds": [{"power_limit": 100, "duration_s": 1}, {"power_limit": 50, "duration_s": 1}]}
    )
    assert policy is None


def test_tier_disarms_at_exact_limit_without_hysteresis() -> None:
    engine = _engine((5000.0, 10.0))
    engine.observe_load(5100, now=0)
    assert "custom_1" in engine.runtime.tier_since
    engine.observe_load(5000, now=1)
    assert engine.runtime.tier_since == {}


def test_from_mapping_derives_zero_dwell_tier_from_max_load() -> None:
    policy = PolicyConfig.from_mapping({"max_load": 5000})
    assert policy is not None
    assert len(policy.thresholds) == 1
    assert policy.thresholds[0].limit_w == 5000.0
    assert policy.thresholds[0].duration_s == 0.0


def test_from_mapping_converts_legacy_named_fields() -> None:
    policy = PolicyConfig.from_mapping(
        {
            "shed_sustained_limit": 6500,
            "shed_sustained_duration": 300,
            "shed_fast_limit": 7000,
            "shed_fast_duration": 30,
            "shed_critical_limit": 8000,
            "shed_critical_duration": 5,
        }
    )
    assert policy is not None
    assert [tier.tier_id for tier in policy.thresholds] == ["sustained", "fast", "critical"]
    assert policy.thresholds[0].reason_code is ReasonCode.SHED_SUSTAINED_OVERLOAD
