"""Deterministic policy engine for bounded load shedding and automatic restore."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from . import const as _const
from .const import (
    CONF_HARD_INTERLOCK,
    CONF_HYSTERESIS,
    CONF_MAX_LOAD,
    CONF_SAFETY_RESERVE,
    CONF_SHED_CRITICAL_DURATION,
    CONF_SHED_CRITICAL_LIMIT,
    CONF_SHED_FAST_DURATION,
    CONF_SHED_FAST_LIMIT,
    CONF_SHED_SUSTAINED_DURATION,
    CONF_SHED_SUSTAINED_LIMIT,
    CONF_THRESHOLDS,
    DEFAULT_POLICY_VERSION,
    MAX_CUSTOM_THRESHOLDS,
)

MAX_POLICY_POWER_W = 100_000.0
MAX_POLICY_DURATION_S = 24 * 60 * 60.0


class PolicyPhase(str, Enum):
    """High-level load-shedding phase."""

    STARTUP = "startup"
    MONITORING = "monitoring"
    SHEDDING = "shedding"
    RESTORING = "restoring"
    WAITING_LOAD_RECONCILIATION = "waiting_load_reconciliation"
    WAITING_RESTORE_RECONCILIATION = "waiting_restore_reconciliation"
    GRID_LOSS = "grid_loss"
    FAULT = "fault"


class ReasonCode(str, Enum):
    """Stable machine-readable decision and safety reasons."""

    NORMAL_MONITORING = "normal_monitoring"
    SHED_SUSTAINED_OVERLOAD = "shed_sustained_overload"
    SHED_FAST_OVERLOAD = "shed_fast_overload"
    SHED_CRITICAL_OVERLOAD = "shed_critical_overload"
    SHED_CUSTOM_THRESHOLD = "shed_custom_threshold"
    RESTORE_HEADROOM_AVAILABLE = "restore_headroom_available"
    RESTORE_BLOCKED_OVERLOAD = "restore_blocked_overload"
    RESTORE_BLOCKED_FENCE = "restore_blocked_fence"
    RESTORE_BLOCKED_NO_CANDIDATES = "restore_blocked_no_candidates"
    RESTORE_OBSERVE_MODE = "restore_observe_mode"
    TELEMETRY_INVALID = "telemetry_invalid"
    TELEMETRY_STALE = "telemetry_stale"
    RELAY_READBACK_TIMEOUT = "relay_readback_timeout"
    PERSISTED_RUNTIME_INVALID = "persisted_runtime_invalid"
    AGGREGATE_RECONCILIATION_TIMEOUT = "aggregate_reconciliation_timeout"
    GRID_LOSS = "grid_loss"
    SAFETY_BLOCKED = "safety_blocked"
    OBSERVE_MODE = "observe_mode"
    CONFIGURATION_INVALID = "configuration_invalid"
    FAULT = "fault"
    MANUAL_ON_ACCEPTED = "manual_on_accepted"
    MANUAL_ON_RESHED = "manual_on_reshed"


class TelemetryValidity(str, Enum):
    """Validated telemetry state."""

    VALID = "valid"
    UNKNOWN = "unknown"
    STALE = "stale"
    WRONG_UNIT = "wrong_unit"
    INVALID = "invalid"
    MISSING = "missing"


@dataclass(frozen=True)
class ThresholdTier:
    """One overload threshold and its required dwell time."""

    tier_id: str
    limit_w: float
    duration_s: float
    reason_code: ReasonCode

    def __post_init__(self) -> None:
        if not self.tier_id:
            raise ValueError("tier_id must not be empty")
        if (
            not math.isfinite(self.limit_w)
            or not math.isfinite(self.duration_s)
            or self.limit_w <= 0
            or self.duration_s < 0
        ):
            raise ValueError("threshold values must be finite, positive limit, non-negative duration")


@dataclass(frozen=True)
class PolicyConfig:
    """Versioned load-shedding policy: a non-empty ordered threshold list only."""

    thresholds: tuple[ThresholdTier, ...]
    policy_version: str = DEFAULT_POLICY_VERSION

    def __post_init__(self) -> None:
        if not self.thresholds:
            raise ValueError("thresholds must be non-empty")
        previous = 0.0
        for tier in self.thresholds:
            if tier.limit_w <= previous:
                raise ValueError("threshold limits must be strictly increasing")
            previous = tier.limit_w

    @property
    def lowest_limit_w(self) -> float:
        """Return the lowest configured tier limit."""
        return self.thresholds[0].limit_w

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "PolicyConfig | None":
        """Build a policy from user-derived thresholds, or None when reconfiguration is required."""
        tiers = derive_thresholds_from_mapping(data)
        if tiers is None:
            return None
        version = data.get("policy_version")
        if not isinstance(version, str) or not version.strip():
            version = DEFAULT_POLICY_VERSION
        try:
            return cls(thresholds=tiers, policy_version=version.strip())
        except ValueError:
            return None


def derive_thresholds_from_mapping(data: Mapping[str, Any]) -> tuple[ThresholdTier, ...] | None:
    """Preserve valid thresholds, convert legacy named fields, or derive from max_load."""
    raw_thresholds = data.get(CONF_THRESHOLDS)
    if isinstance(raw_thresholds, (list, tuple)) and 1 <= len(raw_thresholds) <= MAX_CUSTOM_THRESHOLDS:
        parsed = _parse_threshold_list(raw_thresholds)
        if parsed is not None:
            return parsed

    legacy = _legacy_named_thresholds(data)
    if legacy is not None:
        return legacy

    max_load = _finite_number(data.get(CONF_MAX_LOAD), minimum=1.0, maximum=MAX_POLICY_POWER_W)
    if max_load is not None:
        return (
            ThresholdTier(
                "user_max_load",
                max_load,
                0.0,
                ReasonCode.SHED_CUSTOM_THRESHOLD,
            ),
        )
    return None


def _parse_threshold_list(raw_thresholds: list[Any] | tuple[Any, ...]) -> tuple[ThresholdTier, ...] | None:
    parsed: list[ThresholdTier] = []
    previous = 0.0
    try:
        for index, raw in enumerate(raw_thresholds, start=1):
            if not isinstance(raw, Mapping):
                return None
            limit = float(raw.get("power_limit", raw.get("limit_w")))
            duration = float(raw.get("duration_s", raw.get("time_s")))
            limit, duration = validate_threshold_pair(limit, duration, previous)
            parsed.append(
                ThresholdTier(
                    f"custom_{index}",
                    limit,
                    duration,
                    ReasonCode.SHED_CUSTOM_THRESHOLD,
                )
            )
            previous = limit
    except (TypeError, ValueError):
        return None
    return tuple(parsed)


def _legacy_named_thresholds(data: Mapping[str, Any]) -> tuple[ThresholdTier, ...] | None:
    """Convert legacy sustained/fast/critical fields when any are present."""
    keys = (
        (CONF_SHED_SUSTAINED_LIMIT, CONF_SHED_SUSTAINED_DURATION, "sustained", ReasonCode.SHED_SUSTAINED_OVERLOAD),
        (CONF_SHED_FAST_LIMIT, CONF_SHED_FAST_DURATION, "fast", ReasonCode.SHED_FAST_OVERLOAD),
        (CONF_SHED_CRITICAL_LIMIT, CONF_SHED_CRITICAL_DURATION, "critical", ReasonCode.SHED_CRITICAL_OVERLOAD),
    )
    if not any(limit_key in data for limit_key, _, _, _ in keys):
        return None
    parsed: list[ThresholdTier] = []
    previous = 0.0
    for limit_key, duration_key, tier_id, reason in keys:
        if limit_key not in data:
            continue
        limit = _finite_number(data.get(limit_key), minimum=previous + 1e-9, maximum=MAX_POLICY_POWER_W)
        duration = _finite_number(data.get(duration_key, 0.0), minimum=0.0, maximum=MAX_POLICY_DURATION_S)
        if limit is None or duration is None:
            return None
        try:
            limit, duration = validate_threshold_pair(limit, duration, previous)
        except ValueError:
            return None
        parsed.append(ThresholdTier(tier_id, limit, duration, reason))
        previous = limit
    return tuple(parsed) if parsed else None


def _finite_number(
    value: Any,
    *,
    minimum: float,
    maximum: float,
) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(converted) or converted < minimum or converted > maximum:
        return None
    return converted


def strip_legacy_policy_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Drop deleted policy/restore fields from a config payload."""
    removed = {
        CONF_MAX_LOAD,
        CONF_SAFETY_RESERVE,
        CONF_HYSTERESIS,
        CONF_HARD_INTERLOCK,
        CONF_SHED_SUSTAINED_LIMIT,
        CONF_SHED_SUSTAINED_DURATION,
        CONF_SHED_FAST_LIMIT,
        CONF_SHED_FAST_DURATION,
        CONF_SHED_CRITICAL_LIMIT,
        CONF_SHED_CRITICAL_DURATION,
        "restore_enabled",
        "restore_threshold",
        "restore_hysteresis",
        "restore_dwell",
        "restore_cooldown",
        "restore_armed",
    }
    return {key: value for key, value in data.items() if key not in removed}


@dataclass
class PolicyRuntime:
    """Mutable bounded state reduced by :class:`PolicyEngine`."""

    phase: PolicyPhase = PolicyPhase.STARTUP
    active_tier: str | None = None
    tier_started_at: float | None = None
    tier_since: dict[str, float] = field(default_factory=dict)
    pending_post_shed_generation: int | None = None
    pending_post_shed_after_reported_at: float | None = None
    pending_operation_id: str | None = None
    last_shed_load_generation: int | None = None
    last_telemetry_validity: TelemetryValidity = TelemetryValidity.UNKNOWN
    last_reason_code: ReasonCode = ReasonCode.SAFETY_BLOCKED
    decision_sequence: int = 0
    # Monotonic restore-window start; never persisted across process restart.
    restore_since: float | None = None
    pending_post_restore_generation: int | None = None
    pending_post_restore_after_reported_at: float | None = None
    pending_restore_operation_id: str | None = None
    last_restore_load_generation: int | None = None


@dataclass(frozen=True)
class PolicyDecision:
    """Result of one policy update."""

    triggered: bool
    active_tier: str | None
    reason_code: ReasonCode
    elapsed_s: float = 0.0


class PolicyEngine:
    """Pure timer/phase engine for overload-driven shedding and safe restore."""

    def __init__(self, policy: PolicyConfig, runtime: PolicyRuntime | None = None) -> None:
        self.policy = policy
        self.runtime = runtime or PolicyRuntime()
        self.last_decision = PolicyDecision(False, None, ReasonCode.SAFETY_BLOCKED)

    def reset_restore_window(self) -> None:
        """Clear the in-process safe-capacity restore timer."""
        self.runtime.restore_since = None

    def observe_load(self, load_w: float, *, now: float) -> PolicyDecision:
        """Advance overload dwell timers from one newly reported aggregate value."""
        if isinstance(load_w, bool) or not math.isfinite(load_w) or load_w < 0:
            return self.observe_invalid_load(ReasonCode.TELEMETRY_INVALID, now=now)

        self.runtime.last_telemetry_validity = TelemetryValidity.VALID
        exceeded: list[ThresholdTier] = []
        for tier in self.policy.thresholds:
            if load_w > tier.limit_w:
                exceeded.append(tier)
                self.runtime.tier_since.setdefault(tier.tier_id, now)
            else:
                self.runtime.tier_since.pop(tier.tier_id, None)

        active = exceeded[-1] if exceeded else None
        matured = [
            tier
            for tier in exceeded
            if now - self.runtime.tier_since[tier.tier_id] >= tier.duration_s
        ]
        trigger = matured[-1] if matured else None

        self.runtime.active_tier = active.tier_id if active else None
        self.runtime.tier_started_at = (
            self.runtime.tier_since.get(active.tier_id) if active else None
        )
        if active is None:
            self.runtime.tier_since.clear()

        if trigger is not None:
            self.reset_restore_window()
            self.runtime.phase = PolicyPhase.SHEDDING
            self.runtime.last_reason_code = trigger.reason_code
            elapsed = now - self.runtime.tier_since.get(trigger.tier_id, now)
            decision = PolicyDecision(
                True,
                active.tier_id if active else trigger.tier_id,
                trigger.reason_code,
                max(0.0, elapsed),
            )
        else:
            self.runtime.phase = (
                PolicyPhase.WAITING_LOAD_RECONCILIATION
                if self.runtime.pending_post_shed_generation is not None
                else PolicyPhase.MONITORING
            )
            self.runtime.last_reason_code = ReasonCode.NORMAL_MONITORING
            decision = PolicyDecision(
                False, active.tier_id if active else None, ReasonCode.NORMAL_MONITORING
            )

        self.runtime.decision_sequence += 1
        self.last_decision = decision
        return decision

    def observe_invalid_load(self, reason_code: ReasonCode, *, now: float) -> PolicyDecision:
        """Fail closed on invalid input and reset dwell timers."""
        del now
        self.runtime.last_telemetry_validity = (
            TelemetryValidity.STALE
            if reason_code is ReasonCode.TELEMETRY_STALE
            else TelemetryValidity.INVALID
        )
        self.runtime.active_tier = None
        self.runtime.tier_started_at = None
        self.runtime.tier_since.clear()
        self.reset_restore_window()
        self.runtime.phase = PolicyPhase.FAULT
        self.runtime.last_reason_code = reason_code
        self.runtime.decision_sequence += 1
        self.last_decision = PolicyDecision(False, None, reason_code)
        return self.last_decision

    def append_shed(
        self,
        *,
        operation_id: str,
        load_generation: int,
        reason_code: ReasonCode,
    ) -> None:
        """Install a one-report barrier after a confirmed physical shed."""
        self.reset_restore_window()
        self.runtime.last_shed_load_generation = load_generation
        self.runtime.pending_post_shed_generation = load_generation
        self.runtime.pending_post_shed_after_reported_at = None
        self.runtime.pending_operation_id = operation_id
        self.runtime.phase = PolicyPhase.WAITING_LOAD_RECONCILIATION
        self.runtime.last_reason_code = reason_code

    def set_post_shed_fence(self, reported_at: float | None) -> None:
        """Record the causal relay report required before the next decision."""
        if reported_at is None or not math.isfinite(reported_at):
            self.runtime.pending_post_shed_after_reported_at = None
        else:
            self.runtime.pending_post_shed_after_reported_at = reported_at

    def can_shed_again(self, load_generation: int) -> bool:
        """Require an aggregate report newer than the last confirmed shed."""
        pending = self.runtime.pending_post_shed_generation
        return pending is None or load_generation > pending

    def reconcile_shed(self, load_generation: int, *, reported_at: float | None) -> bool:
        """Release the barrier only after both causal state reports are confirmed."""
        pending = self.runtime.pending_post_shed_generation
        fence = self.runtime.pending_post_shed_after_reported_at
        if pending is None:
            return True
        if load_generation <= pending or fence is None or reported_at is None:
            return False
        if not math.isfinite(reported_at) or reported_at <= fence:
            return False
        self.runtime.pending_post_shed_generation = None
        self.runtime.pending_post_shed_after_reported_at = None
        self.runtime.pending_operation_id = None
        self.runtime.phase = PolicyPhase.MONITORING
        return True

    def observe_restore_safe_capacity(
        self,
        load_w: float,
        *,
        candidate_expected_w: float,
        lowest_limit_w: float,
        now: float,
    ) -> PolicyDecision:
        """Advance the 60s safe-capacity window for the next restore candidate.

        Capacity is safe only while ``load_w + candidate_expected_w < lowest_limit_w``.
        Invalid load or insufficient capacity resets the monotonic window.
        """
        if isinstance(load_w, bool) or not math.isfinite(load_w) or load_w < 0:
            self.reset_restore_window()
            return PolicyDecision(False, None, ReasonCode.TELEMETRY_INVALID)
        if (
            isinstance(candidate_expected_w, bool)
            or not math.isfinite(candidate_expected_w)
            or candidate_expected_w < 0
            or isinstance(lowest_limit_w, bool)
            or not math.isfinite(lowest_limit_w)
            or lowest_limit_w <= 0
        ):
            self.reset_restore_window()
            return PolicyDecision(False, None, ReasonCode.RESTORE_BLOCKED_OVERLOAD)
        if load_w + candidate_expected_w >= lowest_limit_w:
            self.reset_restore_window()
            return PolicyDecision(False, None, ReasonCode.RESTORE_BLOCKED_OVERLOAD)

        if self.runtime.restore_since is None:
            self.runtime.restore_since = now
        elapsed = now - self.runtime.restore_since
        triggered = elapsed >= _const.RESTORE_SAFE_CAPACITY_DWELL_S
        return PolicyDecision(
            triggered,
            "restore" if triggered else None,
            ReasonCode.RESTORE_HEADROOM_AVAILABLE,
            max(0.0, elapsed),
        )

    def append_restore(
        self,
        *,
        operation_id: str,
        load_generation: int,
    ) -> None:
        """Install a one-report barrier after a confirmed physical restore."""
        self.runtime.last_restore_load_generation = load_generation
        self.runtime.pending_post_restore_generation = load_generation
        self.runtime.pending_post_restore_after_reported_at = None
        self.runtime.pending_restore_operation_id = operation_id
        self.reset_restore_window()
        self.runtime.phase = PolicyPhase.WAITING_RESTORE_RECONCILIATION

    def set_post_restore_fence(self, reported_at: float | None) -> None:
        """Record the causal relay report required before the next restore."""
        if reported_at is None or not math.isfinite(reported_at):
            self.runtime.pending_post_restore_after_reported_at = None
        else:
            self.runtime.pending_post_restore_after_reported_at = reported_at

    def can_restore_again(self, load_generation: int) -> bool:
        """Require an aggregate report newer than the last confirmed restore."""
        pending = self.runtime.pending_post_restore_generation
        return pending is None or load_generation > pending

    def reconcile_restore(self, load_generation: int, *, reported_at: float | None) -> bool:
        """Release the restore barrier only after a causal newer aggregate report."""
        pending = self.runtime.pending_post_restore_generation
        fence = self.runtime.pending_post_restore_after_reported_at
        if pending is None:
            return True
        if load_generation <= pending or fence is None or reported_at is None:
            return False
        if not math.isfinite(reported_at) or reported_at <= fence:
            return False
        self.runtime.pending_post_restore_generation = None
        self.runtime.pending_post_restore_after_reported_at = None
        self.runtime.pending_restore_operation_id = None
        self.runtime.phase = PolicyPhase.MONITORING
        return True


def validate_threshold_pair(
    limit_w: float,
    duration_s: float,
    previous_limit: float = 0.0,
) -> tuple[float, float]:
    """Validate one finite, increasing threshold pair."""
    if (
        not math.isfinite(limit_w)
        or not math.isfinite(duration_s)
        or limit_w <= previous_limit
        or limit_w > MAX_POLICY_POWER_W
        or duration_s < 0
        or duration_s > MAX_POLICY_DURATION_S
    ):
        raise ValueError("invalid threshold pair")
    return limit_w, duration_s


def policy_for_tests(
    *pairs: tuple[float, float],
    policy_version: str = DEFAULT_POLICY_VERSION,
) -> PolicyConfig:
    """Build an explicit non-empty policy for unit tests."""
    tiers = tuple(
        ThresholdTier(
            f"custom_{index}",
            limit,
            duration,
            ReasonCode.SHED_CUSTOM_THRESHOLD,
        )
        for index, (limit, duration) in enumerate(pairs, start=1)
    )
    return PolicyConfig(thresholds=tiers, policy_version=policy_version)
