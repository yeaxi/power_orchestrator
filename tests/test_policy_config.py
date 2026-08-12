"""Policy configuration validation tests."""

from __future__ import annotations

from power_orchestrator.const import DEFAULT_HYSTERESIS, MAX_CUSTOM_THRESHOLDS
from power_orchestrator.policy import DEFAULT_POLICY, PolicyConfig


def test_runtime_threshold_cap_is_not_tied_to_the_setup_ui() -> None:
    assert MAX_CUSTOM_THRESHOLDS == 64


def test_canonical_policy_defaults() -> None:
    assert len(DEFAULT_POLICY.thresholds) == 3
    assert DEFAULT_POLICY.thresholds[-1].limit_w < DEFAULT_POLICY.hard_interlock_w


def test_policy_mapping_accepts_bounded_custom_thresholds() -> None:
    policy = PolicyConfig.from_mapping(
        {
            "thresholds": [
                {"power_limit": 1000, "duration_s": 60},
                {"power_limit": 2000, "duration_s": 5},
            ]
        }
    )
    assert len(policy.thresholds) == 2
    assert policy.thresholds[1].limit_w == 2000


def test_policy_mapping_fails_closed_for_invalid_values() -> None:
    assert PolicyConfig.from_mapping({"thresholds": []}) == DEFAULT_POLICY
    assert PolicyConfig.from_mapping({"hard_interlock": -1}) == DEFAULT_POLICY


def test_policy_mapping_parses_and_clamps_hysteresis() -> None:
    assert PolicyConfig.from_mapping({"hysteresis": 300}).hysteresis_w == 300.0
    # Missing key uses the canonical default.
    assert PolicyConfig.from_mapping({}).hysteresis_w == DEFAULT_HYSTERESIS
    # Invalid / out-of-range values fall back to the default.
    assert PolicyConfig.from_mapping({"hysteresis": -5}).hysteresis_w == DEFAULT_HYSTERESIS
    assert PolicyConfig.from_mapping({"hysteresis": "x"}).hysteresis_w == DEFAULT_HYSTERESIS
    assert PolicyConfig.from_mapping({"hysteresis": 999999}).hysteresis_w == DEFAULT_HYSTERESIS
    assert PolicyConfig.from_mapping({"hysteresis": True}).hysteresis_w == DEFAULT_HYSTERESIS
