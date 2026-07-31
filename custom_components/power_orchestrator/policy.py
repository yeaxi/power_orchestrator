"""Pure load-shedding policy, ownership, and diagnostic state types.

This module intentionally has no Home Assistant dependency.  It is the
referentially-transparent policy boundary used by the coordinator and by
offline/replay tests.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class PolicyPhase(str, Enum):
    """High-level controller phase, independent from operating mode."""

    STARTUP = "startup"
    MONITORING = "monitoring"
    SHEDDING = "shedding"
    WAITING_LOAD_RECONCILIATION = "waiting_load_reconciliation"
    RECOVERY_WAIT = "recovery_wait"
    RECOVERY = "recovery"
    GRID_LOSS = "grid_loss"
    FAULT = "fault"


class ReasonCode(str, Enum):
    """Stable machine-readable decision reasons."""

    NORMAL_MONITORING = "normal_monitoring"
    SHED_SUSTAINED_OVERLOAD = "shed_sustained_overload"
    SHED_FAST_OVERLOAD = "shed_fast_overload"
    SHED_CRITICAL_OVERLOAD = "shed_critical_overload"
    SHED_CUSTOM_THRESHOLD = "shed_custom_threshold"
    HARD_INTERLOCK = "hard_interlock"
    RECOVERY_WAITING_FOR_STABLE_LOAD = "recovery_waiting_for_stable_load"
    RECOVERY_READY = "recovery_ready"
    RECOVERY_BLOCKED = "recovery_blocked"
    TELEMETRY_INVALID = "telemetry_invalid"
    TELEMETRY_STALE = "telemetry_stale"
    MANUAL_START_BLOCKED = "manual_start_blocked"
    EXTERNAL_OWNERSHIP = "external_ownership"
    RELAY_READBACK_TIMEOUT = "relay_readback_timeout"
    DELAYED_ACTIVATION = "delayed_activation"
    PERSISTED_RUNTIME_INVALID = "persisted_runtime_invalid"
    AGGREGATE_RECONCILIATION_TIMEOUT = "aggregate_reconciliation_timeout"
    GRID_LOSS = "grid_loss"
    STARTUP_RECONCILIATION = "startup_reconciliation"
    OBSERVE_MODE = "observe_mode"
    CONFIGURATION_INVALID = "configuration_invalid"
    FAULT = "fault"


class Ownership(str, Enum):
    """Who currently owns a logical load's observed state."""

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


MAX_POLICY_POWER_W = 9000.0
MAX_POLICY_DURATION_S = 86400.0


def validate_threshold_pair(
    power_limit: float,
    duration_s: float,
    previous_power: float | None = None,
) -> tuple[float, float]:
    """Validate one threshold using the canonical flow/runtime bounds."""
    if (
        not math.isfinite(power_limit)
        or not math.isfinite(duration_s)
        or not 1.0 <= power_limit <= MAX_POLICY_POWER_W
        or not 1.0 <= duration_s <= MAX_POLICY_DURATION_S
        or (previous_power is not None and power_limit <= previous_power)
    ):
        raise ValueError("threshold pair is outside canonical safety bounds")
    return power_limit, duration_s


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
        if self.limit_w < 0 or self.duration_s < 0:
            raise ValueError("threshold values must be non-negative")


@dataclass(frozen=True)
class PolicyConfig:
    """Versioned load-shedding and recovery policy."""

    policy_version: str = "load_shedding_v1"
    recovery_target_w: float = 6000.0
    recovery_start_w: float = 5000.0
    recovery_low_duration_s: float = 60.0
    recovery_stabilization_s: float = 60.0
    safety_reserve_w: float = 500.0
    hard_interlock_w: Optional[float] = None
    thresholds: tuple[ThresholdTier, ...] = (
        ThresholdTier("sustained", 6500.0, 300.0, ReasonCode.SHED_SUSTAINED_OVERLOAD),
        ThresholdTier("fast", 7000.0, 30.0, ReasonCode.SHED_FAST_OVERLOAD),
        ThresholdTier("critical", 8000.0, 5.0, ReasonCode.SHED_CRITICAL_OVERLOAD),
    )

    def __post_init__(self) -> None:
        if self.recovery_start_w > self.recovery_target_w:
            raise ValueError("recovery_start_w must not exceed recovery_target_w")
        if self.recovery_low_duration_s < 0 or self.recovery_stabilization_s < 0:
            raise ValueError("recovery durations must be non-negative")
        if self.safety_reserve_w < 0:
            raise ValueError("safety reserve must be non-negative")
        previous = -1.0
        for tier in self.thresholds:
            if tier.limit_w <= previous:
                raise ValueError("threshold limits must be strictly increasing")
            previous = tier.limit_w
        if self.hard_interlock_w is not None:
            if self.hard_interlock_w <= 0:
                raise ValueError("hard interlock must be positive")
            if self.thresholds and self.hard_interlock_w < self.thresholds[-1].limit_w:
                raise ValueError("hard interlock must not be below critical threshold")


    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "PolicyConfig":
        """Build a safe policy from config/options with canonical fallbacks.

        ``thresholds`` is the preferred representation and is deliberately
        capped at ten entries.  The three legacy scalar pairs remain accepted
        for migrated entries and produce the canonical three-tier policy.
        """

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
        if raw_thresholds is not None:
            if not isinstance(raw_thresholds, (list, tuple)) or not 1 <= len(raw_thresholds) <= 10:
                return DEFAULT_POLICY
            custom_thresholds: list[ThresholdTier] = []
            previous_limit = 0.0
            for index, raw_threshold in enumerate(raw_thresholds, start=1):
                if not isinstance(raw_threshold, Mapping):
                    return DEFAULT_POLICY
                limit_raw = raw_threshold.get(
                    "power_limit", raw_threshold.get("limit_w")
                )
                duration_raw = raw_threshold.get(
                    "duration_s", raw_threshold.get("time_s")
                )
                if isinstance(limit_raw, bool) or isinstance(duration_raw, bool):
                    return DEFAULT_POLICY
                try:
                    limit_w = float(limit_raw)
                    duration_s = float(duration_raw)
                    limit_w, duration_s = validate_threshold_pair(
                        limit_w,
                        duration_s,
                        previous_limit,
                    )
                except (TypeError, ValueError):
                    return DEFAULT_POLICY
                custom_thresholds.append(
                    ThresholdTier(
                        f"custom_{index}",
                        limit_w,
                        duration_s,
                        ReasonCode.SHED_CUSTOM_THRESHOLD,
                    )
                )
                previous_limit = limit_w
            thresholds = tuple(custom_thresholds)
        else:
            thresholds = (
                ThresholdTier(
                    "sustained",
                    number("shed_sustained_limit", 6500.0, maximum=MAX_POLICY_POWER_W),
                    number("shed_sustained_duration", 300.0, maximum=MAX_POLICY_DURATION_S),
                    ReasonCode.SHED_SUSTAINED_OVERLOAD,
                ),
                ThresholdTier(
                    "fast",
                    number("shed_fast_limit", 7000.0, maximum=MAX_POLICY_POWER_W),
                    number("shed_fast_duration", 30.0, maximum=MAX_POLICY_DURATION_S),
                    ReasonCode.SHED_FAST_OVERLOAD,
                ),
                ThresholdTier(
                    "critical",
                    number("shed_critical_limit", 8000.0, maximum=MAX_POLICY_POWER_W),
                    number("shed_critical_duration", 5.0, maximum=MAX_POLICY_DURATION_S),
                    ReasonCode.SHED_CRITICAL_OVERLOAD,
                ),
            )

        policy_version = data.get("policy_version")
        if not isinstance(policy_version, str) or not policy_version:
            policy_version = "load_shedding_v1"
        try:
            return cls(
                policy_version=policy_version,
                recovery_target_w=number("recovery_target", 6000.0, maximum=50000.0),
                recovery_start_w=number("recovery_start", 5000.0, maximum=50000.0),
                recovery_low_duration_s=number(
                    "recovery_low_duration", 60.0, maximum=MAX_POLICY_DURATION_S
                ),
                recovery_stabilization_s=number(
                    "recovery_stabilization", 60.0, maximum=MAX_POLICY_DURATION_S
                ),
                safety_reserve_w=number("safety_reserve", 500.0, maximum=5000.0),
                hard_interlock_w=number(
                    "hard_interlock", 9000.0, 1.0, MAX_POLICY_POWER_W
                ),
                thresholds=thresholds,
            )
        except ValueError:
            return DEFAULT_POLICY


DEFAULT_POLICY = PolicyConfig()


@dataclass(frozen=True)
class TelemetrySample:
    """A normalized source sample; ``value=None`` never authorizes action."""

    entity_id: str
    value: Optional[float]
    unit: Optional[str]
    reported_at: Optional[float]
    generation: int
    validity: TelemetryValidity
    reason: str = ""

    @property
    def is_valid(self) -> bool:
        return self.validity is TelemetryValidity.VALID and self.value is not None


@dataclass(frozen=True)
class ShedStackEntry:
    """Exact record of a device transition performed by this controller."""

    device_id: str
    operation_id: str
    pre_state: bool
    snapshot: dict[str, Any]
    load_generation: int
    reason_code: ReasonCode = ReasonCode.SHED_SUSTAINED_OVERLOAD
    created_at: Optional[float] = None


@dataclass(frozen=True)
class AuthorizationLease:
    """Short-lived permission for one expected actuator transition."""

    device_id: str
    operation_id: str
    allowed_state: str
    expires_at: float
    reported_at: float | None = None

    def allows(
        self,
        device_id: str,
        observed_state: str,
        now: float,
        *,
        reported_at: float | None = None,
    ) -> bool:
        return (
            now <= self.expires_at
            and device_id == self.device_id
            and observed_state == self.allowed_state
            and (
                self.reported_at is None
                or reported_at is not None
                and reported_at == self.reported_at
            )
        )


@dataclass
class PolicyRuntime:
    """Mutable state reduced by :class:`PolicyEngine`."""

    phase: PolicyPhase = PolicyPhase.STARTUP
    active_tier: Optional[str] = None
    tier_started_at: Optional[float] = None
    tier_since: dict[str, float] = field(default_factory=dict)
    recovery_low_since: Optional[float] = None
    stabilize_until: Optional[float] = None
    shed_stack: list[ShedStackEntry] = field(default_factory=list)
    restore_target: Optional[str] = None
    last_shed_load_generation: Optional[int] = None
    pending_post_shed_generation: Optional[int] = None
    pending_post_shed_after_reported_at: Optional[float] = None
    pending_operation_id: Optional[str] = None
    manual_start_blocked_count: int = 0
    last_telemetry_validity: TelemetryValidity = TelemetryValidity.UNKNOWN
    last_reason_code: ReasonCode = ReasonCode.STARTUP_RECONCILIATION
    decision_sequence: int = 0


@dataclass(frozen=True)
class PolicyDecision:
    """Result of a policy update."""

    triggered: bool
    recovery_ready: bool
    active_tier: Optional[str]
    reason_code: ReasonCode
    elapsed_s: float = 0.0


class PolicyEngine:
    """Deterministic timer/phase engine for overload and recovery policy."""

    def __init__(self, policy: PolicyConfig, runtime: Optional[PolicyRuntime] = None) -> None:
        self.policy = policy
        self.runtime = runtime or PolicyRuntime()
        self.last_decision = PolicyDecision(
            False,
            False,
            None,
            ReasonCode.STARTUP_RECONCILIATION,
        )

    def observe_load(self, load_w: float, *, now: float) -> PolicyDecision:
        """Advance overload and recovery timers from one fresh load report."""
        if load_w < 0:
            return self.observe_invalid_load(ReasonCode.TELEMETRY_INVALID, now=now)

        self.runtime.last_telemetry_validity = TelemetryValidity.VALID
        exceeded_tiers: list[ThresholdTier] = []
        for tier in self.policy.thresholds:
            if load_w > tier.limit_w:
                exceeded_tiers.append(tier)
                self.runtime.tier_since.setdefault(tier.tier_id, now)
            else:
                self.runtime.tier_since.pop(tier.tier_id, None)

        active_tier: Optional[ThresholdTier] = (
            exceeded_tiers[-1] if exceeded_tiers else None
        )
        matured_tiers = [
            tier
            for tier in exceeded_tiers
            if now - self.runtime.tier_since[tier.tier_id] >= tier.duration_s
        ]
        triggered_tier: Optional[ThresholdTier] = (
            matured_tiers[-1] if matured_tiers else None
        )

        if self.policy.hard_interlock_w is not None and load_w >= self.policy.hard_interlock_w:
            triggered_tier = ThresholdTier(
                "hard_interlock",
                self.policy.hard_interlock_w,
                0.0,
                ReasonCode.HARD_INTERLOCK,
            )

        self.runtime.active_tier = active_tier.tier_id if active_tier else None
        self.runtime.tier_started_at = (
            self.runtime.tier_since.get(active_tier.tier_id) if active_tier else None
        )
        if active_tier is None:
            self.runtime.tier_since.clear()

        recovery_ready = self._update_recovery(load_w, now)
        if triggered_tier is not None:
            self.runtime.phase = PolicyPhase.SHEDDING
            self.runtime.last_reason_code = triggered_tier.reason_code
            decision = PolicyDecision(
                True,
                recovery_ready,
                active_tier.tier_id if active_tier else triggered_tier.tier_id,
                triggered_tier.reason_code,
                max(0.0, now - self.runtime.tier_since.get(triggered_tier.tier_id, now)),
            )
        elif recovery_ready:
            self.runtime.phase = PolicyPhase.RECOVERY
            self.runtime.last_reason_code = ReasonCode.RECOVERY_READY
            decision = PolicyDecision(
                False,
                True,
                active_tier.tier_id if active_tier else None,
                ReasonCode.RECOVERY_READY,
            )
        elif self.runtime.recovery_low_since is not None:
            self.runtime.phase = PolicyPhase.RECOVERY_WAIT
            self.runtime.last_reason_code = ReasonCode.RECOVERY_WAITING_FOR_STABLE_LOAD
            decision = PolicyDecision(
                False,
                False,
                active_tier.tier_id if active_tier else None,
                ReasonCode.RECOVERY_WAITING_FOR_STABLE_LOAD,
                max(0.0, now - self.runtime.recovery_low_since),
            )
        else:
            self.runtime.phase = PolicyPhase.MONITORING
            self.runtime.last_reason_code = ReasonCode.NORMAL_MONITORING
            decision = PolicyDecision(
                False,
                False,
                active_tier.tier_id if active_tier else None,
                ReasonCode.NORMAL_MONITORING,
            )
        self.runtime.decision_sequence += 1
        self.last_decision = decision
        return decision

    def observe_invalid_load(self, reason_code: ReasonCode, *, now: float) -> PolicyDecision:
        """Fail closed on invalid/stale telemetry without releasing reservations."""
        del now
        self.runtime.last_telemetry_validity = (
            TelemetryValidity.STALE
            if reason_code is ReasonCode.TELEMETRY_STALE
            else TelemetryValidity.INVALID
        )
        # A gap in telemetry must not preserve elapsed overload/recovery time.
        # The next valid sample starts a new timer from a known-good observation.
        self.runtime.active_tier = None
        self.runtime.tier_started_at = None
        self.runtime.tier_since.clear()
        self.runtime.recovery_low_since = None
        self.runtime.stabilize_until = None
        self.runtime.phase = PolicyPhase.FAULT
        self.runtime.last_reason_code = reason_code
        self.runtime.decision_sequence += 1
        self.last_decision = PolicyDecision(False, False, None, reason_code)
        return self.last_decision

    def _update_recovery(self, load_w: float, now: float) -> bool:
        if load_w >= self.policy.recovery_start_w:
            self.runtime.recovery_low_since = None
            self.runtime.stabilize_until = None
            return False
        if self.runtime.recovery_low_since is None:
            self.runtime.recovery_low_since = now
            self.runtime.stabilize_until = now + self.policy.recovery_low_duration_s + self.policy.recovery_stabilization_s
            return False
        return (
            self.runtime.stabilize_until is not None
            and now >= self.runtime.stabilize_until
        )

    def append_shed(self, entry: ShedStackEntry) -> None:
        """Record only a confirmed controller-owned shed transition."""
        self.runtime.shed_stack.append(entry)
        self.runtime.last_shed_load_generation = entry.load_generation
        self.runtime.pending_post_shed_generation = entry.load_generation
        self.runtime.pending_post_shed_after_reported_at = None
        self.runtime.pending_operation_id = entry.operation_id
        self.runtime.phase = PolicyPhase.WAITING_LOAD_RECONCILIATION
        self.runtime.last_reason_code = entry.reason_code

    def set_post_shed_fence(self, reported_at: float | None) -> None:
        """Record the relay OFF report that a later aggregate report must follow."""
        if reported_at is None or not math.isfinite(reported_at):
            self.runtime.pending_post_shed_after_reported_at = None
            return
        self.runtime.pending_post_shed_after_reported_at = reported_at

    def next_restore_target(self) -> Optional[ShedStackEntry]:
        """Return the latest controller-owned shed that is eligible to restore."""
        return self.runtime.shed_stack[-1] if self.runtime.shed_stack else None

    def pop_restore_target(self) -> Optional[ShedStackEntry]:
        """Pop exactly one controller-owned restore target."""
        entry = self.next_restore_target()
        if entry is None:
            self.runtime.restore_target = None
            return None
        self.runtime.shed_stack.pop()
        self.runtime.restore_target = entry.device_id
        return entry

    def can_shed_again(self, load_generation: int) -> bool:
        """Require a newer aggregate report after every normal shed."""
        pending = self.runtime.pending_post_shed_generation
        return pending is None or load_generation > pending

    def reconcile_shed(
        self,
        load_generation: int,
        *,
        reported_at: float | None = None,
    ) -> bool:
        """Release the barrier only after causal relay and aggregate reports."""
        if self.runtime.pending_post_shed_generation is None:
            return True
        if load_generation <= self.runtime.pending_post_shed_generation:
            return False
        fence = self.runtime.pending_post_shed_after_reported_at
        if fence is None or reported_at is None or not math.isfinite(reported_at):
            return False
        if reported_at <= fence:
            return False
        self.runtime.pending_post_shed_generation = None
        self.runtime.pending_post_shed_after_reported_at = None
        self.runtime.pending_operation_id = None
        self.runtime.phase = PolicyPhase.MONITORING
        return True
