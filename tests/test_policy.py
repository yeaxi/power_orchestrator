"""Pure policy/state tests for the load-shedding contract."""
from __future__ import annotations

import math

import pytest

from power_orchestrator.policy import (
    DEFAULT_POLICY,
    AuthorizationLease,
    Ownership,
    PolicyConfig,
    PolicyEngine,
    PolicyPhase,
    ReasonCode,
    ShedStackEntry,
    TelemetryValidity,
    ThresholdTier,
    TelemetrySample,
    validate_threshold_pair,
)


def test_authorization_lease_requires_matching_causal_report_marker():
    lease = AuthorizationLease(
        device_id="d1",
        operation_id="op-1",
        allowed_state="on",
        expires_at=100.0,
        reported_at=10.0,
    )

    assert lease.allows("d1", "on", 20.0, reported_at=10.0) is True
    assert lease.allows("d1", "on", 20.0, reported_at=11.0) is False
    assert lease.allows("d1", "on", 20.0, reported_at=None) is False


def test_tiered_overload_requires_its_declared_duration():
    engine = PolicyEngine(DEFAULT_POLICY)

    first = engine.observe_load(6501, now=0.0)
    assert first.triggered is False
    assert first.active_tier == "sustained"

    before = engine.observe_load(6501, now=299.9)
    assert before.triggered is False

    at_boundary = engine.observe_load(6501, now=300.0)
    assert at_boundary.triggered is True
    assert at_boundary.reason_code is ReasonCode.SHED_SUSTAINED_OVERLOAD


def test_higher_tier_has_independent_timer_and_can_trigger_fast():
    engine = PolicyEngine(DEFAULT_POLICY)

    assert engine.observe_load(7001, now=0.0).triggered is False
    assert engine.observe_load(7001, now=29.9).triggered is False
    assert engine.observe_load(7001, now=30.0).triggered is True
    fast_after_escalation = engine.observe_load(8001, now=34.0)
    assert fast_after_escalation.triggered is True
    assert fast_after_escalation.reason_code is ReasonCode.SHED_FAST_OVERLOAD
    assert engine.observe_load(8001, now=39.0).triggered is True
    assert engine.last_decision.reason_code is ReasonCode.SHED_CRITICAL_OVERLOAD


def test_escalation_does_not_cancel_matured_lower_threshold_timer():
    engine = PolicyEngine(DEFAULT_POLICY)

    assert engine.observe_load(7001, now=0.0).triggered is False
    assert engine.observe_load(7001, now=29.9).triggered is False
    decision = engine.observe_load(8001, now=30.0)

    assert decision.triggered is True
    assert decision.reason_code is ReasonCode.SHED_FAST_OVERLOAD



def test_recovery_requires_low_load_then_stabilization():
    engine = PolicyEngine(DEFAULT_POLICY)

    assert engine.observe_load(4999, now=0.0).recovery_ready is False
    assert engine.observe_load(4999, now=59.9).recovery_ready is False
    assert engine.observe_load(4999, now=60.0).recovery_ready is False
    assert engine.observe_load(4999, now=119.9).recovery_ready is False
    assert engine.observe_load(4999, now=120.0).recovery_ready is True


def test_recovery_timer_resets_when_load_leaves_recovery_band():
    engine = PolicyEngine(DEFAULT_POLICY)
    engine.observe_load(4999, now=0.0)
    engine.observe_load(5100, now=30.0)
    assert engine.observe_load(4999, now=60.0).recovery_ready is False
    assert engine.observe_load(4999, now=120.0).recovery_ready is False


def test_shed_stack_only_restores_devices_shed_by_orchestrator():
    engine = PolicyEngine(DEFAULT_POLICY)
    entry = ShedStackEntry(
        device_id="accumulator",
        operation_id="op-1",
        pre_state=True,
        snapshot={"switch": "on"},
        load_generation=4,
    )
    engine.runtime.shed_stack.append(entry)
    engine.runtime.restore_target = "accumulator"

    target = engine.next_restore_target()
    assert target is entry
    assert engine.pop_restore_target() is entry
    assert engine.next_restore_target() is None


def test_authorization_lease_rejects_unrelated_manual_on():
    lease = AuthorizationLease(
        device_id="parents",
        operation_id="op-2",
        allowed_state="on",
        expires_at=100.0,
    )
    assert lease.allows("parents", "on", 50.0) is True
    assert lease.allows("bathroom", "on", 50.0) is False
    assert lease.allows("parents", "on", 101.0) is False


def test_typed_telemetry_and_phase_are_fail_closed():
    engine = PolicyEngine(DEFAULT_POLICY)
    engine.runtime.phase = PolicyPhase.WAITING_LOAD_RECONCILIATION
    engine.runtime.last_telemetry_validity = TelemetryValidity.STALE

    decision = engine.observe_invalid_load(ReasonCode.TELEMETRY_STALE, now=1.0)

    assert decision.triggered is False
    assert decision.recovery_ready is False
    assert engine.runtime.phase is PolicyPhase.FAULT
    assert decision.reason_code is ReasonCode.TELEMETRY_STALE
    assert engine.runtime.last_telemetry_validity is TelemetryValidity.STALE


def test_ownership_values_are_explicit():
    assert Ownership.PLANNER.value == "planner"
    assert Ownership.EXTERNAL.value == "external"
    assert Ownership.MANUAL.value == "manual"


def test_invalid_telemetry_resets_overload_and_recovery_timers():
    engine = PolicyEngine(DEFAULT_POLICY)
    assert engine.observe_load(6501, now=0.0).triggered is False

    engine.observe_invalid_load(ReasonCode.TELEMETRY_STALE, now=100.0)

    assert engine.observe_load(6501, now=300.0).triggered is False
    assert engine.observe_load(6501, now=600.0).triggered is True


def test_custom_thresholds_are_bounded_and_positive():
    from power_orchestrator.policy import PolicyConfig

    policy = PolicyConfig.from_mapping(
        {
            "thresholds": [
                {"power_limit": 6200, "duration_s": 10},
                {"power_limit": 7600, "duration_s": 2},
            ]
        }
    )
    assert [tier.limit_w for tier in policy.thresholds] == [6200.0, 7600.0]
    assert PolicyConfig.from_mapping(
        {"thresholds": [{"power_limit": 6200, "duration_s": 0}]}
    ) == DEFAULT_POLICY


def test_policy_validation_and_mapping_fallbacks_are_fail_closed():
    with pytest.raises(ValueError):
        validate_threshold_pair(0, 1)
    with pytest.raises(ValueError):
        validate_threshold_pair(100, math.inf)
    with pytest.raises(ValueError):
        validate_threshold_pair(100, 1, previous_power=100)

    with pytest.raises(ValueError):
        ThresholdTier("", 1, 1, ReasonCode.FAULT)
    with pytest.raises(ValueError):
        ThresholdTier("bad", -1, 1, ReasonCode.FAULT)
    with pytest.raises(ValueError):
        PolicyConfig(recovery_start_w=7000, recovery_target_w=6000)
    with pytest.raises(ValueError):
        PolicyConfig(recovery_low_duration_s=-1)
    with pytest.raises(ValueError):
        PolicyConfig(safety_reserve_w=-1)
    with pytest.raises(ValueError):
        PolicyConfig(
            thresholds=(
                ThresholdTier("a", 10, 1, ReasonCode.FAULT),
                ThresholdTier("b", 10, 1, ReasonCode.FAULT),
            )
        )
    with pytest.raises(ValueError):
        PolicyConfig(hard_interlock_w=0)
    with pytest.raises(ValueError):
        PolicyConfig(hard_interlock_w=7000)

    assert PolicyConfig.from_mapping({"thresholds": []}) == DEFAULT_POLICY
    assert PolicyConfig.from_mapping({"thresholds": ["bad"]}) == DEFAULT_POLICY
    assert PolicyConfig.from_mapping(
        {"thresholds": [{"power_limit": True, "duration_s": 1}]}
    ) == DEFAULT_POLICY
    assert PolicyConfig.from_mapping(
        {"thresholds": [{"limit_w": 6200, "time_s": 10}]}
    ).thresholds[0].limit_w == 6200
    assert PolicyConfig.from_mapping(
        {"recovery_start": 7000, "recovery_target": 6000}
    ) == DEFAULT_POLICY
    assert PolicyConfig.from_mapping(
        {"hard_interlock": 7000}
    ) == DEFAULT_POLICY


def test_policy_engine_hard_interlock_and_reconciliation_fences():
    policy = PolicyConfig(hard_interlock_w=9000)
    engine = PolicyEngine(policy)
    decision = engine.observe_load(9000, now=0.0)
    assert decision.triggered is True
    assert decision.reason_code is ReasonCode.HARD_INTERLOCK

    entry = ShedStackEntry(
        device_id="d1",
        operation_id="op-1",
        pre_state=True,
        snapshot={},
        load_generation=4,
    )
    engine.append_shed(entry)
    assert engine.can_shed_again(4) is False
    assert engine.can_shed_again(5) is True
    engine.set_post_shed_fence(None)
    assert engine.reconcile_shed(5, reported_at=10) is False
    engine.set_post_shed_fence(math.inf)
    assert engine.reconcile_shed(5, reported_at=10) is False
    engine.set_post_shed_fence(10)
    assert engine.reconcile_shed(4, reported_at=11) is False
    assert engine.reconcile_shed(5, reported_at=10) is False
    assert engine.reconcile_shed(5, reported_at=11) is True
    assert engine.can_shed_again(4) is True

    assert engine.pop_restore_target() is entry
    assert engine.pop_restore_target() is None
    assert engine.runtime.restore_target is None


def test_policy_numeric_fallbacks_and_telemetry_sample_validity():
    policy = PolicyConfig.from_mapping(
        {
            "policy_version": "custom-policy",
            "recovery_target": "bad",
            "recovery_start": True,
            "safety_reserve": "bad",
            "hard_interlock": "bad",
        }
    )
    assert policy.policy_version == "custom-policy"
    assert policy.recovery_target_w == 6000.0
    assert policy.recovery_start_w == 5000.0
    assert policy.safety_reserve_w == 500.0
    assert policy.hard_interlock_w == 9000.0

    valid = TelemetrySample("sensor.load", 1.0, "W", 1.0, 1, TelemetryValidity.VALID)
    invalid = TelemetrySample("sensor.load", None, "W", 1.0, 1, TelemetryValidity.INVALID)
    assert valid.is_valid is True
    assert invalid.is_valid is False
    assert PolicyEngine(DEFAULT_POLICY).observe_load(-1, now=1).reason_code is ReasonCode.TELEMETRY_INVALID
