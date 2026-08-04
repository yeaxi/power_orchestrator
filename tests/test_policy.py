"""Tests for deterministic overload policy."""
from __future__ import annotations

from power_orchestrator.const import DEFAULT_HARD_INTERLOCK
from power_orchestrator.policy import (
    DEFAULT_POLICY,
    Ownership,
    PolicyConfig,
    PolicyEngine,
    PolicyPhase,
    ReasonCode,
    TelemetryValidity,
)


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


def test_ownership_enum_has_no_admission_contract() -> None:
    assert {item.value for item in Ownership} == {"unknown", "planner", "manual", "external"}
