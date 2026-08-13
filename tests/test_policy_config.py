"""Boundary tests for PolicyConfig parsing and migration helpers."""
from __future__ import annotations

from power_orchestrator.policy import (
    PolicyConfig,
    derive_thresholds_from_mapping,
    strip_legacy_policy_fields,
)


def test_from_mapping_preserves_valid_thresholds() -> None:
    policy = PolicyConfig.from_mapping(
        {
            "thresholds": [
                {"power_limit": 4000, "duration_s": 0},
                {"power_limit": 6000, "duration_s": 30},
            ]
        }
    )
    assert policy is not None
    assert [tier.limit_w for tier in policy.thresholds] == [4000.0, 6000.0]
    assert [tier.duration_s for tier in policy.thresholds] == [0.0, 30.0]


def test_from_mapping_returns_none_without_user_derived_tier() -> None:
    assert PolicyConfig.from_mapping({}) is None
    assert PolicyConfig.from_mapping({"averaging_period": 10}) is None


def test_derive_thresholds_prefers_explicit_list_over_max_load() -> None:
    tiers = derive_thresholds_from_mapping(
        {
            "thresholds": [{"power_limit": 3000, "duration_s": 10}],
            "max_load": 9000,
        }
    )
    assert tiers is not None
    assert len(tiers) == 1
    assert tiers[0].limit_w == 3000.0


def test_strip_legacy_policy_fields_removes_deleted_keys() -> None:
    cleaned = strip_legacy_policy_fields(
        {
            "load_sensor": "sensor.load",
            "max_load": 5000,
            "safety_reserve": 200,
            "hysteresis": 100,
            "hard_interlock": 9000,
            "restore_enabled": True,
            "restore_threshold": 3000,
            "thresholds": [{"power_limit": 5000, "duration_s": 0}],
        }
    )
    assert "max_load" not in cleaned
    assert "restore_enabled" not in cleaned
    assert cleaned["thresholds"][0]["power_limit"] == 5000
