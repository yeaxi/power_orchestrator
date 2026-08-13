"""Bounded load-shedding coordinator for Power Orchestrator."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DOMAIN,
    EVALUATION_INTERVAL,
    EVENT_ACTION,
    EVENT_DECISION,
    MODE_AUTO,
    MODE_OBSERVE,
    MODE_OFF,
    MODES,
    NOTIFY_MANUAL_ON_PREFIX,
    NOTIFY_TELEMETRY_ID,
    QUARANTINE_CLEAR_MAX_POWER_W,
    STATUS_GRID_LOSS,
    STATUS_LOAD_RESTORING,
    STATUS_LOAD_SHEDDING,
    STATUS_MONITORING,
    STATUS_OBSERVE,
    STATUS_SAFETY_BLOCKED,
)
from .fault_registry import FaultRegistry
from .journal import emit_event, new_action_id, record_action
from .policy import (
    PolicyConfig,
    PolicyDecision,
    PolicyEngine,
    PolicyPhase,
    ReasonCode,
)
from .power_model import ManagedDevice, PowerModel
from .readback import confirm_device_state
from .selection import restore_candidates, shed_candidates, shed_rejection_summary
from .states import (
    logical_device_confirmed_off,
    logical_device_reported_at,
    logical_device_state,
    state_is_available,
)
from .storage import RuntimeStore
from .telemetry import SafetySource, read_load_sensor

_LOGGER = logging.getLogger(__name__)
_MAX_LAST_ACTION_LENGTH = 255


@dataclass(frozen=True)
class CoordinatorConfig:
    """Bounded static configuration for the coordinator."""

    load_sensor: str
    averaging_period: float
    pause_period: float
    grid_loss_mode: str
    policy: PolicyConfig
    grid_loss_sensor: str | None = None
    battery_threshold: float | None = None
    battery_soc_sensor: str | None = None
    entry_id: str = DOMAIN


class PowerOrchestratorCoordinator(DataUpdateCoordinator[dict[str, Any]]):  # type: ignore[misc]
    """Evaluate load telemetry and issue bounded physical OFF/ON commands."""

    def __init__(
        self,
        hass: HomeAssistant,
        model: PowerModel,
        store: RuntimeStore,
        config: CoordinatorConfig,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=EVALUATION_INTERVAL),
        )
        self._model = model
        self._store = store
        self._load_sensor = config.load_sensor
        self._averaging_period = max(1.0, float(config.averaging_period))
        self._pause_period = max(0.0, float(config.pause_period))
        self._grid_loss_mode = config.grid_loss_mode
        self._grid_loss_sensor = config.grid_loss_sensor
        self._battery_threshold = config.battery_threshold
        self._battery_soc_sensor = config.battery_soc_sensor
        self._safety_source = SafetySource(
            mode=config.grid_loss_mode,
            grid_sensor=config.grid_loss_sensor,
            battery_soc_sensor=config.battery_soc_sensor,
            battery_threshold=config.battery_threshold,
        )
        self._entry_id = config.entry_id
        self._policy = config.policy
        self._policy_engine = PolicyEngine(self._policy)
        self._last_policy_decision = self._policy_engine.last_decision

        self._pending_restore: list[str] = []
        self._mode = MODE_OBSERVE
        self._startup_safe = True
        self._status = STATUS_MONITORING
        self._last_action = "Initialized"
        self._load_sensor_valid = False
        self._load_sensor_reason = "not_sampled"
        self._load_reported_at: float | None = None
        self._last_accepted_load_reported_at: float | None = None
        self._load_generation = 0
        self._load_samples: deque[float] = deque(maxlen=240)
        self._load_sample_times: deque[float] = deque(maxlen=240)
        self._evaluation_lock = asyncio.Lock()

        self._last_observed_state: dict[str, bool | None] = {}
        self._initial_device_reconciliation_complete = False
        self._last_confirmed_reported_at: dict[str, float | None] = {}
        self._last_operation_id: str | None = None
        self._last_action_id: str | None = None
        self._last_operation_result = "none"
        self._next_operation = 0

        self._faults = FaultRegistry()
        self._action_journal_invalid = False
        self._journal_dirty = False
        self._journal_persistence_blocked = False
        self._safety_storage_invalid = False
        self._safety_fault_reason: str | None = None
        self._fault_notification_fingerprints: dict[str, str] = {}
        self._fault_notification_pending_fingerprints: dict[str, str] = {}
        self._fault_notification_dirty = False
        self._telemetry_notification_active = False
        self._grid_loss_expected_off: set[str] = set()
        self._manual_override_notified: set[str] = set()
        self._shed_rejection_counts: dict[str, int] = {}
        self._shed_rejection_devices: list[dict[str, Any]] = []
        self._shed_rejection_total = 0
        self._shed_rejection_truncated = 0
        self._shed_rejection_evaluated_at: float | None = None
        self._reconfiguration_required = False

    @property
    def safety_storage_invalid(self) -> bool:
        """Return whether persisted safety state is invalid."""
        return self._safety_storage_invalid

    @property
    def action_journal_invalid(self) -> bool:
        """Return whether the action journal needs operator reconciliation."""
        return self._action_journal_invalid

    @property
    def physical_commands_allowed(self) -> bool:
        return (
            self._mode == MODE_AUTO
            and not self._safety_storage_invalid
            and not self._reconfiguration_required
            and self._load_sensor_valid
            and self.grid_safety_source_available
            and self.grid_ok
        )

    @property
    def emergency_commands_allowed(self) -> bool:
        """Emergency OFF requires valid safety telemetry and Auto mode."""
        return (
            self._mode == MODE_AUTO
            and not self._safety_storage_invalid
            and not self._reconfiguration_required
            and self.grid_safety_source_available
        )

    @property
    def restore_commands_allowed(self) -> bool:
        """Automatic restore requires Auto and clear post-action fences."""
        return (
            self.physical_commands_allowed
            and self._policy_engine.runtime.pending_post_shed_generation is None
            and self._policy_engine.runtime.pending_post_restore_generation is None
        )

    @property
    def policy_phase(self) -> str:
        return self._policy_engine.runtime.phase.value

    @property
    def reason_code(self) -> str:
        return self._policy_engine.runtime.last_reason_code.value

    @property
    def policy(self) -> PolicyConfig:
        return self._policy

    @property
    def mode_is_observe(self) -> bool:
        return self._mode == MODE_OBSERVE

    @property
    def mode(self) -> str:
        return self._mode

    @mode.setter
    def mode(self, value: str) -> None:
        if value not in MODES:
            raise ValueError(f"Unsupported mode: {value}")
        if value == MODE_AUTO and self._safety_storage_invalid:
            raise ValueError("safety storage is invalid; resolve persisted state first")
        if value == MODE_AUTO and self._reconfiguration_required:
            raise ValueError("reconfiguration required before Auto mode")
        previous = self._mode
        self._mode = value
        if previous == MODE_AUTO and value != MODE_AUTO:
            self._policy_engine.reset_restore_window()
        setter = getattr(self._store, "set_mode", None)
        if callable(setter):
            setter(value)
        self._last_action = f"Mode changed to {value}"

    @property
    def startup_safe(self) -> bool:
        """Compatibility diagnostic: ordinary physical activation is absent."""
        return self._startup_safe

    @property
    def load_sensor_valid(self) -> bool:
        return self._load_sensor_valid

    @property
    def load_sensor_reason(self) -> str:
        return self._load_sensor_reason

    @property
    def status(self) -> str:
        return self._status

    @property
    def last_action(self) -> str:
        return self._last_action

    @property
    def current_load(self) -> float | None:
        if not self._load_sensor_valid or not self._load_samples:
            return None
        return self._load_samples[-1]

    @property
    def average_load(self) -> float | None:
        if not self._load_sensor_valid or not self._load_samples:
            return None
        now = time.time()
        values = [
            value
            for value, timestamp in zip(self._load_samples, self._load_sample_times)
            if now - timestamp <= self._averaging_period
        ]
        return sum(values) / len(values) if values else None

    @property
    def available_capacity(self) -> float | None:
        """Expose lowest-tier headroom without clamping; never authorizes action alone."""
        current = self.current_load
        if current is None:
            return None
        return self._policy.lowest_limit_w - current

    @property
    def grid_safety_source_configured(self) -> bool:
        return self._safety_source.configured

    @property
    def grid_safety_source_available(self) -> bool:
        """Return whether the configured safety source reports a usable state."""
        return self._safety_source.available(self.hass)

    @property
    def grid_ok(self) -> bool:
        """Return true only for a configured, available, valid safety source."""
        return self._safety_source.ok(self.hass)

    async def _async_update_data(self) -> dict[str, Any]:
        await self._evaluate_safely()
        await self._persist_runtime_if_dirty()
        await self._notify_faults()
        return self._build_data()

    async def _evaluate_safely(self) -> None:
        """Serialize evaluation and fail closed on unexpected evaluator errors."""
        async with self._evaluation_lock:
            try:
                await self._evaluate()
            except Exception as exc:  # pragma: no cover - defensive safety boundary
                _LOGGER.exception("Power Orchestrator evaluation failed")
                self._status = STATUS_SAFETY_BLOCKED
                self._safety_fault_reason = str(exc)[:160]
                self._policy_engine.runtime.phase = PolicyPhase.FAULT
                self._policy_engine.runtime.last_reason_code = ReasonCode.FAULT
                self._policy_engine.reset_restore_window()
                if self.emergency_commands_allowed:
                    await self._perform_emergency_all_stop()

    async def _evaluate(self) -> None:
        """Run one deterministic telemetry -> safety -> shed/restore cycle."""
        await self._refresh_device_states()
        load = self._read_load_sensor()
        if self._load_sensor_valid:
            self._accept_load_report()
            self._append_load_sample(load)
            pending = self._policy_engine.runtime.pending_post_shed_generation
            if pending is not None:
                if self._policy_engine.runtime.pending_post_shed_after_reported_at is None:
                    self._policy_engine.set_post_shed_fence(
                        self._last_confirmed_reported_at.get(
                            self._policy_engine.runtime.pending_operation_id or ""
                        )
                    )
                self._policy_engine.reconcile_shed(
                    self._load_generation,
                    reported_at=self._load_reported_at,
                )
            if self._policy_engine.runtime.pending_post_restore_generation is not None:
                self._policy_engine.reconcile_restore(
                    self._load_generation,
                    reported_at=self._load_reported_at,
                )
        else:
            self._load_samples.clear()
            self._load_sample_times.clear()
            self._policy_engine.reset_restore_window()

        if not self.grid_safety_source_available:
            self._status = STATUS_SAFETY_BLOCKED
            self._policy_engine.runtime.phase = PolicyPhase.FAULT
            self._policy_engine.runtime.last_reason_code = ReasonCode.TELEMETRY_INVALID
            self._policy_engine.reset_restore_window()
            self._last_action = "Safety blocked — safety telemetry unavailable"
            await self._ensure_telemetry_notification("safety_telemetry_unavailable")
            return
        if not self.grid_ok:
            self._status = STATUS_GRID_LOSS
            self._policy_engine.runtime.phase = PolicyPhase.GRID_LOSS
            self._policy_engine.runtime.last_reason_code = ReasonCode.GRID_LOSS
            self._policy_engine.reset_restore_window()
            await self._dismiss_telemetry_notification()
            await self._handle_grid_loss()
            return

        self._grid_loss_expected_off.clear()
        self._manual_override_notified.clear()

        if not self._load_sensor_valid or self.current_load is None or self.average_load is None:
            self._status = STATUS_SAFETY_BLOCKED
            self._policy_engine.runtime.phase = PolicyPhase.FAULT
            self._policy_engine.runtime.last_reason_code = ReasonCode.TELEMETRY_INVALID
            self._policy_engine.reset_restore_window()
            self._last_action = f"Safety blocked — load sensor {self._load_sensor_reason}"
            await self._ensure_telemetry_notification(f"load_{self._load_sensor_reason}")
            return

        await self._dismiss_telemetry_notification()
        current = self.current_load
        average = self.average_load

        if self._mode == MODE_OFF:
            self._policy_engine.runtime.active_tier = None
            self._policy_engine.runtime.tier_started_at = None
            self._policy_engine.runtime.tier_since.clear()
            self._policy_engine.reset_restore_window()
            self._policy_engine.runtime.phase = (
                PolicyPhase.WAITING_LOAD_RECONCILIATION
                if self._policy_engine.runtime.pending_post_shed_generation is not None
                else PolicyPhase.MONITORING
            )
            self._policy_engine.runtime.last_reason_code = ReasonCode.NORMAL_MONITORING
            self._policy_engine.runtime.decision_sequence += 1
            decision = PolicyDecision(False, None, ReasonCode.NORMAL_MONITORING)
            self._policy_engine.last_decision = decision
            planner_disabled = True
        else:
            decision = self._policy_engine.observe_load(current, now=time.monotonic())
            planner_disabled = False
        self._last_policy_decision = decision
        self._emit_event(
            EVENT_DECISION,
            {
                "phase": self.policy_phase,
                "reason_code": self.reason_code,
                "current_load": current,
                "load_generation": self._load_generation,
                "triggered": decision.triggered,
            },
        )

        if decision.triggered:
            self._status = STATUS_LOAD_SHEDDING
            if not self._policy_engine.can_shed_again(self._load_generation):
                self._last_action = "Waiting for a newer aggregate report after the previous shed"
                return
            await self._perform_shedding(max(current, average), decision=decision)
            return

        if self.restore_commands_allowed and self._policy_engine.can_restore_again(
            self._load_generation
        ):
            if await self._perform_restore(current):
                return

        self._status = STATUS_OBSERVE if self.mode_is_observe else STATUS_MONITORING
        if planner_disabled:
            self._last_action = "Mode off; normal load shedding disabled"
        elif self.mode_is_observe:
            self._last_action = "Observe: monitoring without physical commands"
        else:
            self._last_action = "Monitoring load"

    def _accept_load_report(self) -> bool:
        """Accept only a newly reported state that advances the aggregate generation."""
        if self._load_reported_at is None:
            return False
        if self._last_accepted_load_reported_at is None:
            accepted = True
        else:
            accepted = self._load_reported_at > self._last_accepted_load_reported_at
        if accepted:
            self._last_accepted_load_reported_at = self._load_reported_at
            self._load_generation += 1
        return accepted

    def _append_load_sample(self, load: float) -> None:
        if not math.isfinite(load) or load < 0:
            return
        now = time.time()
        self._load_samples.append(load)
        self._load_sample_times.append(now)
        while self._load_sample_times and now - self._load_sample_times[0] > self._averaging_period:
            self._load_sample_times.popleft()
            self._load_samples.popleft()

    async def _refresh_device_states(self) -> None:
        """Reconcile logical actuator states and handle manual ON of pending loads."""
        for device in self._model.all_devices():
            previous = self._last_observed_state.get(device.device_id, device.is_on)
            logical_state = logical_device_state(self.hass, device)
            if device.device_id in self._faults.quarantined:
                device.is_on = None
                self._remove_pending_restore(device.device_id)
                if logical_state is True:
                    await self._command_off(device, emergency=True, source="quarantine")
            else:
                device.is_on = logical_state
                if previous is not True and logical_state is True:
                    # Skip first startup None->on so restart reconciliation is not
                    # treated as a manual ON of a pending device.
                    if self._initial_device_reconciliation_complete or previous is not None:
                        if device.device_id in self._pending_restore:
                            await self._handle_manual_on_pending(device)
            self._last_observed_state[device.device_id] = device.is_on
            self._refresh_measured_power(device)
        self._initial_device_reconciliation_complete = True

    async def _handle_manual_on_pending(self, device: ManagedDevice) -> None:
        """Journal a manual ON, update its notification, then accept or re-shed."""
        action_id = self._new_action_id("manual_on")
        self._record_action(
            {
                "action_id": action_id,
                "device_id": device.device_id,
                "action": "manual_on",
                "result": "observed",
                "phase": "observed",
                "reason": "manual_on_pending",
                "source": "external",
            }
        )

        load = self._read_load_sensor()
        telemetry_invalid = not self.grid_safety_source_available or not self._load_sensor_valid
        if telemetry_invalid:
            self._append_pending_restore(device.device_id)
            self._policy_engine.reset_restore_window()
            self._last_action = (
                f"Manual ON of {device.name}; telemetry invalid, kept pending without action"
            )
            await self._ensure_telemetry_notification("manual_on_telemetry_invalid")
            await self._ensure_manual_on_notification(
                device, "Telemetry is invalid. The device remains on and pending restore."
            )
            await self._persist_runtime_if_dirty()
            return

        grid_unsafe = not self.grid_ok
        enforced = False
        if not grid_unsafe:
            enforced = self._overload_enforced(load, now=time.monotonic())
        if grid_unsafe or enforced:
            self._append_pending_restore(device.device_id)
            commands_allowed = (
                self.emergency_commands_allowed if grid_unsafe else self.physical_commands_allowed
            )
            if commands_allowed:
                if await self._command_off(
                    device,
                    emergency=grid_unsafe,
                    source="manual_on_reshed",
                ):
                    self._pause_device(device)
                    self._append_pending_restore(device.device_id)
                    self._policy_engine.reset_restore_window()
                    self._record_action(
                        {
                            "action_id": action_id,
                            "device_id": device.device_id,
                            "action": "manual_on",
                            "result": "re_shed",
                            "phase": "confirmed",
                            "reason": ReasonCode.MANUAL_ON_RESHED.value,
                            "source": "manual_on_reshed",
                        }
                    )
                    self._emit_event(
                        EVENT_ACTION,
                        {
                            "action_id": action_id,
                            "device_id": device.device_id,
                            "action": "manual_on",
                            "result": "re_shed",
                            "reason_code": ReasonCode.MANUAL_ON_RESHED.value,
                        },
                    )
                    self._last_action = f"Manual ON of {device.name} re-shed under unsafe conditions"
                    await self._ensure_manual_on_notification(
                        device, "Unsafe conditions remain, so the device was turned off again."
                    )
                else:
                    self._last_action = f"Manual ON of {device.name}; re-shed failed"
                    await self._ensure_manual_on_notification(
                        device, "Unsafe conditions remain, but turning the device off failed."
                    )
            else:
                self._last_action = f"Manual ON of {device.name}; kept pending (no physical mode)"
                await self._ensure_manual_on_notification(
                    device,
                    "Unsafe conditions remain. The current mode prevents physical action.",
                )
            await self._persist_runtime_if_dirty()
            return

        self._remove_pending_restore(device.device_id)
        self._record_action(
            {
                "action_id": action_id,
                "device_id": device.device_id,
                "action": "manual_on",
                "result": "accepted",
                "phase": "confirmed",
                "reason": ReasonCode.MANUAL_ON_ACCEPTED.value,
                "source": "external",
            }
        )
        self._emit_event(
            EVENT_ACTION,
            {
                "action_id": action_id,
                "device_id": device.device_id,
                "action": "manual_on",
                "result": "accepted",
                "reason_code": ReasonCode.MANUAL_ON_ACCEPTED.value,
            },
        )
        self._last_action = f"Manual ON of {device.name} accepted; removed from pending restore"
        await self._ensure_manual_on_notification(
            device, "Capacity is safe. The manual start was accepted."
        )
        await self._persist_runtime_if_dirty()

    def _overload_enforced(self, load_w: float, *, now: float) -> bool:
        """Return whether any tier is currently matured or zero-dwell enforced."""
        if isinstance(load_w, bool) or not math.isfinite(load_w) or load_w < 0:
            return False
        for tier in self._policy.thresholds:
            if load_w <= tier.limit_w:
                continue
            started = self._policy_engine.runtime.tier_since.get(tier.tier_id)
            if started is None:
                if tier.duration_s <= 0:
                    return True
                continue
            if now - started >= tier.duration_s:
                return True
        return False

    def _refresh_measured_power(self, device: ManagedDevice) -> None:
        device.measured_power = 0.0
        device.measured_power_valid = False
        device.measured_power_reason = "not_configured"
        if not device.power_sensor_id:
            return
        state = self.hass.states.get(device.power_sensor_id)
        if state is None or not state_is_available(state):
            device.measured_power_reason = "unavailable"
            return
        unit = getattr(state, "attributes", {}).get("unit_of_measurement")
        if str(unit).strip().lower() not in {"w", "watt", "watts"}:
            device.measured_power_reason = "unsupported_unit"
            return
        raw = getattr(state, "state", None)
        if raw is None or isinstance(raw, bool):
            device.measured_power_reason = "non_numeric"
            return
        try:
            measured = float(raw)
        except (TypeError, ValueError):
            device.measured_power_reason = "non_numeric"
            return
        if not math.isfinite(measured) or measured < 0:
            device.measured_power_reason = "invalid_value"
            return
        device.measured_power = measured
        device.measured_power_valid = True
        device.measured_power_reason = "ok"

    def _read_load_sensor(self) -> float:
        """Read the aggregate load, retaining validity and failure reason."""
        reading = read_load_sensor(self.hass, self._load_sensor)
        self._load_sensor_valid = reading.valid
        self._load_sensor_reason = reading.reason
        self._load_reported_at = reading.reported_at
        return reading.value

    async def _handle_grid_loss(
        self,
        *,
        reason_code: ReasonCode = ReasonCode.GRID_LOSS,
        action_label: str = "grid loss",
    ) -> None:
        """Attempt an emergency OFF for every non-confirmed-off logical load."""
        if self._mode != MODE_AUTO:
            self._status = STATUS_OBSERVE if self.mode_is_observe else STATUS_GRID_LOSS
            self._last_action = (
                f"Observe: {action_label} would stop optional loads"
                if self.mode_is_observe
                else f"Mode off; {action_label} would stop optional loads"
            )
            await self._record_observe_only_action(
                action="grid_loss_all_stop",
                reason=reason_code.value,
                source="grid_loss",
            )
            return
        failed: list[str] = []
        for device in self._model.get_sorted_devices_reversed():
            if logical_device_confirmed_off(self.hass, device):
                continue
            if await self._command_off(device, emergency=True, source="grid_loss"):
                self._grid_loss_expected_off.add(device.device_id)
                self._pause_device(device)
                self._append_pending_restore(device.device_id)
            else:
                failed.append(device.name)
                self._faults.latch(
                    device.device_id,
                    self._safety_fault_reason or ReasonCode.RELAY_READBACK_TIMEOUT.value,
                )
        if failed:
            self._status = STATUS_SAFETY_BLOCKED
            self._last_action = f"{action_label} — OFF failed for: " + ", ".join(failed)
        else:
            self._last_action = f"{action_label} — optional loads are off"
        await self._persist_runtime_if_dirty()

    async def _perform_emergency_all_stop(self) -> None:
        """Best-effort emergency stop of every logical load."""
        for device in self._model.get_sorted_devices_reversed():
            if logical_device_confirmed_off(self.hass, device):
                continue
            if await self._command_off(device, emergency=True, source="emergency"):
                self._pause_device(device)
                self._append_pending_restore(device.device_id)
            else:
                self._faults.latch(
                    device.device_id,
                    self._safety_fault_reason or ReasonCode.RELAY_READBACK_TIMEOUT.value,
                )
        self._policy_engine.reset_restore_window()
        await self._persist_runtime_if_dirty()

    def _shed_candidate_snapshot(
        self,
    ) -> tuple[list[ManagedDevice], dict[str, int]]:
        """Evaluate candidates and rejection reasons from one telemetry snapshot."""
        candidates, rejections = shed_candidates(
            self._model, self._faults.quarantined, now=time.time()
        )
        self._shed_rejection_counts = rejections.counts
        self._shed_rejection_devices = rejections.devices
        self._shed_rejection_total = rejections.total
        self._shed_rejection_truncated = rejections.truncated
        self._shed_rejection_evaluated_at = rejections.evaluated_at
        return candidates, rejections.counts

    async def _perform_shedding(
        self,
        load_w: float,
        *,
        decision: PolicyDecision,
    ) -> None:
        """Switch off exactly one eligible active logical load."""
        candidates, rejection_counts = self._shed_candidate_snapshot()
        if not candidates:
            summary = shed_rejection_summary(rejection_counts)
            self._last_action = (
                f"Load shedding required at {load_w:.0f} W; no eligible load ({summary})"
            )[:_MAX_LAST_ACTION_LENGTH]
            self._status = STATUS_SAFETY_BLOCKED
            return
        device = candidates[0]
        reason = decision.reason_code.value
        if self._mode != MODE_AUTO:
            self._status = STATUS_OBSERVE if self.mode_is_observe else self._status
            self._last_action = (
                f"Observe: would switch off {device.name} ({reason})"
                if self.mode_is_observe
                else f"Mode off; would switch off {device.name} ({reason})"
            )
            await self._record_observe_only_action(
                action="shed",
                reason=reason,
                device_id=device.device_id,
                source="policy",
            )
            return
        if not await self._command_off(device, source="policy"):
            self._status = STATUS_SAFETY_BLOCKED
            self._faults.latch(
                device.device_id,
                self._safety_fault_reason or ReasonCode.RELAY_READBACK_TIMEOUT.value,
            )
            self._last_action = f"Load shedding OFF failed for {device.name}"
            await self._persist_runtime_if_dirty()
            return

        self._pause_device(device)
        self._append_pending_restore(device.device_id)
        operation_id = self._last_operation_id or "unknown"
        self._policy_engine.append_shed(
            operation_id=operation_id,
            load_generation=self._load_generation,
            reason_code=decision.reason_code,
        )
        self._policy_engine.set_post_shed_fence(
            self._last_confirmed_reported_at.get(device.device_id)
        )
        self._last_action = (
            f"Load shedding: switched off {device.name} at {load_w:.0f} W ({reason})"
        )
        self._record_action(
            {
                "action_id": self._last_action_id,
                "operation_id": operation_id,
                "device_id": device.device_id,
                "action": "turn_off",
                "result": "confirmed",
                "reason": reason,
                "load_generation": self._load_generation,
            }
        )
        self._emit_event(
            EVENT_ACTION,
            {
                "action_id": self._last_action_id,
                "operation_id": operation_id,
                "device_id": device.device_id,
                "action": "turn_off",
                "result": "confirmed",
                "reason_code": reason,
            },
        )
        await self._persist_runtime_if_dirty()

    async def _confirm_device_state(
        self,
        device: ManagedDevice,
        expected_state: str,
        *,
        operation_id: int,
        command_issued_at: float,
        pre_reported_at: float | None,
    ) -> bool:
        """Wait within a fixed bound for a causal logical state report."""
        del operation_id
        confirmed_at = await confirm_device_state(
            self.hass,
            device,
            expected_state,
            command_issued_at=command_issued_at,
            pre_reported_at=pre_reported_at,
        )
        if confirmed_at is None:
            return False
        self._last_confirmed_reported_at[device.device_id] = confirmed_at
        return True

    def _latch_device_fault(self, device: ManagedDevice, reason: str) -> None:
        """Make an unconfirmed physical stop durable and safety-blocked."""
        device.is_on = None
        self._faults.latch(device.device_id, reason)
        self._status = STATUS_SAFETY_BLOCKED

    async def _command_off(
        self,
        device: ManagedDevice,
        *,
        emergency: bool = False,
        action_id: str | None = None,
        source: str = "planner",
        actor_id: str | None = None,
        context_id: str | None = None,
    ) -> bool:
        """Issue a bounded OFF command and require causal readback."""
        action_id = action_id or self._new_action_id("stop")
        self._last_action_id = action_id
        if (emergency and not self.emergency_commands_allowed) or (
            not emergency and not self.physical_commands_allowed
        ):
            self._status = STATUS_OBSERVE
            await self._record_observe_only_action(
                action="turn_off",
                reason=ReasonCode.OBSERVE_MODE.value,
                action_id=action_id,
                device_id=device.device_id,
                source=source,
                actor_id=actor_id,
                context_id=context_id,
            )
            return False

        operation_id = self._next_operation_id(device)
        self._last_operation_id = str(operation_id)
        base = {
            "action_id": action_id,
            "operation_id": str(operation_id),
            "device_id": device.device_id,
            "action": "turn_off",
            "source": source,
            "actor_id": actor_id,
            "context_id": context_id,
            "emergency": emergency,
        }
        self._record_action({**base, "phase": "prepared", "result": "prepared"})
        await self._persist_runtime_if_dirty()
        self._record_action({**base, "phase": "dispatched", "result": "dispatched"})
        try:
            pre_reported_at = logical_device_reported_at(self.hass, device)
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
            confirmed = await self._confirm_device_state(
                device,
                STATE_OFF,
                operation_id=operation_id,
                command_issued_at=command_issued_at,
                pre_reported_at=pre_reported_at,
            )
            if not confirmed:
                self._latch_device_fault(device, ReasonCode.RELAY_READBACK_TIMEOUT.value)
                self._last_operation_result = "failed"
                self._safety_fault_reason = ReasonCode.RELAY_READBACK_TIMEOUT.value
                self._record_action(
                    {
                        **base,
                        "phase": "failed",
                        "result": "failed",
                        "reason": ReasonCode.RELAY_READBACK_TIMEOUT.value,
                    }
                )
                return False
            device.is_on = False
            self._last_operation_result = "confirmed"
            self._policy_engine.reset_restore_window()
            self._record_action(
                {
                    **base,
                    "phase": "confirmed",
                    "result": "confirmed",
                    "reason": ReasonCode.NORMAL_MONITORING.value,
                }
            )
            return True
        except Exception as exc:  # pragma: no cover - defensive command boundary
            reason = str(exc)[:160]
            self._latch_device_fault(device, reason)
            self._last_operation_result = "failed"
            self._safety_fault_reason = reason
            self._record_action(
                {**base, "phase": "failed", "result": "failed", "reason": str(exc)[:160]}
            )
            _LOGGER.error("Failed to switch off %s: %s", device.name, exc)
            return False

    def _restore_candidate_snapshot(self, current_load: float) -> list[ManagedDevice]:
        """Return pending-restore loads eligible for one automatic restore, in order."""
        return restore_candidates(
            self.hass,
            self._model,
            planner_shed=self._pending_restore,
            faulted=self._faults.faulted,
            quarantined=self._faults.quarantined,
            lowest_limit_w=self._policy.lowest_limit_w,
            current_load=current_load,
        )

    async def _perform_restore(self, current_load: float) -> bool:
        """Attempt at most one automatic restore of a pending load.

        Returns whether the restore lane acted this cycle (so evaluation stops).
        """
        candidates = self._restore_candidate_snapshot(current_load)
        if not candidates:
            self._policy_engine.reset_restore_window()
            self._policy_engine.runtime.last_reason_code = ReasonCode.RESTORE_BLOCKED_NO_CANDIDATES
            return False
        device = candidates[0]
        decision = self._policy_engine.observe_restore_safe_capacity(
            current_load,
            candidate_expected_w=float(device.expected_power),
            lowest_limit_w=self._policy.lowest_limit_w,
            now=time.monotonic(),
        )
        if not decision.triggered:
            return False
        self._status = STATUS_LOAD_RESTORING
        self._policy_engine.runtime.phase = PolicyPhase.RESTORING
        self._policy_engine.runtime.last_reason_code = ReasonCode.RESTORE_HEADROOM_AVAILABLE
        if not await self._command_on(device, source="policy"):
            self._last_action = f"Automatic restore ON failed for {device.name}"
            await self._persist_runtime_if_dirty()
            return True
        operation_id = self._last_operation_id or "unknown"
        self._remove_pending_restore(device.device_id)
        self._policy_engine.append_restore(
            operation_id=operation_id, load_generation=self._load_generation
        )
        self._policy_engine.set_post_restore_fence(
            self._last_confirmed_reported_at.get(device.device_id)
        )
        self._last_action = (
            f"Automatic restore: switched on {device.name} at {current_load:.0f} W"
        )
        self._record_action(
            {
                "action_id": self._last_action_id,
                "operation_id": operation_id,
                "device_id": device.device_id,
                "action": "turn_on",
                "result": "confirmed",
                "reason": ReasonCode.RESTORE_HEADROOM_AVAILABLE.value,
                "load_generation": self._load_generation,
            }
        )
        self._emit_event(
            EVENT_ACTION,
            {
                "action_id": self._last_action_id,
                "operation_id": operation_id,
                "device_id": device.device_id,
                "action": "turn_on",
                "result": "confirmed",
                "reason_code": ReasonCode.RESTORE_HEADROOM_AVAILABLE.value,
            },
        )
        await self._persist_runtime_if_dirty()
        return True

    async def _command_on(
        self,
        device: ManagedDevice,
        *,
        action_id: str | None = None,
        source: str = "planner",
        actor_id: str | None = None,
        context_id: str | None = None,
    ) -> bool:
        """Issue a bounded ON command and require causal readback."""
        action_id = action_id or self._new_action_id("restore")
        self._last_action_id = action_id
        if not self.restore_commands_allowed:
            self._status = STATUS_OBSERVE
            await self._record_observe_only_action(
                action="turn_on",
                reason=ReasonCode.RESTORE_OBSERVE_MODE.value,
                action_id=action_id,
                device_id=device.device_id,
                source=source,
                actor_id=actor_id,
                context_id=context_id,
            )
            return False
        operation_id = self._next_operation_id(device)
        self._last_operation_id = str(operation_id)
        base = {
            "action_id": action_id,
            "operation_id": str(operation_id),
            "device_id": device.device_id,
            "action": "turn_on",
            "source": source,
            "actor_id": actor_id,
            "context_id": context_id,
            "emergency": False,
        }
        self._record_action({**base, "phase": "prepared", "result": "prepared"})
        await self._persist_runtime_if_dirty()
        self._record_action({**base, "phase": "dispatched", "result": "dispatched"})
        try:
            pre_reported_at = logical_device_reported_at(self.hass, device)
            command_issued_at = time.time()
            for entity_id in device.control_entity_ids:
                domain = entity_id.split(".", 1)[0]
                await self.hass.services.async_call(
                    domain,
                    "turn_on",
                    {"entity_id": entity_id},
                    blocking=True,
                )
            confirmed = await self._confirm_device_state(
                device,
                STATE_ON,
                operation_id=operation_id,
                command_issued_at=command_issued_at,
                pre_reported_at=pre_reported_at,
            )
            if not confirmed:
                self._latch_device_fault(device, ReasonCode.RELAY_READBACK_TIMEOUT.value)
                self._last_operation_result = "failed"
                self._safety_fault_reason = ReasonCode.RELAY_READBACK_TIMEOUT.value
                self._record_action(
                    {
                        **base,
                        "phase": "failed",
                        "result": "failed",
                        "reason": ReasonCode.RELAY_READBACK_TIMEOUT.value,
                    }
                )
                return False
            device.is_on = True
            self._last_observed_state[device.device_id] = True
            self._last_operation_result = "confirmed"
            self._policy_engine.reset_restore_window()
            self._record_action(
                {
                    **base,
                    "phase": "confirmed",
                    "result": "confirmed",
                    "reason": ReasonCode.RESTORE_HEADROOM_AVAILABLE.value,
                }
            )
            return True
        except Exception as exc:  # pragma: no cover - defensive command boundary
            reason = str(exc)[:160]
            self._latch_device_fault(device, reason)
            self._last_operation_result = "failed"
            self._safety_fault_reason = reason
            self._record_action(
                {**base, "phase": "failed", "result": "failed", "reason": reason}
            )
            _LOGGER.error("Failed to switch on %s: %s", device.name, exc)
            return False

    def _pause_device(self, device: ManagedDevice) -> None:
        # Wall-clock (time.time), not monotonic: pause_until is persisted and
        # must remain meaningful across a Home Assistant restart. Restore dwell
        # uses monotonic instead and is never persisted.
        device.last_turn_off_time = time.time()
        if self._pause_period <= 0:
            device.pause_until = None
            clearer = getattr(self._store, "clear_pause", None)
            if callable(clearer):
                clearer(device.device_id)
            return
        device.pause_until = time.time() + self._pause_period
        setter = getattr(self._store, "set_pause", None)
        if callable(setter):
            setter(device.device_id, device.pause_until)

    def _new_action_id(self, prefix: str) -> str:
        return new_action_id(prefix)

    def _next_operation_id(self, device: ManagedDevice) -> int:
        del device
        self._next_operation += 1
        return self._next_operation

    def _record_action(self, event: dict[str, Any]) -> None:
        if record_action(self._store, event):
            self._journal_dirty = True

    async def _record_observe_only_action(
        self,
        *,
        action: str,
        reason: str,
        action_id: str | None = None,
        device_id: str | None = None,
        source: str = "planner",
        actor_id: str | None = None,
        context_id: str | None = None,
    ) -> None:
        self._record_action(
            {
                "action_id": action_id or self._new_action_id("observe"),
                "device_id": device_id,
                "action": action,
                "result": "observe_only",
                "phase": "observe_only",
                "reason": reason,
                "source": source,
                "actor_id": actor_id,
                "context_id": context_id,
            }
        )
        await self._persist_runtime_if_dirty()

    def _emit_event(self, event_type: str, data: dict[str, Any]) -> None:
        emit_event(
            self.hass,
            event_type,
            data,
            entry_id=self._entry_id,
            mode=self._mode,
        )

    async def _ensure_telemetry_notification(self, reason: str) -> None:
        """Create or refresh one deduplicated telemetry-blocked notification."""
        notification_id = f"{NOTIFY_TELEMETRY_ID}_{self._entry_id}"
        fingerprint = hashlib.sha256(reason.encode()).hexdigest()[:16]
        if (
            self._telemetry_notification_active
            and self._fault_notification_fingerprints.get(notification_id) == fingerprint
        ):
            return
        try:
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "notification_id": notification_id,
                    "title": "Power Orchestrator telemetry blocked",
                    "message": (
                        "Physical actions are blocked until aggregate load and "
                        f"safety telemetry recover ({reason[:120]})."
                    ),
                },
                blocking=True,
            )
        except Exception:  # pragma: no cover - notification is non-safety-critical
            _LOGGER.debug("Unable to create telemetry notification", exc_info=True)
            return
        self._telemetry_notification_active = True
        self._fault_notification_fingerprints[notification_id] = fingerprint
        self._fault_notification_dirty = True

    async def _dismiss_telemetry_notification(self) -> None:
        if not self._telemetry_notification_active:
            return
        notification_id = f"{NOTIFY_TELEMETRY_ID}_{self._entry_id}"
        try:
            await self.hass.services.async_call(
                "persistent_notification",
                "dismiss",
                {"notification_id": notification_id},
                blocking=True,
            )
        except Exception:  # pragma: no cover - notification is non-safety-critical
            _LOGGER.debug("Unable to dismiss telemetry notification", exc_info=True)
        self._telemetry_notification_active = False
        self._fault_notification_fingerprints.pop(notification_id, None)
        self._fault_notification_dirty = True

    async def _ensure_manual_on_notification(
        self, device: ManagedDevice, outcome: str
    ) -> None:
        notification_id = f"{NOTIFY_MANUAL_ON_PREFIX}_{self._entry_id}_{device.device_id}"
        try:
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "notification_id": notification_id,
                    "title": "Power Orchestrator manual ON",
                    "message": f"{device.name} was turned on while pending restore. {outcome}",
                },
                blocking=True,
            )
        except Exception:  # pragma: no cover - notification is non-safety-critical
            _LOGGER.debug("Unable to create manual ON notification", exc_info=True)

    async def async_request_stop(
        self,
        device_id: str,
        *,
        source: str = "service",
        actor_id: str | None = None,
        context_id: str | None = None,
    ) -> bool:
        """Guarded logical-device OFF intent."""
        async with self._evaluation_lock:
            device = self._model.get_device(device_id)
            if device is None:
                raise ValueError("unknown device_id")
            if device.is_on is not True:
                return False
            self._read_load_sensor()
            if not self.grid_safety_source_available or not self._load_sensor_valid:
                await self._ensure_telemetry_notification("stop_request_telemetry_invalid")
                return False
            emergency = not self.grid_ok
            stopped = await self._command_off(
                device,
                emergency=emergency,
                action_id=self._new_action_id("intent"),
                source=source,
                actor_id=actor_id,
                context_id=context_id,
            )
            if stopped:
                self._pause_device(device)
                self._append_pending_restore(device.device_id)
            if not await self._persist_runtime_if_dirty() and not stopped:
                raise RuntimeError("OFF intent could not be persisted")
            return stopped

    async def async_clear_quarantine(
        self,
        device_id: str,
        *,
        source: str = "service",
        actor_id: str | None = None,
        context_id: str | None = None,
    ) -> bool:
        """Clear one persisted fault only after verified OFF and safe telemetry proof."""
        async with self._evaluation_lock:
            device = self._model.get_device(device_id)
            if device is None:
                raise ValueError("unknown device_id")
            if device_id not in self._faults.quarantined and device_id not in self._faults.faulted:
                return False
            if logical_device_state(self.hass, device) is not False:
                return False
            load = self._read_load_sensor()
            if not self._load_sensor_valid or load >= self._policy.lowest_limit_w:
                return False
            if device.power_sensor_id:
                if (
                    not device.measured_power_valid
                    or device.measured_power > QUARANTINE_CLEAR_MAX_POWER_W
                ):
                    return False
            self._faults.clear(device_id)
            self._record_action(
                {
                    "action_id": self._new_action_id("clear"),
                    "device_id": device_id,
                    "action": "clear_quarantine",
                    "result": "confirmed",
                    "reason": ReasonCode.NORMAL_MONITORING.value,
                    "source": source,
                    "actor_id": actor_id,
                    "context_id": context_id,
                }
            )
            self._save_runtime_snapshot()
            try:
                await self._store.async_save()
            except Exception:
                self._faults.quarantined.add(device_id)
                self._faults.faulted.add(device_id)
                self._faults.dirty = True
                raise
            return True

    async def async_set_mode(self, value: str) -> None:
        """Persist off/observe/auto across restart; Auto alone may act physically."""
        if value not in MODES:
            raise ValueError(f"Unsupported mode: {value}")
        async with self._evaluation_lock:
            previous_mode = self._mode
            previous_restore_since = self._policy_engine.runtime.restore_since
            try:
                self.mode = value
                if value != MODE_AUTO:
                    self._policy_engine.reset_restore_window()
                self._save_runtime_snapshot()
                await self._store.async_save()
            except Exception:
                self._mode = previous_mode
                self._policy_engine.runtime.restore_since = previous_restore_since
                setter = getattr(self._store, "set_mode", None)
                if callable(setter):
                    setter(previous_mode)
                self._last_action = "Mode persistence failed; previous mode retained"
                raise
        await self._evaluate_safely()
        self.async_set_updated_data(self._build_data())

    async def async_force_evaluate(self) -> None:
        """Run one serialized evaluation immediately."""
        await self._evaluate_safely()
        self.async_set_updated_data(self._build_data())

    def restore_fault_notification_state(
        self,
        sent: Mapping[str, str] | None,
        pending: Mapping[str, str] | None,
    ) -> None:
        self._fault_notification_fingerprints = {
            str(key): str(value)[:160]
            for key, value in (sent or {}).items()
            if isinstance(key, str) and isinstance(value, str)
        }
        self._fault_notification_pending_fingerprints = {
            str(key): str(value)[:160]
            for key, value in (pending or {}).items()
            if isinstance(key, str) and isinstance(value, str)
        }
        telemetry_id = f"{NOTIFY_TELEMETRY_ID}_{self._entry_id}"
        self._telemetry_notification_active = telemetry_id in self._fault_notification_fingerprints

    def restore_action_journal(self, unresolved: list[dict[str, Any]] | None) -> None:
        """Treat unfinished physical actions as ambiguous and quarantine them."""
        if not isinstance(unresolved, list):
            self._action_journal_invalid = True
            return
        for record in unresolved:
            if not isinstance(record, dict):
                self._action_journal_invalid = True
                continue
            device_id = record.get("device_id")
            if isinstance(device_id, str) and self._model.get_device(device_id) is not None:
                self._faults.quarantined.add(device_id)
                self._faults.faulted.add(device_id)
                self._faults.reasons[device_id] = ReasonCode.PERSISTED_RUNTIME_INVALID.value
        self._action_journal_invalid = bool(unresolved)

    def restore_device_runtime(
        self,
        faulted_devices: set[str] | frozenset[str] | list[str],
        quarantined_devices: set[str] | frozenset[str] | list[str],
        *,
        fault_reasons: Mapping[str, str] | None = None,
        storage_invalid: bool = False,
    ) -> None:
        """Restore validated persisted fault/quarantine sets."""
        configured = {device.device_id for device in self._model.all_devices()}
        self._safety_storage_invalid = bool(storage_invalid)
        self._faults.faulted.update(device_id for device_id in faulted_devices if device_id in configured)
        self._faults.quarantined.update(
            device_id for device_id in quarantined_devices if device_id in configured
        )
        self._faults.reasons = {
            device_id: reason[:160]
            for device_id, reason in (fault_reasons or {}).items()
            if device_id in configured and isinstance(reason, str) and reason.strip()
        }
        for device_id in self._faults.faulted | self._faults.quarantined:
            device = self._model.get_device(device_id)
            if device is not None:
                device.is_on = None

    def restore_pending_restore(self, device_ids: list[str]) -> None:
        """Restore the validated queue in its original shed order."""
        configured = {device.device_id for device in self._model.all_devices()}
        self._pending_restore = []
        for device_id in device_ids:
            if device_id in configured and device_id not in self._pending_restore:
                self._pending_restore.append(device_id)

    def _append_pending_restore(self, device_id: str) -> None:
        if device_id in self._pending_restore:
            self._pending_restore.remove(device_id)
        self._pending_restore.append(device_id)

    def _remove_pending_restore(self, device_id: str) -> None:
        if device_id in self._pending_restore:
            self._pending_restore.remove(device_id)

    def _pending_restore_names(self) -> list[str]:
        names: list[str] = []
        for device_id in self._pending_restore:
            device = self._model.get_device(device_id)
            names.append(device.name if device is not None else device_id)
        return names

    def restore_policy_runtime(self, runtime: Any) -> None:
        """Compatibility hook for callers that restore through the store."""
        del runtime

    def _save_runtime_snapshot(self) -> None:
        setter = getattr(self._store, "set_mode", None)
        if callable(setter):
            setter(self._mode)
        self._store.save_policy_runtime(self._policy_engine)
        pending_saver = getattr(self._store, "save_pending_restore", None)
        if callable(pending_saver):
            pending_saver(self._pending_restore)
        saver = getattr(self._store, "save_device_runtime", None)
        if callable(saver):
            saver(
                self._model,
                faulted_devices=self._faults.faulted,
                quarantined_devices=self._faults.quarantined,
                fault_reasons=self._faults.reasons,
            )
        notification_saver = getattr(self._store, "save_fault_notification_state", None)
        if callable(notification_saver):
            notification_saver(
                self._fault_notification_fingerprints,
                self._fault_notification_pending_fingerprints,
            )

    async def _persist_runtime_if_dirty(self) -> bool:
        if not (
            self._faults.dirty
            or self._journal_dirty
            or self._action_journal_invalid
            or self._journal_persistence_blocked
            or self._fault_notification_dirty
        ):
            return True
        try:
            self._save_runtime_snapshot()
            await self._store.async_save()
        except Exception:
            self._journal_persistence_blocked = True
            self._status = STATUS_SAFETY_BLOCKED
            return False
        self._faults.dirty = False
        self._journal_dirty = False
        self._fault_notification_dirty = False
        self._journal_persistence_blocked = False
        return True

    async def _notify_faults(self) -> None:
        """Keep fault notification bookkeeping bounded and retryable."""
        if not self._faults.faulted:
            return
        for device_id in sorted(self._faults.faulted):
            reason = self._faults.reasons.get(device_id, ReasonCode.FAULT.value)
            fingerprint = hashlib.sha256(f"{device_id}:{reason}".encode()).hexdigest()[:32]
            if self._fault_notification_fingerprints.get(device_id) != fingerprint:
                self._fault_notification_fingerprints[device_id] = fingerprint
                self._fault_notification_dirty = True
        await self._persist_runtime_if_dirty()

    def _build_data(self) -> dict[str, Any]:
        history_reader = getattr(self._store, "audit_history", None)
        history = history_reader() if callable(history_reader) else []
        if not isinstance(history, list):
            history = []
        unresolved_reader = getattr(self._store, "unresolved_actions", None)
        unresolved = unresolved_reader() if callable(unresolved_reader) else []
        if not isinstance(unresolved, list):
            unresolved = []
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
            "physical_commands_allowed": self.physical_commands_allowed,
            "startup_safe": self._startup_safe,
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
            "lowest_limit_w": self._policy.lowest_limit_w,
            "shed_barrier_pending": self._policy_engine.runtime.pending_post_shed_generation
            is not None,
            "restore_commands_allowed": self.restore_commands_allowed,
            "restore_barrier_pending": self._policy_engine.runtime.pending_post_restore_generation
            is not None,
            "pending_restore_ids": list(self._pending_restore),
            "pending_restore_names": self._pending_restore_names(),
            "reconfiguration_required": self._reconfiguration_required,
            "last_operation_id": self._last_operation_id,
            "last_operation_result": self._last_operation_result,
            "last_action_id": self._last_action_id,
            "journal_unresolved_count": len(unresolved),
            "action_journal_invalid": self._action_journal_invalid,
            "journal_persistence_blocked": self._journal_persistence_blocked,
            "faulted_devices": sorted(self._faults.faulted),
            "quarantined_devices": sorted(self._faults.quarantined),
            "fault_reasons": dict(sorted(self._faults.reasons.items())),
            "safety_fault_reason": self._safety_fault_reason,
            "shed_rejection_counts": dict(self._shed_rejection_counts),
            "shed_rejection_devices": list(self._shed_rejection_devices),
            "shed_rejection_total": self._shed_rejection_total,
            "shed_rejection_truncated": self._shed_rejection_truncated,
            "shed_rejection_evaluated_at": self._shed_rejection_evaluated_at,
            "audit_history": history,
            "devices": [device.to_dict() for device in self._model.all_devices()],
        }

    async def async_config_entry_first_refresh(self) -> None:
        """Run one refresh and publish the initial projection."""
        await self._async_update_data()
        self.async_set_updated_data(self._build_data())

    async def async_persist_runtime(self) -> None:
        """Persist runtime state during entry unload."""
        self._save_runtime_snapshot()
        await self._store.async_save()
