"""Policy configuration defaults and validation."""
from power_orchestrator.policy import DEFAULT_POLICY, PolicyConfig


def test_canonical_policy_defaults_match_transitional_automation():
    assert [tier.limit_w for tier in DEFAULT_POLICY.thresholds] == [6500.0, 7000.0, 8000.0]
    assert [tier.duration_s for tier in DEFAULT_POLICY.thresholds] == [300.0, 30.0, 5.0]
    assert DEFAULT_POLICY.recovery_target_w == 6000.0
    assert DEFAULT_POLICY.recovery_start_w == 5000.0
    assert DEFAULT_POLICY.recovery_low_duration_s == 60.0
    assert DEFAULT_POLICY.recovery_stabilization_s == 60.0


def test_policy_mapping_invalid_values_fails_closed_to_safe_defaults():
    policy = PolicyConfig.from_mapping(
        {
            "shed_sustained_limit": "nan",
            "shed_fast_limit": 1,
            "shed_critical_limit": 2,
            "recovery_start": 9000,
            "recovery_target": 1000,
        }
    )
    assert policy == DEFAULT_POLICY


def test_policy_mapping_accepts_up_to_ten_custom_thresholds():
    policy = PolicyConfig.from_mapping(
        {
            "thresholds": [
                {"power_limit": 6100 + index * 100, "duration_s": 10 + index}
                for index in range(10)
            ],
            "hard_interlock": 8000,
        }
    )

    assert len(policy.thresholds) == 10
    assert policy.thresholds[0].limit_w == 6100
    assert policy.thresholds[-1].limit_w == 7000
    assert policy.thresholds[3].duration_s == 13


def test_policy_mapping_rejects_more_than_ten_custom_thresholds():
    policy = PolicyConfig.from_mapping(
        {
            "thresholds": [
                {"power_limit": 6000 + index * 100, "duration_s": 10}
                for index in range(11)
            ]
        }
    )

    assert policy == DEFAULT_POLICY


def test_policy_mapping_rejects_non_increasing_custom_thresholds():
    policy = PolicyConfig.from_mapping(
        {
            "thresholds": [
                {"power_limit": 7000, "duration_s": 10},
                {"power_limit": 6500, "duration_s": 5},
            ]
        }
    )

    assert policy == DEFAULT_POLICY
