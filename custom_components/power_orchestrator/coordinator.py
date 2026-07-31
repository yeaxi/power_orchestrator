"""Coordinator — periodic evaluation and load management."""

from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DOMAIN,
    EVALUATION_INTERVAL,
    EVENT_ACTION,
    EVENT_DECISION,
    EVENT_SCHEMA_VERSION,
    EXECUTION_MODE_LIVE,
    EXECUTION_MODE_OBSERVE,
    EXTERNAL_OWNERSHIP_GRACE_SECONDS,
    GRID_LOSS_MODE_SENSOR,
    GRID_LOSS_MODE_THRESHOLD,
    MODE_AUTO,
    MODE_OFF,
    QUARANTINE_CLEAR_MAX_POWER_W,
    RELAY_READBACK_POLL_INTERVAL_SECONDS,
    RELAY_READBACK_TIMEOUT_SECONDS,
    SAFETY_INPUT_MAX_AGE_SECONDS,
    STATUS_ADDING_LOAD,
    STATUS_GRID_LOSS,
    STATUS_LOAD_SHEDDING,
    STATUS_MONITORING,
    STATUS_OBSERVE,
    STATUS_RECOVERY_WAIT,
    STATUS_SAFETY_BLOCKED,
)
from .forecast import current_power_forecast_w
from .policy import (
    AuthorizationLease,
    Ownership,
    PolicyConfig,
    PolicyDecision,
    PolicyEngine,
    PolicyPhase,
    ReasonCode,
    ShedStackEntry,
    ThresholdTier,
)
from .power_model import ManagedDevice, PowerModel
from .storage import RuntimeStore

_LOGGER = logging.getLogger(__name__)


@dataclass
class _PendingStart:
    """Reservation and reconciliation state for one physical start."""

    operation_id: int
    device_id: str
    expected_w: float
    admission_reported_at: float | None
    admission_generation: int
    admission_load_w: float | None
    phase: str = "reserved"
    on_confirmed_reported_at: float | None = None
    rollback_off_reported_at: float | None = None
    telemetry_deadline_monotonic: float = 0.0


class PowerOrchestratorCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Orchestrator coordinator — evaluates and manages loads."""

    def __init__(
        self,
        hass: HomeAssistant,
        model: PowerModel,
        store: RuntimeStore,
        load_sensor: str,
        max_load: float,
        averaging_period: float,
        safety_reserve: float,
        hysteresis: float,
        pause_period: float,
        grid_loss_mode: str,
        grid_loss_sensor: str | None,
        battery_threshold: float | None,
        battery_soc_sensor: str | None,
        solar_forecast_entity: str | None,
        solar_production_entity: str | None,
        entry_id: str = DOMAIN,
        policy: PolicyConfig | None = None,
        execution_mode: str = EXECUTION_MODE_LIVE,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=EVALUATION_INTERVAL),
        )
        self._model = model
        self._store = store
        self._load_sensor = load_sensor
        self._max_load = max_load
        self._averaging_period = averaging_period
        self._safety_reserve = safety_reserve
        self._hysteresis = hysteresis
        self._pause_period = pause_period
        self._grid_loss_mode = grid_loss_mode
        self._grid_loss_sensor = grid_loss_sensor
        self._battery_threshold = battery_threshold
        self._battery_soc_sensor = battery_soc_sensor
        self._solar_forecast_entity = solar_forecast_entity
        self._solar_production_entity = solar_production_entity
        self._entry_id = entry_id
        self._execution_mode = (
            execution_mode
            if execution_mode in (EXECUTION_MODE_LIVE, EXECUTION_MODE_OBSERVE)
            else EXECUTION_MODE_OBSERVE
        )
        self._policy_enabled = policy is not None
        self._policy = policy or PolicyConfig(
            policy_version="legacy",
            recovery_target_w=float(max_load),
            recovery_start_w=float(max_load),
            recovery_low_duration_s=0.0,
            recovery_stabilization_s=0.0,
            safety_reserve_w=float(safety_reserve),
            thresholds=(
                ThresholdTier(
                    "legacy",
                    float(max_load),
                    0.0,
                    ReasonCode.SHED_SUSTAINED_OVERLOAD,
                ),
            ),
        )
        self._policy_engine = PolicyEngine(self._policy)
        self._last_policy_decision = self._policy_engine.last_decision
        self._authorization_leases: dict[str, AuthorizationLease] = {}
        self._decision_events: deque[dict[str, Any]] = deque(maxlen=100)
        self._last_operation_result = "none"
        self._last_operation_id: str | None = None
        self._last_reconciliation_generation: int | None = None
        self._shed_snapshots: dict[str, dict[str, Any]] = {}
        self._safety_fault_reason: str | None = None
        self._last_observed_state: dict[str, bool | None] = {}
        self._initial_device_reconciliation_complete = False
        self._pending_restore_entry: ShedStackEntry | None = None
        self._external_ownership_grace = EXTERNAL_OWNERSHIP_GRACE_SECONDS
        self._safety_input_max_age = SAFETY_INPUT_MAX_AGE_SECONDS
        self._relay_readback_timeout = RELAY_READBACK_TIMEOUT_SECONDS
        self._relay_readback_poll_interval = RELAY_READBACK_POLL_INTERVAL_SECONDS
        # A valid relay ON must be followed by aggregate telemetry within a
        # bounded window; otherwise fail closed and recover with OFF.
        self._pending_start_timeout = max(
            2 * self._safety_input_max_age,
            2 * EVALUATION_INTERVAL,
        )
        self._evaluation_lock = asyncio.Lock()

        # Runtime state. Automatic starts stay disarmed until an explicit
        # post-startup set_mode(auto) command arms this coordinator.
        self._mode = MODE_OFF
        self._startup_safe = True
        self._post_arm_reconciliation_required = False
        self._arm_issued_at: float | None = None
        self._arm_load_generation: int | None = None
        self._execution_mode_reconciliation_required = False
        self._execution_mode_transition_issued_at: float | None = None
        self._execution_mode_transition_generation: int | None = None
        self._status = STATUS_MONITORING
        self._load_samples: deque[float] = deque(maxlen=100)
        self._load_sample_times: deque[float] = deque(maxlen=100)
        self._last_action = "Initialized"
        self._load_sensor_valid = False
        self._load_sensor_reason = "not_sampled"
        self._load_reported_at: float | None = None
        self._last_accepted_load_reported_at: float | None = None
        self._load_generation = 0
        self._last_admission_generation: int | None = None
        # Keep the scalar projection for entity/backward-compatible callers;
        # lifecycle decisions use the structured reservation below.
        self._pending_start_w = 0.0
        self._pending_start: _PendingStart | None = None
        self._operation_generation = 0
        self._active_operations: dict[str, int] = {}
        self._last_confirmed_reported_at: dict[str, float | None] = {}
        self._recovery_blocked: set[str] = set()
        self._faulted: set[str] = set()
        self._fault_reasons: dict[str, str] = {}
        self._fault_state_dirty = False
        self._journal_dirty = False
        self._fault_notifications_sent: set[str] = set()
        self._fault_notifications_pending_dismissal: set[str] = set()
        self._safety_storage_invalid = False
        self._grid_loss_expected_off: set[str] = set()
        self._manual_override_notified: set[str] = set()

    # ── Properties ─────────────────────────────────────────────────

    @property
    def safety_storage_invalid(self) -> bool:
        """Return whether persisted safety runtime is malformed or future-versioned."""
        return self._safety_storage_invalid

    @property
    def execution_mode(self) -> str:
        """Return physical command policy: live or observe."""
        return self._execution_mode

    @property
    def physical_commands_allowed(self) -> bool:
        """Return whether any physical command may be issued."""
        return self._execution_mode == EXECUTION_MODE_LIVE

    @property
    def policy_phase(self) -> str:
        """Return the typed policy phase."""
        return self._policy_engine.runtime.phase.value

    @property
    def reason_code(self) -> str:
        """Return the stable reason for the latest decision."""
        return self._policy_engine.runtime.last_reason_code.value

    @property
    def policy(self) -> PolicyConfig:
        """Return the immutable configured policy."""
        return self._policy

    @property
    def shed_stack(self) -> list[ShedStackEntry]:
        """Return a copy of controller-owned shed history."""
        return list(self._policy_engine.runtime.shed_stack)

    @property
    def recovery_ready(self) -> bool:
        """Return whether policy stability permits recovery."""
        return self._last_policy_decision.recovery_ready

    @property
    def execution_mode_is_observe(self) -> bool:
        """Return whether this instance is intentionally non-enforcing."""
        return self._execution_mode == EXECUTION_MODE_OBSERVE

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def startup_safe(self) -> bool:
        """Return whether automatic physical starts are still startup-blocked."""
        return self._startup_safe

    @mode.setter  # type: ignore[no-redef, attr-defined]
    def mode(self, value: str) -> None:
        if value not in (MODE_AUTO, MODE_OFF):
            raise ValueError(f"Unsupported mode: {value}")
        if value == MODE_AUTO and self._safety_storage_invalid:
            raise ValueError("safety storage is invalid; resolve persisted state first")
        self._mode = value
        set_mode = getattr(self._store, "set_mode", None)
        if callable(set_mode):
            set_mode(value)
        self._last_action = f"Mode changed to {value}"
        _LOGGER.info("Mode changed to %s", value)

    @property
    def load_sensor_valid(self) -> bool:
        """Return whether the latest load sample is safe to use."""
        return self._load_sensor_valid

    @property
    def load_sensor_reason(self) -> str:
        """Return the latest load sensor validation reason."""
        return self._load_sensor_reason

    @property
    def status(self) -> str:
        return self._status

    @property
    def current_load(self) -> float | None:
        """Return the latest valid load sample, or unknown when unsafe."""
        if not self._load_sensor_valid or not self._load_samples:
            return None
        return self._load_samples[-1]

    @property
    def average_load(self) -> float | None:
        """Return the valid average, or unknown when no safe sample exists."""
        if not self._load_sensor_valid or not self._load_samples:
            return None
        if len(self._load_sample_times) == len(self._load_samples):
            now = time.monotonic()
            values = [
                value
                for value, sample_time in zip(
                    self._load_samples, self._load_sample_times
                )
                if now - sample_time <= self._averaging_period
            ]
            return sum(values) / len(values) if values else None
        # Direct test/manual inserts have no timestamps; do not discard them.
        return sum(self._load_samples) / len(self._load_samples)

    @property
    def available_capacity(self) -> float | None:
        """Return capacity, or unknown when load telemetry is not safe."""
        current = self.current_load
        average = self.average_load
        if current is None or average is None:
            return None
        effective_load = max(current, average) + self._pending_start_w
        limit = self._policy.recovery_target_w if self._policy_enabled else self._max_load
        return max(0.0, limit - effective_load - self._safety_reserve)

    @property
    def pending_start_power(self) -> float:
        """Return expected power reserved until safe reconciliation."""
        return self._pending_start_w

    def _next_operation_id(self, device: ManagedDevice) -> int:
        """Create the causal generation for the next command on a device."""
        self._operation_generation += 1
        operation_id = self._operation_generation
        self._active_operations[device.device_id] = operation_id
        self._last_confirmed_reported_at[device.device_id] = None
        return operation_id

    def _reserve_pending_start(self, device: ManagedDevice) -> _PendingStart:
        """Reserve expected watts before any await at the start boundary."""
        if self._pending_start is not None:
            raise RuntimeError("A start reservation is already active")
        operation_id = self._next_operation_id(device)
        admission_load = self.current_load
        pending = _PendingStart(
            operation_id=operation_id,
            device_id=device.device_id,
            expected_w=float(device.expected_power),
            admission_reported_at=self._load_reported_at,
            admission_generation=self._load_generation,
            admission_load_w=admission_load,
            telemetry_deadline_monotonic=time.monotonic() + self._pending_start_timeout,
        )
        self._pending_start = pending
        self._pending_start_w = pending.expected_w
        return pending

    def _clear_pending_start(self) -> None:
        """Release a reservation only after reconciliation is complete."""
        self._pending_start = None
        self._pending_start_w = 0.0

    async def _reconcile_pending_start(self) -> None:
        """Reconcile a pending start only against causal telemetry."""
        pending = self._pending_start
        if pending is None:
            return
        if pending.phase == "recovery_blocked":
            await self._reconcile_recovery_block(pending)
            return
        if not (
            pending.phase == "waiting_load_telemetry"
            and pending.on_confirmed_reported_at is not None
            and self._load_reported_at is not None
            and self._load_reported_at > pending.on_confirmed_reported_at
            and self._load_generation > pending.admission_generation
        ):
            return
        device = self._model.get_device(pending.device_id)
        if device is None:
            self._status = STATUS_SAFETY_BLOCKED
            self._last_action = "Safety blocked — pending device no longer exists"
            self._clear_pending_start()
            return
        if self._pending_restore_entry is not None:
            target = self._policy_engine.next_restore_target()
            if target is None or target.device_id != pending.device_id:
                self._status = STATUS_SAFETY_BLOCKED
                self._last_action = (
                    "Safety blocked — recovery stack target changed during reconciliation"
                )
                return
            self._policy_engine.pop_restore_target()
            self._pending_restore_entry = None
            self._store.save_policy_runtime(self._policy_engine)
        self._clear_pending_start()

    async def _reconcile_recovery_block(self, pending: _PendingStart) -> None:
        """Release failed-start quarantine only after durable OFF/load proof."""
        if (
            pending.rollback_off_reported_at is None
            or self._load_reported_at is None
            or self._load_reported_at <= pending.rollback_off_reported_at
            or self._load_generation <= pending.admission_generation
        ):
            return
        device = self._model.get_device(pending.device_id)
        if device is None:
            self._status = STATUS_SAFETY_BLOCKED
            self._last_action = "Safety blocked — pending device no longer exists"
            self._clear_pending_start()
            return
        if self._logical_device_state(device) is not False:
            return
        current_load = self.current_load
        average_load = self.average_load
        recovery_limit = (
            self._policy.recovery_start_w if self._policy_enabled else self._max_load
        )
        if (
            current_load is None
            or average_load is None
            or current_load > recovery_limit
            or average_load > recovery_limit
        ):
            return
        old_measured_power = device.measured_power
        old_measured_power_valid = device.measured_power_valid
        old_measured_power_reason = device.measured_power_reason
        if device.power_sensor_id:
            try:
                self._read_current_device_power_for_clear(device)
            except ValueError:
                return
        if device.measured_power_valid and device.measured_power > QUARANTINE_CLEAR_MAX_POWER_W:
            return

        old_faulted = set(self._faulted)
        old_recovery_blocked = set(self._recovery_blocked)
        old_fault_reasons = dict(self._fault_reasons)
        old_fault_state_dirty = self._fault_state_dirty
        old_fault_reason = self._safety_fault_reason
        old_status = self._status
        old_pending_restore = self._pending_restore_entry
        store_snapshot = (
            self._store.snapshot() if isinstance(self._store, RuntimeStore) else None
        )
        self._recovery_blocked.discard(device.device_id)
        if device.device_id not in self._faulted:
            self._fault_reasons.pop(device.device_id, None)
        self._fault_state_dirty = True
        self._pending_restore_entry = None
        device.is_on = False
        self._last_admission_generation = self._load_generation
        if not self._faulted and not self._recovery_blocked:
            self._safety_fault_reason = None
        self._status = STATUS_MONITORING
        self._last_action = f"Automatic recovery quarantine cleared for {device.name}"
        self._record_action(
            {
                "operation_id": f"recovery-clear-{pending.operation_id}",
                "device_id": device.device_id,
                "action": "clear_quarantine",
                "result": "confirmed",
                "source": "recovery",
                "reason": ReasonCode.NORMAL_MONITORING.value,
            }
        )
        try:
            self._save_runtime_snapshot()
            await self._store.async_save()
        except Exception:
            self._faulted = old_faulted
            self._recovery_blocked = old_recovery_blocked
            self._fault_reasons = old_fault_reasons
            self._fault_state_dirty = old_fault_state_dirty
            self._safety_fault_reason = old_fault_reason
            self._status = old_status
            self._last_action = (
                "Automatic recovery clear persistence failed; quarantine retained"
            )
            self._pending_restore_entry = old_pending_restore
            device.is_on = None
            device.measured_power = old_measured_power
            device.measured_power_valid = old_measured_power_valid
            device.measured_power_reason = old_measured_power_reason
            if store_snapshot is not None:
                self._store.restore_snapshot(store_snapshot)
            _LOGGER.exception("Failed to persist automatic recovery quarantine clear")
            return
        self._fault_state_dirty = False
        dismissed = await self._dismiss_fault_notification(device.device_id)
        if not dismissed:
            self._fault_notifications_pending_dismissal.add(device.device_id)
            self._last_action = (
                f"Recovery quarantine cleared for {device.name}; "
                "notification dismissal will be retried"
            )
        self._emit_event(
            "power_orchestrator.quarantine_cleared",
            {
                "device_id": device.device_id,
                "source": "recovery",
            },
        )
        self._clear_pending_start()

    async def _expire_pending_start_if_needed(self) -> None:
        """Bound the telemetry barrier and recover the pending relay safely."""
        pending = self._pending_start
        if pending is None or pending.phase not in {
            "reserved",
            "waiting_load_telemetry",
        }:
            return
        if time.monotonic() < pending.telemetry_deadline_monotonic:
            return
        device = self._model.get_device(pending.device_id)
        if device is None:
            self._status = STATUS_SAFETY_BLOCKED
            self._last_action = "Safety blocked — pending device no longer exists"
            self._clear_pending_start()
            return
        self._status = STATUS_SAFETY_BLOCKED
        self._last_action = (
            f"Safety blocked — aggregate load report timed out for {device.name}"
        )
        self._mark_recovery_blocked(device)
        stopped = await self._turn_off_device(device)
        pending = self._pending_start
        if pending is not None and pending.device_id == device.device_id:
            pending.phase = "recovery_blocked"
            pending.rollback_off_reported_at = (
                self._last_confirmed_reported_at.get(device.device_id)
                if stopped
                else None
            )
        self._mark_recovery_blocked(device)

    @property
    def last_action(self) -> str:
        return self._last_action

    @staticmethod
    def _state_reported_timestamp(state: Any) -> float | None:
        """Return a finite epoch timestamp for an HA state report."""
        reported = getattr(state, "last_reported", None)
        if isinstance(reported, datetime):
            if reported.tzinfo is None:
                reported = reported.replace(tzinfo=timezone.utc)
            timestamp = reported.timestamp()
        elif isinstance(reported, (int, float)) and not isinstance(reported, bool):
            timestamp = float(reported)
        else:
            return None
        return timestamp if math.isfinite(timestamp) else None

    def _state_is_fresh(self, state: Any) -> bool:
        """Require a recent HA state report before using it for control."""
        timestamp = self._state_reported_timestamp(state)
        if timestamp is None:
            return False
        age = time.time() - timestamp
        return 0 <= age <= self._safety_input_max_age

    @property
    def grid_safety_source_configured(self) -> bool:
        """Return whether the selected grid/battery safety source exists."""
        if self._grid_loss_mode == GRID_LOSS_MODE_SENSOR:
            return bool(self._grid_loss_sensor)
        if self._grid_loss_mode == GRID_LOSS_MODE_THRESHOLD:
            return bool(
                self._battery_soc_sensor is not None
                and self._battery_threshold is not None
            )
        return False

    @property
    def grid_ok(self) -> bool:
        """Return True only when the configured safety source is valid."""
        if self._grid_loss_mode == GRID_LOSS_MODE_SENSOR:
            if not self._grid_loss_sensor:
                return False
            state = self.hass.states.get(self._grid_loss_sensor)
            return (
                state is not None
                and self._state_is_fresh(state)
                and getattr(state, "state", None) == STATE_ON
            )

        if self._grid_loss_mode == GRID_LOSS_MODE_THRESHOLD:
            if not self._battery_soc_sensor or self._battery_threshold is None:
                return False
            state = self.hass.states.get(self._battery_soc_sensor)
            raw_state = getattr(state, "state", None) if state is not None else None
            attributes = getattr(state, "attributes", {}) if state is not None else {}
            unit = attributes.get("unit_of_measurement") if isinstance(attributes, dict) else None
            if (
                state is None
                or unit != "%"
                or not self._state_is_fresh(state)
                or raw_state is None
                or str(raw_state).strip().lower() in {
                STATE_UNKNOWN,
                STATE_UNAVAILABLE,
                "",
                }
            ):
                return False
            try:
                soc = float(raw_state)
            except (TypeError, ValueError):
                return False
            if not math.isfinite(soc) or not 0 <= soc <= 100:
                return False
            return soc > self._battery_threshold

        return False

    async def _persist_runtime_if_dirty(self) -> bool:
        """Persist safety state and action journal, retaining dirty flags on failure."""
        if not self._fault_state_dirty and not self._journal_dirty:
            return True
        try:
            self._save_runtime_snapshot()
            await self._store.async_save()
        except Exception as exc:
            _LOGGER.error("Failed to persist Power Orchestrator runtime state: %s", exc)
            return False
        self._fault_state_dirty = False
        self._journal_dirty = False
        return True

    async def _persist_fault_state_if_dirty(self) -> None:
        """Durably retain quarantine and journal state without weakening fail-closed memory."""
        await self._persist_runtime_if_dirty()

    async def _notify_faults(self) -> None:
        """Create one persistent notification for each active quarantined device."""
        for device_id in sorted(self._faulted | self._recovery_blocked):
            if device_id in self._fault_notifications_sent:
                continue
            device = self._model.get_device(device_id)
            if device is None:
                continue
            reason = self._fault_reasons.get(device_id) or self._safety_fault_reason or (
                ReasonCode.RECOVERY_BLOCKED.value
                if device_id in self._recovery_blocked
                else ReasonCode.FAULT.value
            )
            try:
                await self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "Power Orchestrator: safety quarantine",
                        "message": (
                            f"{device.name} is quarantined and remains unavailable to "
                            f"normal recovery. Reason: {reason}."
                        ),
                        "notification_id": (
                            f"{DOMAIN}_{self._entry_id}_{device_id}_safety_fault"
                        ),
                    },
                    blocking=True,
                )
            except Exception as exc:
                _LOGGER.error("Failed to create safety notification for %s: %s", device_id, exc)
            else:
                self._fault_notifications_sent.add(device_id)

    async def _dismiss_fault_notification(self, device_id: str) -> bool:
        """Dismiss the diagnostic notification after a verified quarantine clear."""
        try:
            await self.hass.services.async_call(
                "persistent_notification",
                "dismiss",
                {
                    "notification_id": f"{DOMAIN}_{self._entry_id}_{device_id}_safety_fault"
                },
                blocking=True,
            )
        except Exception as exc:
            _LOGGER.warning("Failed to dismiss safety notification for %s: %s", device_id, exc)
            return False
        self._fault_notifications_sent.discard(device_id)
        return True

    async def _retry_fault_notification_dismissals(self) -> None:
        """Retry notification cleanup without reopening a cleared quarantine."""
        for device_id in tuple(self._fault_notifications_pending_dismissal):
            if await self._dismiss_fault_notification(device_id):
                self._fault_notifications_pending_dismissal.discard(device_id)


    async def _evaluate_safely(self) -> None:
        """Run evaluation and emergency-stop on unexpected evaluator errors."""
        try:
            await self._evaluate()
        except Exception as exc:
            self._status = STATUS_SAFETY_BLOCKED
            self._load_sensor_valid = False
            self._load_sensor_reason = "evaluation_error"
            self._last_action = "Safety blocked — evaluation error"
            _LOGGER.exception("Evaluation error: %s", exc)
            try:
                await self._handle_grid_loss()
            except Exception:
                _LOGGER.exception("Emergency stop after evaluation error failed")
        finally:
            await self._persist_fault_state_if_dirty()
            await self._notify_faults()
            await self._retry_fault_notification_dismissals()

    async def _async_update_data(self) -> dict[str, Any]:
        """Periodic evaluation, serialized with all forced evaluations."""
        async with self._evaluation_lock:
            await self._evaluate_safely()
        return self._build_data()

    # ── Main evaluation ────────────────────────────────────────────

    async def _evaluate(self) -> None:
        """Main evaluation loop — policy decisions are side-effect free until dispatch."""
        # 1. Update device states and bound unresolved telemetry reservations.
        await self._refresh_device_states()
        await self._expire_pending_start_if_needed()

        # 2. Sample current load and retain validity separately from its value.
        load = self._read_load_sensor()
        if self._load_sensor_valid:
            if self._accept_load_report():
                self._append_load_sample(load)
            await self._reconcile_pending_start()
            if self._policy_enabled:
                if (
                    self._policy_engine.runtime.pending_post_shed_generation is not None
                    and self._policy_engine.runtime.pending_post_shed_after_reported_at is None
                ):
                    self._policy_engine.set_post_shed_fence(self._load_reported_at)
                else:
                    self._policy_engine.reconcile_shed(
                        self._load_generation,
                        reported_at=self._load_reported_at,
                    )
        else:
            # Never let an invalid sample become a synthetic 0 W reading or
            # release power that is still unresolved by telemetry.
            self._load_samples.clear()
            self._load_sample_times.clear()

        # 3. A missing/invalid grid or battery safety source is an emergency
        # state. Observe mode records it but never becomes a second physical owner.
        grid_ok = self.grid_ok
        if not grid_ok:
            self._status = STATUS_GRID_LOSS
            self._policy_engine.runtime.phase = PolicyPhase.GRID_LOSS
            self._policy_engine.runtime.last_reason_code = ReasonCode.GRID_LOSS
            await self._notify_manual_overrides()
            await self._handle_grid_loss()
            return

        # The emergency-stop ownership latch ends only after the safety source recovers.
        self._grid_loss_expected_off.clear()
        self._manual_override_notified.clear()

        # 4. A missing/invalid load sample must never authorize a start.
        if not self._load_sensor_valid:
            self._status = STATUS_SAFETY_BLOCKED
            self._policy_engine.runtime.phase = PolicyPhase.FAULT
            self._policy_engine.runtime.last_reason_code = ReasonCode.TELEMETRY_INVALID
            self._last_action = (
                f"Safety blocked — load sensor {self._load_sensor_reason}"
            )
            return

        current = self.current_load
        avg = self.average_load
        if current is None or avg is None:
            self._status = STATUS_SAFETY_BLOCKED
            self._load_sensor_valid = False
            self._load_sensor_reason = "no_usable_sample"
            self._policy_engine.runtime.phase = PolicyPhase.FAULT
            self._policy_engine.runtime.last_reason_code = ReasonCode.TELEMETRY_INVALID
            self._last_action = "Safety blocked — no usable load sample"
            return

        # Hard interlock is instantaneous and bypasses policy dwell timers.
        hard_interlock = self._policy.hard_interlock_w if self._policy_enabled else None
        if hard_interlock is not None and current >= hard_interlock:
            self._last_policy_decision = PolicyDecision(
                True,
                False,
                None,
                ReasonCode.HARD_INTERLOCK,
                0.0,
            )
            self._policy_engine.runtime.phase = PolicyPhase.SHEDDING
            self._policy_engine.runtime.last_reason_code = ReasonCode.HARD_INTERLOCK
            self._status = STATUS_LOAD_SHEDDING
            self._last_action = (
                f"Hard interlock exceeded at {current:.0f} W; emergency shedding required"
            )
            self._emit_event(
                EVENT_DECISION,
                {
                    "phase": self.policy_phase,
                    "reason_code": self.reason_code,
                    "current_load": current,
                    "load_generation": self._load_generation,
                    "triggered": True,
                    "recovery_ready": False,
                },
            )
            await self._perform_emergency_all_stop()
            return

        if self._execution_mode_reconciliation_required:
            issued_at = self._execution_mode_transition_issued_at
            transition_generation = self._execution_mode_transition_generation
            fresh_reports = all(
                self._logical_device_reported_at(device) is not None
                and self._logical_device_state(device) is not None
                for device in self._model.all_devices()
            )
            report_ready = (
                issued_at is not None
                and transition_generation is not None
                and self._load_reported_at is not None
                and self._load_reported_at > issued_at
                and self._load_generation > transition_generation
                and fresh_reports
            )
            if not report_ready:
                self._status = STATUS_SAFETY_BLOCKED
                self._last_action = (
                    "Safety blocked — waiting for post-execution-mode telemetry reconciliation"
                )
                return
            self._execution_mode_reconciliation_required = False
            self._execution_mode_transition_issued_at = None
            self._execution_mode_transition_generation = None
            self._last_admission_generation = self._load_generation
            self._status = STATUS_MONITORING
            self._last_action = "Execution-mode telemetry reconciliation complete"
            return

        # 5. Use the versioned policy when configured by the integration.  Direct
        # constructor callers without a policy retain the legacy immediate ceiling
        # for backwards-compatible unit tests and migrations.
        if self._policy_enabled:
            self._last_policy_decision = self._policy_engine.observe_load(
                current,
                now=time.monotonic(),
            )
            self._emit_event(
                EVENT_DECISION,
                {
                    "phase": self.policy_phase,
                    "reason_code": self.reason_code,
                    "current_load": current,
                    "load_generation": self._load_generation,
                    "triggered": self._last_policy_decision.triggered,
                    "recovery_ready": self._last_policy_decision.recovery_ready,
                },
            )
            if self._last_policy_decision.triggered:
                self._status = STATUS_LOAD_SHEDDING
                if not self.physical_commands_allowed:
                    self._status = STATUS_OBSERVE
                    self._last_action = (
                        "Observe: overload policy would shed one device "
                        f"({self._last_policy_decision.reason_code.value}); no physical command issued"
                    )
                    return
                if not self._policy_engine.can_shed_again(self._load_generation):
                    self._status = STATUS_RECOVERY_WAIT
                    self._last_action = (
                        "Waiting for a newer aggregate-load report before another shed"
                    )
                    return
                await self._perform_shedding(current)
                return
        else:
            # Legacy compatibility path: the old max_load is an immediate current
            # interlock and average ceiling. The installed versioned policy does not
            # use this branch.
            if current > self._max_load:
                self._status = STATUS_LOAD_SHEDDING
                if not self.physical_commands_allowed:
                    self._status = STATUS_OBSERVE
                    self._last_action = "Observe: legacy overload would shed; no physical command issued"
                    return
                await self._perform_shedding(current)
                return
            if avg > self._max_load:
                self._status = STATUS_LOAD_SHEDDING
                if not self.physical_commands_allowed:
                    self._status = STATUS_OBSERVE
                    self._last_action = "Observe: legacy average overload would shed; no physical command issued"
                    return
                await self._perform_shedding(avg)
                return

        # 6. Explicit arm requires a newer aggregate report after startup.
        if self._mode == MODE_AUTO and self._post_arm_reconciliation_required:
            arm_issued_at = self._arm_issued_at
            arm_generation = self._arm_load_generation
            report_ready = (
                arm_issued_at is not None
                and arm_generation is not None
                and self._load_reported_at is not None
                and self._load_reported_at > arm_issued_at
                and self._load_generation > arm_generation
            )
            if not report_ready:
                self._status = STATUS_SAFETY_BLOCKED
                self._last_action = (
                    "Safety blocked — waiting for a post-arm aggregate load report"
                )
                return
            self._post_arm_reconciliation_required = False
            self._last_admission_generation = self._load_generation
            self._status = STATUS_MONITORING
            self._last_action = (
                "Startup reconciliation complete; waiting for the next load report"
            )
            return

        # 7. Startup-safe latch blocks all normal admission until explicit arm.
        if self._startup_safe:
            self._status = STATUS_MONITORING
            self._last_action = "Startup-safe: waiting for explicit auto arm"
            return

        # 8. Do not issue another start until the aggregate load sensor has
        # reported after the previous confirmed start.
        if self._pending_start_w > 0:
            self._status = STATUS_MONITORING
            self._last_action = (
                "Waiting for post-start aggregate load report "
                f"(reserved={self._pending_start_w:.0f} W)"
            )
            return

        # Recovery must pass the policy's low-load/stabilization gate and only
        # considers the controller-owned LIFO shed stack.
        if self._policy_enabled and self._policy_engine.runtime.shed_stack:
            if not self._last_policy_decision.recovery_ready:
                self._status = STATUS_RECOVERY_WAIT
                self._last_action = "Recovery waiting for stable low load"
                return

        # 9. Add/restore one load if capacity is available.
        effective_load = max(current, avg) + self._pending_start_w
        limit = self._policy.recovery_target_w if self._policy_enabled else self._max_load
        capacity = limit - self._hysteresis - effective_load - self._safety_reserve
        if capacity > 0 and self._mode == MODE_AUTO:
            if not self.physical_commands_allowed:
                self._status = STATUS_OBSERVE
                self._last_action = "Observe: a load would be admitted; no physical command issued"
                return
            self._status = STATUS_ADDING_LOAD
            await self._perform_adding(avg, capacity)
        else:
            self._status = STATUS_MONITORING
            if self.execution_mode_is_observe:
                self._status = STATUS_OBSERVE
                self._last_action = "Observe: monitoring without physical commands"

    def _accept_load_report(self) -> bool:
        """Advance load generation once for each distinct HA report."""
        reported_at = self._load_reported_at
        if reported_at is not None:
            if (
                self._last_accepted_load_reported_at is not None
                and reported_at <= self._last_accepted_load_reported_at
            ):
                return False
            self._last_accepted_load_reported_at = reported_at
        self._load_generation += 1
        return True

    def _append_load_sample(self, value: float) -> None:
        """Append a fresh load sample and prune it by elapsed time."""
        if len(self._load_sample_times) != len(self._load_samples):
            self._load_samples.clear()
            self._load_sample_times.clear()
        now = time.monotonic()
        self._load_samples.append(value)
        self._load_sample_times.append(now)
        while (
            self._load_sample_times
            and now - self._load_sample_times[0] > self._averaging_period
        ):
            self._load_sample_times.popleft()
            self._load_samples.popleft()

    # ── Device state refresh ───────────────────────────────────────

    def _mark_recovery_blocked(
        self,
        device: ManagedDevice,
        reason: str | None = None,
    ) -> None:
        """Keep a failed start quarantined until relay/load reconciliation."""
        self._recovery_blocked.add(device.device_id)
        fault_reason = reason or self._safety_fault_reason or ReasonCode.RECOVERY_BLOCKED.value
        if isinstance(fault_reason, str) and fault_reason.strip():
            self._fault_reasons[device.device_id] = fault_reason[:160]
        self._fault_state_dirty = True
        pending = self._pending_start
        if pending is not None and pending.device_id == device.device_id:
            pending.phase = "recovery_blocked"
            pending.rollback_off_reported_at = self._last_confirmed_reported_at.get(
                device.device_id
            )
        device.is_on = None

    def _actuator_state_on(self, entity_id: str, state: Any) -> bool | None:
        """Normalize one switch/light/climate state without guessing unknown."""
        raw_state = getattr(state, "state", None) if state is not None else None
        if not self._state_is_fresh(state):
            return None
        normalized = str(raw_state).strip().lower() if raw_state is not None else ""
        if normalized in {STATE_UNKNOWN, STATE_UNAVAILABLE, ""}:
            return None
        if entity_id.split(".", 1)[0] == "climate":
            return normalized != STATE_OFF
        if normalized not in {STATE_ON, STATE_OFF}:
            return None
        return normalized == STATE_ON

    def _logical_device_state(self, device: ManagedDevice) -> bool | None:
        """Return ON only when every actuator in a logical group is ON."""
        states = [
            self._actuator_state_on(entity_id, self.hass.states.get(entity_id))
            for entity_id in device.control_entity_ids
        ]
        if not states or any(value is None for value in states):
            return None
        if len(set(states)) != 1:
            return None
        return states[0]

    def _logical_device_reported_at(self, device: ManagedDevice) -> float | None:
        """Return the newest causal report marker for every actuator in a group."""
        markers = [
            self._state_reported_timestamp(self.hass.states.get(entity_id))
            for entity_id in device.control_entity_ids
        ]
        if not markers or any(marker is None for marker in markers):
            return None
        return max(marker for marker in markers if marker is not None)

    def _manual_start_is_active(self) -> bool:
        """Return whether a normal-shedding/recovery guard is active."""
        phase = self._policy_engine.runtime.phase
        return (
            phase in {
                PolicyPhase.SHEDDING,
                PolicyPhase.WAITING_LOAD_RECONCILIATION,
            }
            or (
                bool(self._policy_engine.runtime.shed_stack)
                and phase in {PolicyPhase.RECOVERY_WAIT, PolicyPhase.RECOVERY}
            )
            or self._policy_engine.runtime.pending_post_shed_generation is not None
        )

    def _logical_device_confirmed_off(self, device: ManagedDevice) -> bool:
        """Require every logical actuator to have fresh confirmed OFF state."""
        return all(
            self._actuator_state_on(entity_id, self.hass.states.get(entity_id)) is False
            for entity_id in device.control_entity_ids
        )

    async def _handle_external_start(self, device: ManagedDevice) -> None:
        """Record and compensate an unauthorized ON transition when possible."""
        lease = self._authorization_leases.pop(device.device_id, None)
        if lease is not None and lease.allows(
            device.device_id,
            STATE_ON,
            time.time(),
            reported_at=self._logical_device_reported_at(device),
        ):
            device.ownership = Ownership.PLANNER
            device.ownership_until = None
            return
        if self._manual_start_is_active():
            self._policy_engine.runtime.manual_start_blocked_count += 1
            self._faulted.add(device.device_id)
            self._fault_reasons[device.device_id] = ReasonCode.MANUAL_START_BLOCKED.value
            self._fault_state_dirty = True
            device.ownership = Ownership.EXTERNAL
            device.ownership_until = time.time() + self._external_ownership_grace
            self._safety_fault_reason = ReasonCode.MANUAL_START_BLOCKED.value
            self._status = STATUS_SAFETY_BLOCKED
            self._last_action = (
                f"Manual start blocked for {device.name}; "
                "only the authorized restore target may start"
            )
            self._record_action(
                {
                    "operation_id": f"manual-{self._operation_generation}",
                    "device_id": device.device_id,
                    "action": "manual_start",
                    "result": "blocked",
                    "reason": ReasonCode.MANUAL_START_BLOCKED.value,
                }
            )
            if self.physical_commands_allowed:
                await self._turn_off_device(device)
            return
        # External starts are not stolen by the planner.  Preserve them for a
        # bounded lease; emergency grid loss remains an explicit override.
        device.ownership = Ownership.EXTERNAL
        device.ownership_until = time.time() + self._external_ownership_grace
        self._record_action(
            {
                "operation_id": f"external-{self._operation_generation}",
                "device_id": device.device_id,
                "action": "manual_start",
                "result": "preserved",
                "reason": ReasonCode.EXTERNAL_OWNERSHIP.value,
            }
        )

    async def _refresh_device_states(self) -> None:
        """Read logical device telemetry, ownership transitions, and measurements."""
        for device in self._model.all_devices():
            previous = self._last_observed_state.get(device.device_id, device.is_on)
            logical_state = self._logical_device_state(device)
            if device.device_id in self._recovery_blocked:
                # A late ON after a causal rollback is unresolved physical
                # activation, never a normal confirmed state.
                device.is_on = None
                if logical_state is True:
                    self._faulted.add(device.device_id)
                    self._fault_reasons[device.device_id] = "delayed_activation"
                    self._fault_state_dirty = True
                    self._status = STATUS_SAFETY_BLOCKED
                    self._last_action = (
                        f"Delayed activation detected for {device.name}; "
                        "emergency stop required"
                    )
                    stopped = await self._turn_off_device(device, emergency=True)
                    if stopped:
                        self._mark_recovery_blocked(
                            device,
                            reason=ReasonCode.DELAYED_ACTIVATION.value,
                        )
                else:
                    self._mark_recovery_blocked(device)
            else:
                device.is_on = logical_state
                if previous is not True and logical_state is True:
                    restored_state = (
                        not self._initial_device_reconciliation_complete
                        and previous is None
                        and device.ownership is not Ownership.UNKNOWN
                    )
                    if not restored_state:
                        await self._handle_external_start(device)
                if (
                    device.ownership is Ownership.EXTERNAL
                    and device.ownership_until is not None
                    and time.time() >= device.ownership_until
                ):
                    device.ownership = Ownership.PLANNER
                    device.ownership_until = None
            self._last_observed_state[device.device_id] = device.is_on

            # Invalid measured telemetry stays invalid; it is never exposed as
            # a valid 0 W reading and never authorizes capacity decisions.
            device.measured_power = 0.0
            device.measured_power_valid = False
            device.measured_power_reason = "not_configured"
            if device.power_sensor_id:
                ps = self.hass.states.get(device.power_sensor_id)
                ps_state = getattr(ps, "state", None) if ps is not None else None
                attributes = getattr(ps, "attributes", {}) if ps is not None else {}
                unit = attributes.get("unit_of_measurement") if isinstance(attributes, dict) else None
                if ps is None or not self._state_is_fresh(ps):
                    device.measured_power_reason = "unavailable_or_stale"
                elif str(unit).strip().lower() not in {"w", "watt", "watts"}:
                    device.measured_power_reason = "unsupported_unit"
                elif isinstance(ps_state, bool) or not isinstance(ps_state, (str, int, float)):
                    device.measured_power_reason = "non_numeric"
                else:
                    try:
                        measured = float(ps_state)
                    except (ValueError, TypeError):
                        measured = math.nan
                    if not math.isfinite(measured):
                        device.measured_power_reason = "invalid_value"
                    elif measured < 0:
                        device.measured_power_reason = "negative_value"
                    else:
                        device.measured_power = measured
                        device.measured_power_valid = True
                        device.measured_power_reason = "ok"
        self._initial_device_reconciliation_complete = True

    # ── Load sensor ────────────────────────────────────────────────

    def _read_load_sensor(self) -> float:
        """Read and validate the configured load sensor."""
        self._load_sensor_valid = False
        self._load_sensor_reason = "missing"
        self._load_reported_at = None
        state = self.hass.states.get(self._load_sensor)
        raw_state = getattr(state, "state", None) if state is not None else None
        if (
            state is None
            or not self._state_is_fresh(state)
            or raw_state is None
            or str(raw_state).strip().lower() in {
                STATE_UNKNOWN,
                STATE_UNAVAILABLE,
                "",
            }
        ):
            self._load_sensor_reason = "unavailable_or_stale"
            return 0.0
        attributes = getattr(state, "attributes", {})
        if not isinstance(attributes, dict):
            attributes = {}
        unit = attributes.get("unit_of_measurement")
        if str(unit).strip().lower() not in {"w", "watt", "watts"}:
            self._load_sensor_reason = "unsupported_unit"
            return 0.0
        try:
            value = float(raw_state)
        except (ValueError, TypeError):
            self._load_sensor_reason = "non_numeric"
            return 0.0
        if not math.isfinite(value) or value < 0:
            self._load_sensor_reason = "invalid_value"
            return 0.0
        self._load_reported_at = self._state_reported_timestamp(state)
        self._load_sensor_valid = True
        self._load_sensor_reason = "ok"
        return value

    # ── Solar forecast check ───────────────────────────────────────

    def _solar_forecast_ok(self, device: ManagedDevice) -> bool:
        """Check whether fresh estimated power covers the device."""
        if not device.only_from_solar:
            return True
        if not self._solar_forecast_entity:
            return False  # only_from_solar but no forecast → can't enable
        state = self.hass.states.get(self._solar_forecast_entity)
        forecast_power = current_power_forecast_w(state)
        if forecast_power is None:
            return False
        return forecast_power >= device.expected_power

    def _pause_device(self, device: ManagedDevice) -> None:
        """Apply pause to both live model and persistent storage."""
        duration = max(0.0, float(self._pause_period))
        device.pause_until = time.time() + duration
        device.last_turn_off_time = time.time()
        self._store.set_pause(device.device_id, duration)

    def _clear_pause(self, device: ManagedDevice) -> None:
        """Clear pause in both live model and persistent storage."""
        device.pause_until = None
        self._store.clear_pause(device.device_id)

    # ── Grid loss handling ─────────────────────────────────────────

    async def _handle_grid_loss(self) -> None:
        """Grid lost — emergency-stop all optional devices in live mode only."""
        if self.execution_mode_is_observe:
            self._status = STATUS_OBSERVE
            self._last_action = (
                "Observe: grid loss would stop all optional devices; "
                "no physical command issued"
            )
            self._emit_event(
                EVENT_ACTION,
                {
                    "action": "grid_loss_all_stop",
                    "result": "observe_only",
                    "reason_code": ReasonCode.GRID_LOSS.value,
                },
            )
            return
        failed: list[str] = []
        for device in self._model.get_sorted_devices_reversed():
            pending = self._pending_start
            all_confirmed_off = self._logical_device_confirmed_off(device)
            if (
                all_confirmed_off
                and not (
                    pending is not None and pending.device_id == device.device_id
                )
            ):
                continue
            # Unknown, stale, or mixed logical-actuator state is fail-safe:
            # attempt the emergency stop for every member of the group.
            if await self._turn_off_device(device, emergency=True):
                if pending is not None and pending.device_id == device.device_id:
                    self._mark_recovery_blocked(device)
                    pending = self._pending_start
                    if pending is not None:
                        pending.phase = "recovery_blocked"
                        pending.rollback_off_reported_at = (
                            self._last_confirmed_reported_at.get(device.device_id)
                        )
                self._grid_loss_expected_off.add(device.device_id)
                self._pause_device(device)
                self._record_action(
                    {
                        "device_id": device.device_id,
                        "action": "turn_off",
                        "result": "confirmed",
                        "reason": ReasonCode.GRID_LOSS.value,
                    }
                )
                _LOGGER.warning("Grid loss: turned off %s", device.name)
            else:
                failed.append(device.name)
                fault_reason = (
                    self._safety_fault_reason or ReasonCode.RELAY_READBACK_TIMEOUT.value
                )
                self._faulted.add(device.device_id)
                self._mark_recovery_blocked(device, reason=fault_reason)
                self._fault_state_dirty = True
                self._record_action(
                    {
                        "device_id": device.device_id,
                        "action": "turn_off",
                        "result": "failed",
                        "reason": ReasonCode.GRID_LOSS.value,
                        "fault_reason": fault_reason,
                    }
                )

        if failed:
            self._status = STATUS_SAFETY_BLOCKED
            self._last_action = (
                "Grid loss — stop failed for: " + ", ".join(failed)
            )
            _LOGGER.error(self._last_action)
        else:
            self._last_action = "Grid loss — all optional devices turned off"
        try:
            self._save_runtime_snapshot()
            await self._store.async_save()
        except Exception as exc:
            _LOGGER.error("Failed to persist grid-loss state: %s", exc)
            self._fault_state_dirty = True
            self._status = STATUS_SAFETY_BLOCKED
            return
        self._fault_state_dirty = False

    async def _notify_manual_overrides(self) -> None:
        """Notify once when a device is re-enabled after an emergency stop."""
        for device in self._model.all_devices():
            if (
                not device.is_on
                or device.device_id not in self._grid_loss_expected_off
                or device.device_id in self._manual_override_notified
            ):
                continue
            try:
                await self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "Power Orchestrator: manual override",
                        "message": (
                            f"{device.name} was re-enabled while grid loss/battery "
                            "safety stop is active. It will be turned off again."
                        ),
                        "notification_id": (
                            f"{DOMAIN}_{self._entry_id}_"
                            f"{device.device_id}_manual_override"
                        ),
                    },
                    blocking=True,
                )
                self._manual_override_notified.add(device.device_id)
            except Exception as exc:
                _LOGGER.error("Failed to create manual override notification: %s", exc)

    # ── Load shedding ──────────────────────────────────────────────

    async def _perform_emergency_all_stop(self) -> None:
        """Attempt an emergency OFF on every non-confirmed-OFF load."""
        if self.execution_mode_is_observe:
            self._status = STATUS_OBSERVE
            self._last_action = (
                "Observe: hard interlock requires emergency all-stop; "
                "no physical commands issued"
            )
            self._emit_event(
                EVENT_ACTION,
                {
                    "action": "emergency_all_stop",
                    "result": "observe_only",
                    "reason": ReasonCode.HARD_INTERLOCK.value,
                },
            )
            return

        failed: list[str] = []
        for device in self._model.get_sorted_devices_reversed():
            logical_state = self._logical_device_state(device)
            if device.is_on is False and logical_state is False:
                continue
            try:
                stopped = await self._turn_off_device(device, emergency=True)
            except Exception as exc:
                _LOGGER.error("Emergency stop failed for %s: %s", device.name, exc)
                stopped = False
            if stopped:
                device.is_on = False
                if self._pending_start is not None and self._pending_start.device_id == device.device_id:
                    self._mark_recovery_blocked(device)
            else:
                failed.append(device.device_id)
                self._faulted.add(device.device_id)
                self._fault_reasons[device.device_id] = ReasonCode.HARD_INTERLOCK.value
                self._fault_state_dirty = True
                device.is_on = None
                self._status = STATUS_SAFETY_BLOCKED
            self._record_action(
                {
                    "operation_id": str(self._last_operation_id or "unknown"),
                    "device_id": device.device_id,
                    "action": "emergency_all_stop",
                    "result": "confirmed" if stopped else "failed",
                    "reason": ReasonCode.HARD_INTERLOCK.value,
                }
            )

        if failed:
            self._status = STATUS_SAFETY_BLOCKED
            self._last_action = (
                "Hard interlock emergency all-stop incomplete; "
                f"failed devices: {', '.join(failed)}"
            )
        else:
            self._status = STATUS_LOAD_SHEDDING
            self._last_action = "Hard interlock emergency all-stop completed"
        self._save_runtime_snapshot()
        await self._store.async_save()
        self._fault_state_dirty = False

    async def _perform_shedding(
        self,
        avg_load: float,
        *,
        emergency: bool = False,
    ) -> None:
        """Turn off one logical device and record a confirmed LIFO snapshot."""
        pending = self._pending_start
        pending_device_id = (
            pending.device_id
            if pending is not None
            and pending.phase in {"reserved", "waiting_load_telemetry"}
            else None
        )
        devices = (
            self._model.get_shed_devices()
            if self._policy_enabled
            else self._model.get_sorted_devices_reversed()
        )
        if pending_device_id is not None:
            pending_device = self._model.get_device(pending_device_id)
            if pending_device is not None:
                devices = [
                    pending_device,
                    *(device for device in devices if device.device_id != pending_device_id),
                ]
        for device in devices:
            is_pending = device.device_id == pending_device_id
            if is_pending:
                if device.is_on is False:
                    continue
            elif device.is_on is not True:
                continue
            if (
                device.ownership is Ownership.EXTERNAL
                and device.ownership_until is not None
                and time.time() < device.ownership_until
            ):
                continue
            snapshot = device.capture_runtime_snapshot(
                {
                    entity_id: self.hass.states.get(entity_id)
                    for entity_id in device.control_entity_ids
                }
            )
            if await self._turn_off_device(device, emergency=emergency):
                if is_pending:
                    self._mark_recovery_blocked(device)
                    pending = self._pending_start
                    if pending is not None:
                        pending.phase = "recovery_blocked"
                        pending.rollback_off_reported_at = (
                            self._last_confirmed_reported_at.get(device.device_id)
                        )
                else:
                    device.ownership = Ownership.PLANNER
                    device.ownership_until = None
                    if self._policy_enabled:
                        reason = self._last_policy_decision.reason_code
                        entry = ShedStackEntry(
                            device_id=device.device_id,
                            operation_id=str(self._last_operation_id or "unknown"),
                            pre_state=True,
                            snapshot=snapshot,
                            load_generation=self._load_generation,
                            reason_code=reason,
                            created_at=time.time(),
                        )
                        self._policy_engine.append_shed(entry)
                        self._policy_engine.set_post_shed_fence(
                            self._last_confirmed_reported_at.get(device.device_id)
                        )
                        self._shed_snapshots[device.device_id] = snapshot
                        self._store.save_policy_runtime(self._policy_engine)
                self._pause_device(device)
                reason_text = (
                    self._last_policy_decision.reason_code.value
                    if self._policy_enabled
                    else ReasonCode.SHED_SUSTAINED_OVERLOAD.value
                )
                self._last_operation_result = "confirmed"
                self._last_action = (
                    f"Load shedding: turned off {device.name} "
                    f"(load={avg_load:.0f} W; reason={reason_text})"
                )
                self._record_action(
                    {
                        "operation_id": str(self._last_operation_id or "unknown"),
                        "device_id": device.device_id,
                        "action": "turn_off",
                        "result": "confirmed",
                        "reason": reason_text,
                        "load_generation": self._load_generation,
                    }
                )
                self._emit_event(
                    EVENT_ACTION,
                    {
                        "operation_id": str(self._last_operation_id or "unknown"),
                        "device_id": device.device_id,
                        "action": "turn_off",
                        "result": "confirmed",
                        "reason_code": reason_text,
                    },
                )
                _LOGGER.info(self._last_action)
                self._save_runtime_snapshot()
                await self._store.async_save()
            else:
                self._faulted.add(device.device_id)
                self._fault_reasons[device.device_id] = ReasonCode.RELAY_READBACK_TIMEOUT.value
                self._fault_state_dirty = True
                self._safety_fault_reason = ReasonCode.RELAY_READBACK_TIMEOUT.value
                self._policy_engine.runtime.phase = PolicyPhase.FAULT
                self._policy_engine.runtime.last_reason_code = ReasonCode.RELAY_READBACK_TIMEOUT
                self._status = STATUS_SAFETY_BLOCKED
                self._last_operation_result = "failed"
                self._last_action = f"Load shedding stop failed for {device.name}"
                self._record_action(
                    {
                        "operation_id": str(self._last_operation_id or "unknown"),
                        "device_id": device.device_id,
                        "action": "turn_off",
                        "result": "failed",
                        "reason": ReasonCode.RELAY_READBACK_TIMEOUT.value,
                    }
                )
                self._save_runtime_snapshot()
                await self._store.async_save()
                self._fault_state_dirty = False
            return  # One at a time; newer aggregate telemetry is required next

    # ── Adding load ────────────────────────────────────────────────

    async def _perform_adding(self, avg_load: float, capacity: float) -> None:
        """Reserve and turn on at most one LIFO restore or normal load."""
        if self._mode != MODE_AUTO or self._startup_safe or self._pending_start is not None:
            return
        if (
            self._load_reported_at is not None
            and self._last_admission_generation == self._load_generation
        ):
            return

        # Recovery is deliberately LIFO and never guesses from arbitrary OFF
        # states. A failed target remains on the stack and quarantined.
        if self._policy_enabled and self._policy_engine.runtime.shed_stack:
            entry = self._policy_engine.next_restore_target()
            if entry is None:
                return
            device = self._model.get_device(entry.device_id)
            if (
                device is None
                or device.is_on is not False
                or device.device_id in self._recovery_blocked
                or device.device_id in self._faulted
                or device.expected_power > capacity
            ):
                return
            if device.ownership is Ownership.EXTERNAL and (
                device.ownership_until is None or time.time() < device.ownership_until
            ):
                return
            device.snapshot = entry.snapshot
            self._clear_pause(device)
            self._pending_restore_entry = entry
            self._reserve_pending_start(device)
            self._last_admission_generation = self._load_generation
            started = await self._turn_on_device(device)
            if started:
                self._last_action = (
                    f"Recovery: restored {device.name} in LIFO order "
                    f"(expected={device.expected_power:.0f} W; waiting for aggregate report)"
                )
            else:
                self._pending_restore_entry = None
            return

        for device in self._model.get_sorted_devices():
            if device.is_on is not False or device.pause_active:
                continue
            if device.device_id in self._recovery_blocked or device.device_id in self._faulted:
                continue
            if device.ownership is Ownership.EXTERNAL and (
                device.ownership_until is None or time.time() < device.ownership_until
            ):
                continue
            if not self._solar_forecast_ok(device):
                continue
            if device.expected_power <= capacity:
                self._reserve_pending_start(device)
                self._last_admission_generation = self._load_generation
                started = await self._turn_on_device(device)
                if started:
                    self._last_action = (
                        f"Added load: turned on {device.name} "
                        f"(expected={device.expected_power} W, "
                        f"capacity={capacity:.0f} W; "
                        "waiting for aggregate load report)"
                    )
                    _LOGGER.info(self._last_action)
                else:
                    # _turn_on_device keeps the reservation and enters the
                    # recovery quarantine; never release it in this branch.
                    if self._pending_start is not None:
                        self._mark_recovery_blocked(device)
                return  # One at a time; wait for causal aggregate report

    # ── Device control ─────────────────────────────────────────────

    async def _confirm_device_state(
        self,
        device: ManagedDevice,
        expected_state: str,
        *,
        operation_id: int,
        command_issued_at: float,
        pre_reported_at: float | Mapping[str, float | None] | None,
    ) -> bool:
        """Confirm fresh causal readback for every actuator in a logical group."""
        deadline = time.monotonic() + self._relay_readback_timeout
        while True:
            if self._active_operations.get(device.device_id) != operation_id:
                return False
            all_confirmed = True
            reported_markers: list[float] = []
            for entity_id in device.control_entity_ids:
                state = self.hass.states.get(entity_id)
                raw = getattr(state, "state", None) if state is not None else None
                normalized = str(raw).strip().lower() if raw is not None else ""
                if entity_id.split(".", 1)[0] == "climate":
                    actuator_on = self._actuator_state_on(entity_id, state)
                    state_matches = (
                        actuator_on is True
                        if expected_state == STATE_ON
                        else actuator_on is False
                    )
                else:
                    state_matches = normalized == expected_state
                reported_at = self._state_reported_timestamp(state)
                pre_marker = (
                    pre_reported_at.get(entity_id)
                    if isinstance(pre_reported_at, Mapping)
                    else pre_reported_at
                )
                if not (
                    state is not None
                    and self._state_is_fresh(state)
                    and state_matches
                    and reported_at is not None
                    and reported_at > command_issued_at
                    and (pre_marker is None or reported_at > pre_marker)
                ):
                    all_confirmed = False
                    break
                reported_markers.append(reported_at)
            if all_confirmed and reported_markers:
                self._last_confirmed_reported_at[device.device_id] = max(reported_markers)
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(self._relay_readback_poll_interval, remaining))

    def _pre_command_reported_at(self, device: ManagedDevice) -> dict[str, float | None]:
        """Capture each actuator report marker before issuing a command."""
        return {
            entity_id: self._state_reported_timestamp(self.hass.states.get(entity_id))
            for entity_id in device.control_entity_ids
        }

    async def _rollback_failed_start(self, device: ManagedDevice) -> bool:
        """Compensate with a separate OFF fence and keep recovery blocked."""
        self._mark_recovery_blocked(device)
        stopped = False
        try:
            stopped = await self._turn_off_device(device)
        except Exception as exc:
            _LOGGER.error("Compensating stop failed for %s: %s", device.entity_id, exc)
        pending = self._pending_start
        if pending is not None and pending.device_id == device.device_id:
            pending.phase = "recovery_blocked"
            pending.rollback_off_reported_at = (
                self._last_confirmed_reported_at.get(device.device_id)
                if stopped
                else None
            )
        self._mark_recovery_blocked(device)
        self._status = STATUS_SAFETY_BLOCKED
        await self._persist_fault_state_if_dirty()
        return stopped

    async def _turn_on_device(self, device: ManagedDevice) -> bool:
        """Turn on only with a reservation and a causal ON report."""
        pending = self._pending_start
        if (
            self._mode != MODE_AUTO
            or not self.physical_commands_allowed
            or self._startup_safe
            or device.is_on is not False
            or pending is None
            or pending.device_id != device.device_id
            or pending.phase != "reserved"
            or self._active_operations.get(device.device_id) != pending.operation_id
        ):
            _LOGGER.info("Ignoring unsafe or unreserved start of %s", device.name)
            return False
        domain = device.entity_id.split(".")[0]
        try:
            pre_reported_at = self._pre_command_reported_at(device)
            command_issued_at = time.time()
            for entity_id in device.control_entity_ids:
                domain = entity_id.split(".", 1)[0]
                if domain == "climate":
                    snapshot = device.snapshot or {}
                    attributes = snapshot.get(entity_id, {}).get("attributes", {})
                    hvac_mode = attributes.get("hvac_mode") or device.hvac_mode_on
                    await self.hass.services.async_call(
                        domain,
                        "set_hvac_mode",
                        {"entity_id": entity_id, "hvac_mode": hvac_mode},
                        blocking=True,
                    )
                else:
                    await self.hass.services.async_call(
                        domain,
                        "turn_on",
                        {"entity_id": entity_id},
                        blocking=True,
                    )
            if not await self._confirm_device_state(
                device,
                STATE_ON,
                operation_id=pending.operation_id,
                command_issued_at=command_issued_at,
                pre_reported_at=pre_reported_at,
            ):
                device.is_on = None
                self._status = STATUS_SAFETY_BLOCKED
                rollback_ok = await self._rollback_failed_start(device)
                self._last_action = (
                    f"Start not confirmed for {device.name}; "
                    f"compensating stop {'confirmed' if rollback_ok else 'failed'}"
                )
                return False
            pending = self._pending_start
            if pending is None or pending.device_id != device.device_id:
                device.is_on = None
                self._status = STATUS_SAFETY_BLOCKED
                return False
            pending.phase = "waiting_load_telemetry"
            pending.on_confirmed_reported_at = self._last_confirmed_reported_at.get(
                device.device_id
            )
            if pending.on_confirmed_reported_at is None:
                device.is_on = None
                self._status = STATUS_SAFETY_BLOCKED
                rollback_ok = await self._rollback_failed_start(device)
                self._last_action = (
                    f"Start readback marker missing for {device.name}; "
                    f"compensating stop {'confirmed' if rollback_ok else 'failed'}"
                )
                return False
            pending.telemetry_deadline_monotonic = (
                time.monotonic() + self._pending_start_timeout
            )
            device.is_on = True
            self._last_observed_state[device.device_id] = True
            device.ownership = Ownership.PLANNER
            device.ownership_until = None
            self._authorization_leases.pop(device.device_id, None)
            self._last_operation_id = str(pending.operation_id)
            self._last_operation_result = "confirmed"
            self._clear_pause(device)
            return True
        except Exception as exc:
            device.is_on = None
            self._status = STATUS_SAFETY_BLOCKED
            rollback_ok = await self._rollback_failed_start(device)
            self._last_action = (
                f"Start failed for {device.name}; "
                f"compensating stop {'confirmed' if rollback_ok else 'failed'}"
            )
            _LOGGER.error("Failed to turn on %s: %s", device.entity_id, exc)
            return False

    async def _turn_off_device(
        self,
        device: ManagedDevice,
        *,
        emergency: bool = False,
    ) -> bool:
        """Turn off every actuator and require causal OFF readback."""
        if not self.physical_commands_allowed:
            self._last_operation_result = "observe_only"
            self._status = STATUS_OBSERVE
            self._last_action = f"Observe: would turn off {device.name}"
            return False
        operation_id = self._next_operation_id(device)
        self._last_operation_id = str(operation_id)
        try:
            pre_reported_at = self._pre_command_reported_at(device)
            command_issued_at = time.time()
            for entity_id in device.control_entity_ids:
                domain = entity_id.split(".", 1)[0]
                if domain == "climate":
                    await self.hass.services.async_call(
                        domain,
                        "set_hvac_mode",
                        {"entity_id": entity_id, "hvac_mode": STATE_OFF},
                        blocking=True,
                    )
                else:
                    await self.hass.services.async_call(
                        domain,
                        "turn_off",
                        {"entity_id": entity_id},
                        blocking=True,
                    )
            if not await self._confirm_device_state(
                device,
                STATE_OFF,
                operation_id=operation_id,
                command_issued_at=command_issued_at,
                pre_reported_at=pre_reported_at,
            ):
                device.is_on = None
                self._last_operation_result = "failed"
                self._safety_fault_reason = ReasonCode.RELAY_READBACK_TIMEOUT.value
                return False
            device.is_on = False
            device.ownership = Ownership.PLANNER
            device.ownership_until = None
            self._last_operation_result = "confirmed"
            return True
        except Exception as exc:
            device.is_on = None
            self._status = STATUS_SAFETY_BLOCKED
            self._last_operation_result = "failed"
            self._safety_fault_reason = str(exc)
            self._last_action = f"Stop failed for {device.name}"
            _LOGGER.error("Failed to turn off %s: %s", device.entity_id, exc)
            return False

    @staticmethod
    def _new_action_id(prefix: str = "action") -> str:
        """Create a process/restart-stable unique action identifier."""
        return f"{prefix}-{uuid.uuid4().hex}"

    def _record_action(self, event: dict[str, Any]) -> None:
        """Upsert one versioned audit action and mark journal persistence dirty."""
        normalized = dict(event)
        normalized.setdefault("action_id", self._new_action_id())
        normalized.setdefault("event_schema", EVENT_SCHEMA_VERSION)
        normalized.setdefault("event_type", EVENT_ACTION)
        normalized.setdefault("operation_id", self._last_operation_id or "unknown")
        normalized.setdefault("policy_phase", self.policy_phase)
        normalized.setdefault("execution_mode", self._execution_mode)
        normalized.setdefault("source", "planner")
        normalized.setdefault("actor_id", None)
        normalized.setdefault("context_id", None)
        self._store.record_action(normalized)
        self._journal_dirty = True

    def _emit_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Publish a bounded, versioned event without making control depend on it."""
        event = {
            **payload,
            "event_schema": EVENT_SCHEMA_VERSION,
            "event_type": event_type,
            "entry_id": self._entry_id,
            "sequence": self._policy_engine.runtime.decision_sequence,
            "timestamp": time.time(),
            "mode": self._mode,
            "execution_mode": self._execution_mode,
            "policy_phase": self.policy_phase,
            "reason_code": payload.get("reason_code", self.reason_code),
            "operation_id": payload.get("operation_id", self._last_operation_id),
            "source": payload.get("source", "planner"),
            "actor_id": payload.get("actor_id"),
            "context_id": payload.get("context_id"),
        }
        self._decision_events.append(event)
        bus = getattr(self.hass, "bus", None)
        fire = getattr(bus, "async_fire", None)
        if callable(fire):
            try:
                fire(event_type, event)
            except Exception:
                _LOGGER.debug("Unable to publish %s event", event_type, exc_info=True)

    def _capacity_for_admission(self, current: float, average: float) -> float:
        """Return conservative expected-power headroom."""
        policy_limit = self._policy.recovery_target_w if self._policy_enabled else self._max_load
        limit = min(self._max_load, policy_limit)
        return limit - max(current, average) - self._pending_start_w - self._safety_reserve - self._hysteresis

    async def async_request_start(
        self,
        device_id: str,
        *,
        source: str = "service",
        actor_id: str | None = None,
        context_id: str | None = None,
    ) -> bool:
        """Guarded logical-device start intent used by dashboards/services."""
        async with self._evaluation_lock:
            device = self._model.get_device(device_id)
            if device is None:
                raise ValueError("unknown device_id")
            if self._execution_mode == EXECUTION_MODE_OBSERVE:
                self._policy_engine.runtime.last_reason_code = ReasonCode.OBSERVE_MODE
                self._last_action = f"Observe: start intent for {device.name} was not executed"
                self._record_action(
                    {
                        "operation_id": f"intent-{self._operation_generation}",
                        "device_id": device_id,
                        "action": "turn_on",
                        "result": "observe_only",
                        "source": source,
                    "actor_id": actor_id,
                    "context_id": context_id,
                        "reason": ReasonCode.OBSERVE_MODE.value,
                    }
                )
                return False
            if self._mode != MODE_AUTO or self._startup_safe:
                raise ValueError("planner is not armed")
            if self._post_arm_reconciliation_required:
                raise ValueError("waiting for post-arm aggregate report")
            if self._execution_mode_reconciliation_required:
                raise ValueError("waiting for post-execution-mode telemetry reconciliation")
            if self._pending_start is not None or self._pending_start_w > 0:
                raise ValueError("another start is awaiting aggregate reconciliation")
            if (
                self._policy_enabled
                and self._policy_engine.runtime.pending_post_shed_generation is not None
            ):
                raise ValueError("waiting for post-shed aggregate reconciliation")
            if self._last_admission_generation == self._load_generation:
                raise ValueError("current aggregate generation was already consumed")
            if (
                not self.grid_ok
                or not self._load_sensor_valid
                or self._load_reported_at is None
                or not 0 <= time.time() - self._load_reported_at <= self._safety_input_max_age
            ):
                raise ValueError("safety telemetry is not fresh and valid")
            current = self.current_load
            average = self.average_load
            if current is None or average is None:
                raise ValueError("load telemetry is not valid")
            if device.is_on is not False:
                raise ValueError("device is not confirmed off")
            if device.device_id in self._recovery_blocked or device.device_id in self._faulted:
                raise ValueError("device is quarantined")
            if device.pause_active:
                raise ValueError("device is paused")
            top = self._policy_engine.next_restore_target() if self._policy_enabled else None
            if self._policy_enabled and self._policy_engine.runtime.shed_stack:
                if not self._last_policy_decision.recovery_ready:
                    raise ValueError("recovery is not ready")
                if top is None or top.device_id != device_id:
                    raise ValueError("only the LIFO restore target may start during recovery")
            if device.ownership is Ownership.EXTERNAL and (
                device.ownership_until is None or time.time() < device.ownership_until
            ) and (top is None or top.device_id != device_id):
                raise ValueError("device is under external ownership grace")
            if device.ownership is Ownership.MANUAL and (top is None or top.device_id != device_id):
                raise ValueError("device is manually owned")
            if self._manual_start_is_active() and (
                top is None or top.device_id != device_id
            ):
                raise ValueError("manual start is blocked during recovery")
            if top is not None and top.device_id != device_id:
                raise ValueError("only the LIFO restore target may start during recovery")
            capacity = self._capacity_for_admission(current, average)
            if device.expected_power > capacity or not self._solar_forecast_ok(device):
                raise ValueError("expected power is not admitted by current policy")
            self._reserve_pending_start(device)
            started = await self._turn_on_device(device)
            self._record_action(
                {
                    "operation_id": str(self._last_operation_id or "unknown"),
                    "device_id": device_id,
                    "action": "turn_on",
                    "result": "confirmed" if started else "failed",
                    "source": source,
                    "actor_id": actor_id,
                    "context_id": context_id,
                    "reason": self.reason_code,
                }
            )
            self._save_runtime_snapshot()
            await self._store.async_save()
            return started

    async def async_request_stop(
        self,
        device_id: str,
        *,
        source: str = "service",
        actor_id: str | None = None,
        context_id: str | None = None,
    ) -> bool:
        """Guarded logical-device stop intent used by dashboards/services."""
        async with self._evaluation_lock:
            device = self._model.get_device(device_id)
            if device is None:
                raise ValueError("unknown device_id")
            if device.is_on is not True:
                return False
            if self._execution_mode == EXECUTION_MODE_OBSERVE:
                self._last_action = f"Observe: stop intent for {device.name} was not executed"
                self._record_action(
                    {
                        "operation_id": f"intent-{self._operation_generation}",
                        "device_id": device_id,
                        "action": "turn_off",
                        "result": "observe_only",
                        "source": source,
                    "actor_id": actor_id,
                    "context_id": context_id,
                        "reason": ReasonCode.OBSERVE_MODE.value,
                    }
                )
                return False
            stopped = await self._turn_off_device(device)
            if stopped:
                self._pause_device(device)
            self._record_action(
                {
                    "operation_id": str(self._last_operation_id or "unknown"),
                    "device_id": device_id,
                    "action": "turn_off",
                    "result": "confirmed" if stopped else "failed",
                    "source": source,
                    "actor_id": actor_id,
                    "context_id": context_id,
                    "reason": self.reason_code,
                }
            )
            self._save_runtime_snapshot()
            await self._store.async_save()
            return stopped

    def _read_current_device_power_for_clear(self, device: ManagedDevice) -> None:
        """Refresh measured power before quarantine clear; never trust cached telemetry."""
        if not device.power_sensor_id:
            return
        state = self.hass.states.get(device.power_sensor_id)
        raw = getattr(state, "state", None) if state is not None else None
        attributes = getattr(state, "attributes", {}) if state is not None else {}
        unit = attributes.get("unit_of_measurement") if isinstance(attributes, dict) else None
        reported_at = self._state_reported_timestamp(state)
        causal_off = self._last_confirmed_reported_at.get(device.device_id)
        if (
            state is None
            or not self._state_is_fresh(state)
            or str(unit).strip().lower() not in {"w", "watt", "watts"}
            or raw is None
            or str(raw).strip().lower() in {STATE_UNKNOWN, STATE_UNAVAILABLE, ""}
            or reported_at is None
            or (causal_off is not None and reported_at <= causal_off)
        ):
            raise ValueError("device measured power is not fresh and valid")
        try:
            measured = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("device measured power is not fresh and valid") from exc
        if not math.isfinite(measured) or measured < 0:
            raise ValueError("device measured power is not fresh and valid")
        if measured > QUARANTINE_CLEAR_MAX_POWER_W:
            raise ValueError("device measured power is not at the clear level")
        device.measured_power = measured
        device.measured_power_valid = True
        device.measured_power_reason = "ok"

    async def async_clear_quarantine(
        self,
        device_id: str,
        *,
        source: str = "service",
        actor_id: str | None = None,
        context_id: str | None = None,
    ) -> bool:
        """Clear a persisted device quarantine only after fresh OFF/load proof."""
        async with self._evaluation_lock:
            device = self._model.get_device(device_id)
            if device is None:
                raise ValueError("unknown device_id")
            if device_id not in self._faulted and device_id not in self._recovery_blocked:
                return False
            if self._pending_start is not None and self._pending_start.device_id == device_id:
                raise ValueError("device still has a pending operation")
            if not all(
                self._state_is_fresh(self.hass.states.get(entity_id))
                and self._actuator_state_on(entity_id, self.hass.states.get(entity_id)) is False
                for entity_id in device.control_entity_ids
            ):
                raise ValueError("device OFF readback is not fresh and verified")
            if (
                not self._load_sensor_valid
                or self._load_reported_at is None
                or time.time() - self._load_reported_at > self._safety_input_max_age
            ):
                raise ValueError("aggregate load telemetry is not fresh and valid")
            current_load = self.current_load
            average_load = self.average_load
            if current_load is None or average_load is None:
                raise ValueError("aggregate load telemetry is not usable")
            recovery_limit = self._policy.recovery_start_w if self._policy_enabled else self._max_load
            if current_load > recovery_limit or average_load > recovery_limit:
                raise ValueError("aggregate load is still above the clear gate")
            old_measured_power = device.measured_power
            old_measured_power_valid = device.measured_power_valid
            old_measured_power_reason = device.measured_power_reason
            if device.power_sensor_id:
                self._read_current_device_power_for_clear(device)

            old_faulted = set(self._faulted)
            old_recovery_blocked = set(self._recovery_blocked)
            old_state = device.is_on
            old_fault_reason = self._safety_fault_reason
            old_fault_reasons = dict(self._fault_reasons)
            old_fault_state_dirty = self._fault_state_dirty
            old_status = self._status
            old_last_action = self._last_action
            store_snapshot = (
                self._store.snapshot() if isinstance(self._store, RuntimeStore) else None
            )
            self._faulted.discard(device_id)
            self._recovery_blocked.discard(device_id)
            self._fault_reasons.pop(device_id, None)
            self._fault_state_dirty = True
            device.is_on = False
            self._status = STATUS_MONITORING
            if not self._faulted and not self._recovery_blocked:
                self._safety_fault_reason = None
            self._last_action = f"Quarantine cleared for {device.name} ({source})"
            self._record_action(
                {
                    "operation_id": f"clear-{self._operation_generation}",
                    "device_id": device_id,
                    "action": "clear_quarantine",
                    "result": "confirmed",
                    "source": source,
                    "actor_id": actor_id,
                    "context_id": context_id,
                    "reason": ReasonCode.NORMAL_MONITORING.value,
                }
            )
            try:
                self._save_runtime_snapshot()
                await self._store.async_save()
            except Exception:
                self._faulted = old_faulted
                self._recovery_blocked = old_recovery_blocked
                self._fault_reasons = old_fault_reasons
                self._fault_state_dirty = old_fault_state_dirty
                self._safety_fault_reason = old_fault_reason
                self._status = old_status
                self._last_action = old_last_action
                device.is_on = old_state
                device.measured_power = old_measured_power
                device.measured_power_valid = old_measured_power_valid
                device.measured_power_reason = old_measured_power_reason
                if store_snapshot is not None:
                    self._store.restore_snapshot(store_snapshot)
                self._last_action = "Quarantine clear failed; quarantine retained"
                raise
            self._fault_state_dirty = False
            dismissed = await self._dismiss_fault_notification(device_id)
            if not dismissed:
                self._fault_notifications_pending_dismissal.add(device_id)
                self._last_action = (
                    f"Quarantine cleared for {device.name}; "
                    "notification dismissal will be retried"
                )
            self._emit_event(
                "power_orchestrator.quarantine_cleared",
                {
                    "device_id": device_id,
                    "source": source,
                    "actor_id": actor_id,
                    "context_id": context_id,
                },
            )
            return True

    async def async_set_execution_mode(self, value: str, *, confirm_live: bool = False) -> None:
        """Change physical ownership boundary; live requires explicit confirmation."""
        if value not in (EXECUTION_MODE_LIVE, EXECUTION_MODE_OBSERVE):
            raise ValueError("execution mode must be live or observe")
        if value == EXECUTION_MODE_LIVE and not confirm_live:
            raise ValueError("live execution requires explicit confirmation")
        async with self._evaluation_lock:
            old_mode = self._execution_mode
            old_persisted_mode = None
            restore_execution_mode = getattr(self._store, "restore_execution_mode", None)
            if callable(restore_execution_mode):
                old_persisted_mode = restore_execution_mode()
            self._execution_mode = value
            if value == EXECUTION_MODE_LIVE and old_mode != EXECUTION_MODE_LIVE:
                self._execution_mode_reconciliation_required = True
                self._execution_mode_transition_issued_at = time.time()
                self._execution_mode_transition_generation = self._load_generation
            try:
                self._policy_engine.runtime.last_reason_code = (
                    ReasonCode.NORMAL_MONITORING
                    if value == EXECUTION_MODE_LIVE
                    else ReasonCode.OBSERVE_MODE
                )
                self._last_action = f"Execution mode changed to {value}"
                self._record_action(
                    {
                        "operation_id": f"mode-{self._operation_generation}",
                        "action": "execution_mode",
                        "result": "confirmed",
                        "mode": value,
                    }
                )
                self._save_runtime_snapshot()
                await self._store.async_save()
            except Exception:
                self._execution_mode = old_mode
                self._execution_mode_reconciliation_required = False
                self._execution_mode_transition_issued_at = None
                self._execution_mode_transition_generation = None
                clear_execution_mode = getattr(self._store, "clear_execution_mode", None)
                if callable(clear_execution_mode) and old_persisted_mode is None:
                    clear_execution_mode()
                elif callable(getattr(self._store, "set_execution_mode", None)):
                    self._store.set_execution_mode(old_persisted_mode or old_mode)
                self._last_action = "Execution mode change failed; previous mode retained"
                raise
            self._emit_event("power_orchestrator.execution_mode", {"mode": value})

    # ── Force re-evaluation ────────────────────────────────────────

    async def async_force_evaluate(self) -> None:
        """Trigger immediate serialized evaluation."""
        data = await self._async_update_data()
        self.async_set_updated_data(data)

    async def async_set_mode(self, value: str) -> None:
        """Atomically persist a mode change and re-evaluate."""
        if value not in (MODE_AUTO, MODE_OFF):
            raise ValueError(f"Unsupported mode: {value}")
        if value == MODE_AUTO and self._safety_storage_invalid:
            raise ValueError("safety storage is invalid; resolve persisted state first")
        async with self._evaluation_lock:
            self.mode = value  # type: ignore[misc]
            try:
                self._save_runtime_snapshot()
                await self._store.async_save()
            except Exception:
                # A mode that was not durably persisted must never leave the
                # in-memory controller armed for automatic physical starts.
                self._mode = MODE_OFF
                self._startup_safe = True
                self._post_arm_reconciliation_required = False
                self._arm_issued_at = None
                self._arm_load_generation = None
                set_mode = getattr(self._store, "set_mode", None)
                if callable(set_mode):
                    set_mode(MODE_OFF)
                self._last_action = "Mode persistence failed; mode forced to off"
                _LOGGER.exception("Failed to persist mode change; forcing off")
                raise
            if value == MODE_AUTO:
                self._startup_safe = False
                self._post_arm_reconciliation_required = True
                self._arm_issued_at = time.time()
                self._arm_load_generation = self._load_generation
            else:
                self._startup_safe = True
                self._post_arm_reconciliation_required = False
                self._arm_issued_at = None
                self._arm_load_generation = None
            await self._evaluate_safely()
            self.async_set_updated_data(self._build_data())

    def restore_device_runtime(
        self,
        faulted_devices: set[str] | frozenset[str] | list[str],
        recovery_blocked_devices: set[str] | frozenset[str] | list[str],
        *,
        fault_reasons: Mapping[str, str] | None = None,
        storage_invalid: bool = False,
    ) -> None:
        """Restore validated fault/quarantine sets before first refresh."""
        self._safety_storage_invalid = bool(storage_invalid)
        configured_ids = {device.device_id for device in self._model.all_devices()}
        self._faulted.update(
            device_id for device_id in faulted_devices if device_id in configured_ids
        )
        self._recovery_blocked.update(
            device_id
            for device_id in recovery_blocked_devices
            if device_id in configured_ids
        )
        self._fault_reasons = {
            device_id: reason[:160]
            for device_id, reason in (fault_reasons or {}).items()
            if device_id in configured_ids
            and isinstance(reason, str)
            and reason.strip()
        }
        for device_id in self._faulted | self._recovery_blocked:
            device = self._model.get_device(device_id)
            if device is not None:
                device.is_on = None

    def _save_runtime_snapshot(self) -> None:
        """Update the in-memory storage snapshot before an async save."""
        set_execution_mode = getattr(self._store, "set_execution_mode", None)
        if callable(set_execution_mode):
            set_execution_mode(self._execution_mode)
        self._store.save_policy_runtime(self._policy_engine)
        save_device_runtime = getattr(self._store, "save_device_runtime", None)
        if callable(save_device_runtime):
            save_device_runtime(
                self._model,
                faulted_devices=self._faulted,
                recovery_blocked_devices=self._recovery_blocked,
                fault_reasons=self._fault_reasons,
            )

    async def async_persist_runtime(self) -> None:
        """Persist pauses, typed policy state, ownership, and quarantine history."""
        self._save_runtime_snapshot()
        await self._store.async_save()

    # ── Data for entities ──────────────────────────────────────────

    def _build_data(self) -> dict[str, Any]:
        """Build data dict for entity state updates."""
        history_reader = getattr(self._store, "audit_history", None)
        history = history_reader() if callable(history_reader) else []
        if not isinstance(history, list):
            history = []
        next_restore = self._policy_engine.next_restore_target()
        return {
            "status": self._status,
            "current_load": self.current_load,
            "average_load": self.average_load,
            "available_capacity": self.available_capacity,
            "last_action": self._last_action,
            "grid_ok": self.grid_ok,
            "load_sensor_valid": self._load_sensor_valid,
            "load_sensor_reason": self._load_sensor_reason,
            "mode": self._mode,
            "execution_mode": self._execution_mode,
            "physical_commands_allowed": self.physical_commands_allowed,
            "policy_version": self._policy.policy_version,
            "policy_phase": self.policy_phase,
            "reason_code": self.reason_code,
            "thresholds": [
                {
                    "tier_id": tier.tier_id,
                    "limit_w": tier.limit_w,
                    "duration_s": tier.duration_s,
                    "reason_code": tier.reason_code.value,
                }
                for tier in self._policy.thresholds
            ],
            "recovery_target_w": self._policy.recovery_target_w,
            "recovery_start_w": self._policy.recovery_start_w,
            "recovery_low_since": self._policy_engine.runtime.recovery_low_since,
            "recovery_stabilize_until": self._policy_engine.runtime.stabilize_until,
            "recovery_ready": self.recovery_ready,
            "shed_stack": [
                {
                    "device_id": entry.device_id,
                    "operation_id": entry.operation_id,
                    "load_generation": entry.load_generation,
                    "reason_code": entry.reason_code.value,
                    "created_at": entry.created_at,
                }
                for entry in self._policy_engine.runtime.shed_stack
            ],
            "next_restore_target": next_restore.device_id if next_restore else None,
            "pending_expected_w": self._pending_start_w,
            "pending_operation_id": (
                self._pending_start.operation_id if self._pending_start is not None else None
            ),
            "last_operation_id": self._last_operation_id,
            "last_operation_result": self._last_operation_result,
            "faulted_devices": sorted(self._faulted),
            "recovery_blocked_devices": sorted(self._recovery_blocked),
            "fault_reasons": dict(sorted(self._fault_reasons.items())),
            "safety_fault_reason": self._safety_fault_reason,
            "manual_start_blocked_count": self._policy_engine.runtime.manual_start_blocked_count,
            "audit_history": history,
            "devices": [d.to_dict() for d in self._model.all_devices()],
        }