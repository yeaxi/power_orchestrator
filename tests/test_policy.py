"""Tests for deterministic overload policy."""
from __future__ import annotations

from power_orchestrator.const import DEFAULT_HARD_INTERLOCK
from power_orchestrator.policy import (
    DEFAULT_POLICY,
    PolicyConfig,
    PolicyEngine,
    PolicyPhase,
    ReasonCode,
    TelemetryValidity,
    ThresholdTier,
)


def _single_tier_engine(*, hysteresis_w: float, duration_s: float = 10.0) -> PolicyEngine:
    policy = PolicyConfig(
        hysteresis_w=hysteresis_w,
        hard_interlock_w=9000.0,
        thresholds=(
            ThresholdTier("t", 5000.0, duration_s, ReasonCode.SHED_SUSTAINED_OVERLOAD),
        ),
    )
    return PolicyEngine(policy)


def test_policy_defaults_are_shedding_only() -> None:
    assert [tier.limit_w for tier in DEFAULT_POLICY.thresholds] == [6500.0, 7000.0, 8000.0]
    assert [tier.duration_s for tier in DEFAULT_POLICY.thresholds] == [300.0, 30.0, 5.0]
    assert DEFAULT_POLICY.hard_interlock_w == DEFAULT_HARD_INTERLOCK
    assert not hasattr(DEFAULT_POLICY, "recovery_target_w")


def test_policy_dwell_triggers_only_after_threshold_duration() -> None:
    engine = PolicyEngine(DEFAULT_POLICY)
    assert not engine.observe_load(8100, now=0).triggered
    decision = engine.observe_load(8100, now=5.1)
    assert decision.triggered
    assert decision.reason_code is ReasonCode.SHED_CRITICAL_OVERLOAD
    assert engine.runtime.phase is PolicyPhase.SHEDDING


def test_policy_invalid_telemetry_fails_closed_and_resets_dwell() -> None:
    engine = PolicyEngine(DEFAULT_POLICY)
    engine.observe_load(8100, now=0)
    decision = engine.observe_invalid_load(ReasonCode.TELEMETRY_STALE, now=1)
    assert not decision.triggered
    assert engine.runtime.phase is PolicyPhase.FAULT
    assert engine.runtime.last_telemetry_validity is TelemetryValidity.STALE
    assert engine.runtime.tier_since == {}


def test_policy_post_shed_barrier_requires_newer_causal_report() -> None:
    engine = PolicyEngine(DEFAULT_POLICY)
    engine.append_shed(operation_id="op-1", load_generation=3, reason_code=ReasonCode.SHED_FAST_OVERLOAD)
    assert not engine.can_shed_again(3)
    engine.set_post_shed_fence(10.0)
    assert not engine.reconcile_shed(4, reported_at=10.0)
    assert engine.reconcile_shed(4, reported_at=11.0)
    assert engine.can_shed_again(4)


def test_policy_mapping_rejects_bad_thresholds_to_safe_defaults() -> None:
    policy = PolicyConfig.from_mapping({"thresholds": [{"power_limit": 100, "duration_s": 1}, {"power_limit": 50, "duration_s": 1}]})
    assert policy == DEFAULT_POLICY


def test_hysteresis_zero_disarms_at_the_exact_limit() -> None:
    engine = _single_tier_engine(hysteresis_w=0.0)
    engine.observe_load(5100, now=0)  # arm
    assert "t" in engine.runtime.tier_since
    engine.observe_load(4999, now=1)  # any dip below limit de-arms with no band
    assert engine.runtime.tier_since == {}


def test_hysteresis_band_keeps_tier_armed_and_preserves_dwell_start() -> None:
    engine = _single_tier_engine(hysteresis_w=200.0)
    engine.observe_load(5100, now=0)  # arm at t=0
    assert engine.runtime.tier_since["t"] == 0
    # Dip into (limit-band, limit] = (4800, 5000]: stays armed, dwell start kept.
    engine.observe_load(4900, now=6)
    assert engine.runtime.tier_since.get("t") == 0
    # Below the lower band edge de-arms and resets the dwell.
    engine.observe_load(4800, now=7)
    assert engine.runtime.tier_since == {}


def test_hysteresis_band_does_not_reset_dwell_so_maturation_still_fires() -> None:
    engine = _single_tier_engine(hysteresis_w=200.0, duration_s=10.0)
    assert not engine.observe_load(5100, now=0).triggered  # arm, dwell not yet met
    # A brief in-band dip must not reset the dwell; maturation still triggers at t>=10.
    assert not engine.observe_load(4900, now=5).triggered
    decision = engine.observe_load(4900, now=10)
    assert decision.triggered
    assert decision.reason_code is ReasonCode.SHED_SUSTAINED_OVERLOAD


def test_hysteresis_at_or_above_limit_falls_back_to_exact_limit_arming() -> None:
    # A band >= the tier limit must not create a tier that never de-arms.
    engine = _single_tier_engine(hysteresis_w=5000.0, duration_s=0.0)
    engine.observe_load(150, now=0)  # arm (limit 5000? no: single-tier limit is 5000)
    # limit is 5000; load 150 is below it, so it should not even arm.
    assert engine.runtime.tier_since == {}
    # Arm above the limit, then drop below: with band >= limit, de-arm at limit.
    engine.observe_load(5100, now=1)
    assert "t" in engine.runtime.tier_since
    engine.observe_load(4999, now=2)
    assert engine.runtime.tier_since == {}


def test_hysteresis_does_not_affect_hard_interlock() -> None:
    engine = _single_tier_engine(hysteresis_w=200.0)
    decision = engine.observe_load(9000, now=0)
    assert decision.triggered
    assert decision.reason_code is ReasonCode.HARD_INTERLOCK
