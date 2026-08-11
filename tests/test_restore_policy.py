"""Pure, deterministic tests for the guarded-restore policy engine."""
from __future__ import annotations

import math

from power_orchestrator.policy import (
    DEFAULT_POLICY,
    DEFAULT_RESTORE,
    PolicyEngine,
    ReasonCode,
    RestoreConfig,
)
from power_orchestrator.power_model import ManagedDevice


def _engine() -> PolicyEngine:
    return PolicyEngine(DEFAULT_POLICY)


def test_restore_disabled_by_default() -> None:
    assert DEFAULT_RESTORE.enabled is False
    assert RestoreConfig.from_mapping({}).enabled is False


def test_from_mapping_parses_enabled_config() -> None:
    config = RestoreConfig.from_mapping(
        {
            "restore_enabled": True,
            "restore_threshold": 4000,
            "restore_hysteresis": 200,
            "restore_dwell": 300,
            "restore_cooldown": 600,
        }
    )
    assert config.enabled is True
    assert config.ceiling_w == 3800.0
    assert config.dwell_s == 300.0
    assert config.cooldown_s == 600.0


def test_from_mapping_disables_on_degenerate_ceiling() -> None:
    # hysteresis >= threshold means the ceiling can never accrue headroom.
    config = RestoreConfig.from_mapping(
        {"restore_enabled": True, "restore_threshold": 200, "restore_hysteresis": 200}
    )
    assert config.enabled is False


def test_from_mapping_rejects_invalid_values() -> None:
    config = RestoreConfig.from_mapping(
        {"restore_enabled": True, "restore_threshold": float("nan"), "restore_hysteresis": -5}
    )
    # Invalid threshold falls back to 0 -> degenerate ceiling -> disabled.
    assert config.enabled is False


def test_headroom_not_triggered_when_disabled() -> None:
    engine = _engine()
    decision = engine.observe_restore_headroom(0.0, now=1000.0, config=DEFAULT_RESTORE)
    assert decision.triggered is False
    assert decision.reason_code is ReasonCode.RESTORE_BLOCKED_NOT_ARMED
    assert engine.runtime.restore_since is None


def test_headroom_fails_closed_on_invalid_load() -> None:
    engine = _engine()
    config = RestoreConfig(enabled=True, threshold_w=4000, hysteresis_w=200, dwell_s=300)
    for bad in (math.nan, -1.0, True):
        decision = engine.observe_restore_headroom(bad, now=10.0, config=config)
        assert decision.triggered is False
        assert engine.runtime.restore_since is None


def test_headroom_resets_when_load_above_ceiling() -> None:
    engine = _engine()
    config = RestoreConfig(enabled=True, threshold_w=4000, hysteresis_w=200, dwell_s=300)
    engine.observe_restore_headroom(3800.0, now=0.0, config=config)
    assert engine.runtime.restore_since == 0.0
    decision = engine.observe_restore_headroom(3900.0, now=5.0, config=config)
    assert decision.triggered is False
    assert decision.reason_code is ReasonCode.RESTORE_BLOCKED_OVERLOAD
    assert engine.runtime.restore_since is None


def test_headroom_triggers_only_after_full_dwell() -> None:
    engine = _engine()
    config = RestoreConfig(enabled=True, threshold_w=4000, hysteresis_w=200, dwell_s=300)
    first = engine.observe_restore_headroom(3000.0, now=0.0, config=config)
    assert first.triggered is False
    mid = engine.observe_restore_headroom(3000.0, now=299.0, config=config)
    assert mid.triggered is False
    matured = engine.observe_restore_headroom(3000.0, now=300.0, config=config)
    assert matured.triggered is True
    assert matured.reason_code is ReasonCode.RESTORE_HEADROOM_AVAILABLE
    assert matured.active_tier == "restore"


def test_restore_fence_blocks_until_causal_newer_report() -> None:
    engine = _engine()
    engine.append_restore(operation_id="op-1", load_generation=5)
    assert engine.runtime.pending_post_restore_generation == 5
    # No further restore until the aggregate generation advances.
    assert engine.can_restore_again(5) is False
    assert engine.can_restore_again(6) is True
    # Reconcile requires both a newer generation and a report after the fence.
    engine.set_post_restore_fence(100.0)
    assert engine.reconcile_restore(5, reported_at=200.0) is False
    assert engine.reconcile_restore(6, reported_at=100.0) is False
    assert engine.reconcile_restore(6, reported_at=200.0) is True
    assert engine.runtime.pending_post_restore_generation is None


def test_device_restore_enabled_round_trips() -> None:
    device = ManagedDevice("d1", "Boiler", "switch.d1", restore_enabled=True)
    restored = ManagedDevice.from_dict(device.to_dict())
    assert restored.restore_enabled is True
    # Default remains off when the key is absent.
    assert ManagedDevice.from_dict({"device_id": "d2", "name": "N", "entity": "switch.d2"}).restore_enabled is False
