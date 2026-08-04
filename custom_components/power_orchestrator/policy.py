"""Deterministic policy engine for bounded load shedding."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from .const import (
    DEFAULT_HARD_INTERLOCK,
    DEFAULT_POLICY_VERSION,
    DEFAULT_SAFETY_RESERVE,
    DEFAULT_SHED_CRITICAL_DURATION,
    DEFAULT_SHED_CRITICAL_LIMIT,
    DEFAULT_SHED_FAST_DURATION,
    DEFAULT_SHED_FAST_LIMIT,
    DEFAULT_SHED_SUSTAINED_DURATION,
    DEFAULT_SHED_SUSTAINED_LIMIT,
    MAX_CUSTOM_THRESHOLDS,
)

MAX_POLICY_POWER_W = 100_000.0
MAX_POLICY_DURATION_S = 24 * 60 * 60.0


class PolicyPhase(str, Enum):
    """High-level load-shedding phase."""

    STARTUP = "startup"
    MONITORING = "monitoring"
    SHEDDING = "shedding"
    WAITING_LOAD_RECONCILIATION = "waiting_load_reconciliation"
    GRID_LOSS = "grid_loss"
    FAULT = "fault"


class ReasonCode(str, Enum):
    """Stable machine-readable decision and safety reasons."""

    NORMAL_MONITORING = "normal_monitoring"
    SHED_SUSTAINED_OVERLOAD = "shed_sustained_overload"
    SHED_FAST_OVERLOAD = "shed_fast_overload"
    SHED_CRITICAL_OVERLOAD = "shed_critical_overload"
    SHED_CUSTOM_THRESHOLD = "shed_custom_threshold"
    HARD_INTERLOCK = "hard_interlock"
    TELEMETRY_INVALID = "telemetry_invalid"
    TELEMETRY_STALE = "telemetry_stale"
    EXTERNAL_OWNERSHIP = "external_ownership"
    RELAY_READBACK_TIMEOUT = "relay_readback_timeout"
    PERSISTED_RUNTIME_INVALID = "persisted_runtime_invalid"
    AGGREGATE_RECONCILIATION_TIMEOUT = "aggregate_reconciliation_timeout"
    GRID_LOSS = "grid_loss"
    SAFETY_BLOCKED = "safety_blocked"
    OBSERVE_MODE = "observe_mode"
    CONFIGURATION_INVALID = "configuration_invalid"
    FAULT = "fault"


class Ownership(str, Enum):
    """Who owns the currently observed state of a logical load."""

    UNKNOWN = "unknown"
    PLANNER = "planner"
    MANUAL = "manual"
    EXTERNAL = "external"


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
            or self.limit_w < 0
            or self.duration_s < 0
        ):
            raise ValueError("threshold values must be finite and non-negative")


@dataclass(frozen=True)
class PolicyConfig:
    """Versioned load-shedding policy with no activation branch."""

    policy_version: str = DEFAULT_POLICY_VERSION
    safety_reserve_w: float = DEFAULT_SAFETY_RESERVE
    hard_interlock_w: float | None = DEFAULT_HARD_INTERLOCK
    thresholds: tuple[ThresholdTier, ...] = (
        ThresholdTier(
            "sustained",
            DEFAULT_SHED_SUSTAINED_LIMIT,
            DEFAULT_SHED_SUSTAINED_DURATION,
            ReasonCode.SHED_SUSTAINED_OVERLOAD,
        ),
        ThresholdTier(
            "fast",
            DEFAULT_SHED_FAST_LIMIT,
            DEFAULT_SHED_FAST_DURATION,
            ReasonCode.SHED_FAST_OVERLOAD,
        ),
        ThresholdTier(
            "critical",
            DEFAULT_SHED_CRITICAL_LIMIT,
            DEFAULT_SHED_CRITICAL_DURATION,
            ReasonCode.SHED_CRITICAL_OVERLOAD,
        ),
    )

    def __post_init__(self) -> None:
        if not math.isfinite(self.safety_reserve_w) or self.safety_reserve_w < 0:
            raise ValueError("safety reserve must be finite and non-negative")
        previous = -1.0
        for tier in self.thresholds:
            if tier.limit_w <= previous:
                raise ValueError("threshold limits must be strictly increasing")
            previous = tier.limit_w
        if self.hard_interlock_w is not None:
            if not math.isfinite(self.hard_interlock_w) or self.hard_interlock_w <= 0:
                raise ValueError("hard interlock must be positive")
            if self.thresholds and self.hard_interlock_w < self.thresholds[-1].limit_w:
                raise ValueError("hard interlock must not be below the highest threshold")

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "PolicyConfig":
        """Build a bounded policy from config/options with safe fallbacks."""

        def number(
            key: str,
            default: float,
            minimum: float = 0.0,
            maximum: float | None = None,
        ) -> float:
            value = data.get(key, default)
            if isinstance(value, bool):
                return default
            try:
                converted = float(value)
            except (TypeError, ValueError):
                return default
            if (
                not math.isfinite(converted)
                or converted < minimum
                or (maximum is not None and converted > maximum)
            ):
                return default
            return converted

        raw_thresholds = data.get("thresholds")
        thresholds: tuple[ThresholdTier, ...]
        if raw_thresholds is None:
            thresholds = (
                ThresholdTier(
                    "sustained",
                    number("shed_sustained_limit", DEFAULT_SHED_SUSTAINED_LIMIT, maximum=MAX_POLICY_POWER_W),
                    number("shed_sustained_duration", DEFAULT_SHED_SUSTAINED_DURATION, maximum=MAX_POLICY_DURATION_S),
                    ReasonCode.SHED_SUSTAINED_OVERLOAD,
                ),
                ThresholdTier(
                    "fast",
                    number("shed_fast_limit", DEFAULT_SHED_FAST_LIMIT, maximum=MAX_POLICY_POWER_W),
                    number("shed_fast_duration", DEFAULT_SHED_FAST_DURATION, maximum=MAX_POLICY_DURATION_S),
                    ReasonCode.SHED_FAST_OVERLOAD,
                ),
                ThresholdTier(
                    "critical",
                    number("shed_critical_limit", DEFAULT_SHED_CRITICAL_LIMIT, maximum=MAX_POLICY_POWER_W),
                    number("shed_critical_duration", DEFAULT_SHED_CRITICAL_DURATION, maximum=MAX_POLICY_DURATION_S),
                    ReasonCode.SHED_CRITICAL_OVERLOAD,
                ),
            )
        elif isinstance(raw_thresholds, (list, tuple)) and 1 <= len(raw_thresholds) <= MAX_CUSTOM_THRESHOLDS:
            parsed: list[ThresholdTier] = []
            previous = 0.0
            try:
                for index, raw in enumerate(raw_thresholds, start=1):
                    if not isinstance(raw, Mapping):
                        raise ValueError
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
                return DEFAULT_POLICY
            thresholds = tuple(parsed)
        else:
            return DEFAULT_POLICY

        hard_interlock = number(
            "hard_interlock",
            DEFAULT_HARD_INTERLOCK,
            minimum=1.0,
            maximum=MAX_POLICY_POWER_W,
        )
        version = data.get("policy_version")
        if not isinstance(version, str) or not version.strip():
            version = DEFAULT_POLICY_VERSION
        try:
            return cls(
                policy_version=version.strip(),
                safety_reserve_w=number("safety_reserve", DEFAULT_SAFETY_RESERVE, maximum=5000.0),
                hard_interlock_w=hard_interlock,
                thresholds=thresholds,
            )
        except ValueError:
            return DEFAULT_POLICY


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


@dataclass(frozen=True)
class PolicyDecision:
    """Result of one policy update."""

    triggered: bool
    active_tier: str | None
    reason_code: ReasonCode
    elapsed_s: float = 0.0


class PolicyEngine:
    """Pure timer/phase engine for overload-driven shedding."""

    def __init__(self, policy: PolicyConfig, runtime: PolicyRuntime | None = None) -> None:
        self.policy = policy
        self.runtime = runtime or PolicyRuntime()
        self.last_decision = PolicyDecision(False, None, ReasonCode.SAFETY_BLOCKED)

    def observe_load(self, load_w: float, *, now: float) -> PolicyDecision:
        """Advance overload dwell timers from one fresh aggregate report."""
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
        if self.policy.hard_interlock_w is not None and load_w >= self.policy.hard_interlock_w:
            trigger = ThresholdTier(
                "hard_interlock",
                self.policy.hard_interlock_w,
                0.0,
                ReasonCode.HARD_INTERLOCK,
            )

        self.runtime.active_tier = active.tier_id if active else None
        self.runtime.tier_started_at = (
            self.runtime.tier_since.get(active.tier_id) if active else None
        )
        if active is None:
            self.runtime.tier_since.clear()

        if trigger is not None:
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
            decision = PolicyDecision(False, active.tier_id if active else None, ReasonCode.NORMAL_MONITORING)

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
        """Release the barrier only after both causal reports are fresh."""
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


DEFAULT_POLICY = PolicyConfig()
