"""Bounded load-shedding coordinator for Power Orchestrator."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
import uuid
from collections import deque
from collections.abc import Mapping
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
    STATUS_GRID_LOSS,
    STATUS_LOAD_SHEDDING,
    STATUS_MONITORING,
    STATUS_OBSERVE,
    STATUS_SAFETY_BLOCKED,
)
from .policy import (
    Ownership,
    PolicyConfig,
    PolicyDecision,
    PolicyEngine,
    PolicyPhase,
    ReasonCode,
)
from .power_model import ManagedDevice, PowerModel
from .storage import RuntimeStore

_LOGGER = logging.getLogger(__name__)
_MAX_SHED_REJECTION_DETAILS = 12
_MAX_LAST_ACTION_LENGTH = 255


class PowerOrchestratorCoordinator(DataUpdateCoordinator[dict[str, Any]]):  # type: ignore[misc]
    """Evaluate load telemetry and issue bounded physical OFF commands only."""

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
        self._max_load = float(max_load)
        self._averaging_period = max(1.0, float(averaging_period))
        self._safety_reserve = max(0.0, float(safety_reserve))
        self._hysteresis = max(0.0, float(hysteresis))
        self._pause_period = max(0.0, float(pause_period))
        self._grid_loss_mode = grid_loss_mode
        self._grid_loss_sensor = grid_loss_sensor
        self._battery_threshold = battery_threshold
        self._battery_soc_sensor = battery_soc_sensor
        self._entry_id = entry_id
        self._execution_mode = (
            execution_mode
            if execution_mode in (EXECUTION_MODE_LIVE, EXECUTION_MODE_OBSERVE)
            else EXECUTION_MODE_OBSERVE
        )
        self._policy = policy or PolicyConfig.from_mapping(
            {
                "safety_reserve": self._safety_reserve,
                "hard_interlock": self._max_load,
                "thresholds": [
                    {
                        "power_limit": self._max_load,
                        "duration_s": 0,
                    }
                ],
            }
        )
        self._policy_engine = PolicyEngine(self._policy)
        self._policy_enabled = policy is not None
        self._last_policy_decision = self._policy_engine.last_decision

        self._mode = MODE_OFF
        # There is no normal physical activation path. This compatibility
        # projection remains true so old entity consumers cannot interpret a
        # restart as permission for an unseen physical action.
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
        self._external_ownership_grace = EXTERNAL_OWNERSHIP_GRACE_SECONDS
        self._last_confirmed_reported_at: dict[str, float | None] = {}
        self._last_operation_id: str | None = None
        self._last_action_id: str | None = None
        self._last_operation_result = "none"
        self._next_operation = 0

        self._faulted: set[str] = set()
        self._quarantined: set[str] = set()
        self._fault_reasons: dict[str, str] = {}
        self._fault_state_dirty = False
        self._action_journal_invalid = False
        self._journal_persistence_blocked = False
        self._safety_storage_invalid = False
        self._safety_fault_reason: str | None = None
        self._fault_notification_fingerprints: dict[str, str] = {}
        self._fault_notification_pending_fingerprints: dict[str, str] = {}
        self._fault_notification_dirty = False
        self._grid_loss_expected_off: set[str] = set()
        self._manual_override_notified: set[str] = set()
        self._shed_rejection_counts: dict[str, int] = {}
        self._shed_rejection_devices: list[dict[str, Any]] = []
        self._shed_rejection_total = 0
        self._shed_rejection_truncated = 0
        self._shed_rejection_evaluated_at: float | None = None

    @property
    def safety_storage_invalid(self) -> bool:
        """Return whether persisted safety state is invalid."""
        return self._safety_storage_invalid

    @property
    def action_journal_invalid(self) -> bool:
        """Return whether the action journal needs operator reconciliation."""
        return self._action_journal_invalid

    @property
    def execution_mode(self) -> str:
        return self._execution_mode

    @property
    def physical_commands_allowed(self) -> bool:
        return (
            self._execution_mode == EXECUTION_MODE_LIVE
            and self._mode == MODE_AUTO
            and not self._safety_storage_invalid
        )

    @property
    def emergency_commands_allowed(self) -> bool:
        """Emergency OFF may bypass planner mode, but never observe mode/storage gates."""
        return self._execution_mode == EXECUTION_MODE_LIVE and not self._safety_storage_invalid

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
    def execution_mode_is_observe(self) -> bool:
        return self._execution_mode == EXECUTION_MODE_OBSERVE

    @property
    def mode(self) -> str:
        return self._mode

    @mode.setter
    def mode(self, value: str) -> None:
        if value not in (MODE_AUTO, MODE_OFF):
            raise ValueError(f"Unsupported mode: {value}")
        if value == MODE_AUTO and self._safety_storage_invalid:
            raise ValueError("safety storage is invalid; resolve persisted state first")
        self._mode = value
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
        """Expose remaining headroom as telemetry; it never authorizes activation."""
        current = self.current_load
        average = self.average_load
        if current is None or average is None:
            return None
        return max(0.0, self._max_load - max(current, average) - self._safety_reserve)

    @property
    def grid_safety_source_configured(self) -> bool:
        if self._grid_loss_mode == GRID_LOSS_MODE_SENSOR:
            return bool(self._grid_loss_sensor)
        if self._grid_loss_mode == GRID_LOSS_MODE_THRESHOLD:
            return bool(self._battery_soc_sensor and self._battery_threshold is not None)
        return False

    @property
    def grid_safety_source_available(self) -> bool:
        """Return whether the configured safety source reports a usable state.

        Home Assistant's source entity owns semantic availability. Do
        not infer unavailability from ``last_updated``: a valid numeric zero or
        an unchanged binary state can be legitimately reported. The source
        must publish ``unavailable``/``unknown`` when it cannot vouch for its
        value.
        """
        if not self.grid_safety_source_configured:
            return False
        sensor_id = (
            self._grid_loss_sensor
            if self._grid_loss_mode == GRID_LOSS_MODE_SENSOR
            else self._battery_soc_sensor
        )
        if not isinstance(sensor_id, str):
            return False
        state = self.hass.states.get(sensor_id)
        if not self._state_is_available(state):
            return False
        if self._grid_loss_mode == GRID_LOSS_MODE_SENSOR:
            return getattr(state, "state", None) in {STATE_ON, STATE_OFF}
        unit = getattr(state, "attributes", {}).get("unit_of_measurement")
        if str(unit).strip() not in {"%", "percent"}:
            return False
        try:
            soc = float(getattr(state, "state", ""))
        except TypeError, ValueError:
            return False
        return math.isfinite(soc) and 0 <= soc <= 100

    @property
    def grid_ok(self) -> bool:
        """Return true only for a configured, available, valid safety source."""
        if not self.grid_safety_source_configured:
            return False
        if self._grid_loss_mode == GRID_LOSS_MODE_SENSOR:
            sensor_id = self._grid_loss_sensor
            if not isinstance(sensor_id, str):
                return False
            state = self.hass.states.get(sensor_id)
            if not self.grid_safety_source_available:
                return False
            return getattr(state, "state", None) == STATE_ON
        sensor_id = self._battery_soc_sensor
        if not isinstance(sensor_id, str):
            return False
        state = self.hass.states.get(sensor_id)
        if not self.grid_safety_source_available:
            return False
        unit = getattr(state, "attributes", {}).get("unit_of_measurement")
        if str(unit).strip() not in {"%", "percent"}:
            return False
        try:
            soc = float(getattr(state, "state", ""))
        except TypeError, ValueError:
            return False
        threshold = self._battery_threshold
        if threshold is None:
            return False
        return math.isfinite(soc) and 0 <= soc <= 100 and soc > float(threshold)

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
                await self._perform_emergency_all_stop()

    async def _evaluate(self) -> None:
        """Run one deterministic telemetry -> safety -> shedding cycle."""
        await self._refresh_device_states()
        load = self._read_load_sensor()
        if self._load_sensor_valid:
            self._accept_load_report()
            # Sampling an available state is independent from the generation
            # fence. ``last_reported`` only gates causal post-shed
            # reconciliation; it must not make an unchanged valid reading
            # disappear from the averaging window.
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
        else:
            self._load_samples.clear()
            self._load_sample_times.clear()

        if not self.grid_safety_source_available:
            self._status = STATUS_SAFETY_BLOCKED
            self._policy_engine.runtime.phase = PolicyPhase.FAULT
            self._policy_engine.runtime.last_reason_code = ReasonCode.TELEMETRY_INVALID
            await self._handle_grid_loss(
                reason_code=ReasonCode.TELEMETRY_INVALID,
                action_label="Safety telemetry unavailable",
            )
            return
        if not self.grid_ok:
            self._status = STATUS_GRID_LOSS
            self._policy_engine.runtime.phase = PolicyPhase.GRID_LOSS
            self._policy_engine.runtime.last_reason_code = ReasonCode.GRID_LOSS
            await self._handle_grid_loss()
            return

        self._grid_loss_expected_off.clear()
        self._manual_override_notified.clear()

        if not self._load_sensor_valid or self.current_load is None or self.average_load is None:
            self._status = STATUS_SAFETY_BLOCKED
            self._policy_engine.runtime.phase = PolicyPhase.FAULT
            self._policy_engine.runtime.last_reason_code = ReasonCode.TELEMETRY_INVALID
            self._last_action = f"Safety blocked — load sensor {self._load_sensor_reason}"
            return

        current = self.current_load
        average = self.average_load
        hard_interlock = self._policy.hard_interlock_w
        if hard_interlock is not None and current >= hard_interlock:
            decision = PolicyDecision(True, "hard_interlock", ReasonCode.HARD_INTERLOCK)
        elif self._policy_enabled:
            decision = self._policy_engine.observe_load(current, now=time.monotonic())
        else:
            decision = PolicyDecision(
                current > self._max_load or average > self._max_load,
                None,
                ReasonCode.SHED_SUSTAINED_OVERLOAD,
            )
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
            if self._policy_enabled and not self._policy_engine.can_shed_again(
                self._load_generation
            ):
                self._last_action = "Waiting for a newer aggregate report after the previous shed"
                return
            await self._perform_shedding(max(current, average), decision=decision)
            return

        self._status = STATUS_OBSERVE if self.execution_mode_is_observe else STATUS_MONITORING
        self._last_action = (
            "Observe: monitoring without physical commands"
            if self.execution_mode_is_observe
            else "Monitoring load; only load shedding is permitted"
        )

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
        """Reconcile logical actuator states and preserve external ownership briefly."""
        for device in self._model.all_devices():
            previous = self._last_observed_state.get(device.device_id, device.is_on)
            logical_state = self._logical_device_state(device)
            if device.device_id in self._quarantined:
                device.is_on = None
                if logical_state is True:
                    await self._command_off(device, emergency=True, source="quarantine")
            else:
                device.is_on = logical_state
                if (
                    previous is not True
                    and logical_state is True
                    and not (
                        not self._initial_device_reconciliation_complete
                        and previous is None
                        and device.ownership is not Ownership.UNKNOWN
                    )
                ):
                    device.ownership = Ownership.EXTERNAL
                    device.ownership_until = time.time() + self._external_ownership_grace
                    self._last_action = f"Preserving external ownership for {device.name}"
                if (
                    device.ownership is Ownership.EXTERNAL
                    and device.ownership_until is not None
                    and time.time() >= device.ownership_until
                ):
                    device.ownership = Ownership.PLANNER
                    device.ownership_until = None
            self._last_observed_state[device.device_id] = device.is_on
            self._refresh_measured_power(device)
        self._initial_device_reconciliation_complete = True

    def _refresh_measured_power(self, device: ManagedDevice) -> None:
        device.measured_power = 0.0
        device.measured_power_valid = False
        device.measured_power_reason = "not_configured"
        if not device.power_sensor_id:
            return
        state = self.hass.states.get(device.power_sensor_id)
        if state is None or not self._state_is_available(state):
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
        except TypeError, ValueError:
            device.measured_power_reason = "non_numeric"
            return
        if not math.isfinite(measured) or measured < 0:
            device.measured_power_reason = "invalid_value"
            return
        device.measured_power = measured
        device.measured_power_valid = True
        device.measured_power_reason = "ok"

    @staticmethod
    def _ordinary_shedding_power_eligible(device: ManagedDevice) -> bool:
        """Require positive measured draw before ordinary shedding.

        A configured power sensor reporting a valid zero (or only the bounded
        near-zero clear threshold) describes an already-heated/idle load. It
        must not be switched off merely because another load caused overload.
        Emergency interlocks use their separate all-stop path.
        """
        if device.power_sensor_id is None:
            return True
        return device.measured_power_valid and device.measured_power > QUARANTINE_CLEAR_MAX_POWER_W

    def _read_load_sensor(self) -> float:
        """Read an available, non-negative power value and retain its failure reason."""
        state = self.hass.states.get(self._load_sensor)
        if state is None:
            self._load_sensor_valid = False
            self._load_sensor_reason = "unavailable"
            self._load_reported_at = None
            return 0.0
        if not self._state_is_available(state):
            self._load_sensor_valid = False
            self._load_sensor_reason = "unavailable"
            self._load_reported_at = None
            return 0.0
        unit = getattr(state, "attributes", {}).get("unit_of_measurement")
        normalized_unit = str(unit).strip().lower()
        try:
            value = float(getattr(state, "state", ""))
        except TypeError, ValueError:
            value = math.nan
        if normalized_unit in {"kw", "kilowatt", "kilowatts"}:
            value *= 1000
        elif normalized_unit not in {"w", "watt", "watts"}:
            self._load_sensor_valid = False
            self._load_sensor_reason = "unsupported_unit"
            self._load_reported_at = None
            return 0.0
        if not math.isfinite(value) or value < 0:
            self._load_sensor_valid = False
            self._load_sensor_reason = "invalid_value"
            self._load_reported_at = None
            return 0.0
        self._load_sensor_valid = True
        self._load_sensor_reason = "ok"
        self._load_reported_at = self._state_reported_timestamp(state)
        return value

    def _state_reported_timestamp(self, state: Any) -> float | None:
        raw = getattr(state, "last_reported", None) if state is not None else None
        if isinstance(raw, datetime):
            if raw.tzinfo is None:
                raw = raw.replace(tzinfo=timezone.utc)
            value = raw.timestamp()
            if math.isfinite(value):
                return value
        elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
            value = float(raw)
            if math.isfinite(value):
                return value
        return None

    @staticmethod
    def _state_is_available(state: Any) -> bool:
        """Return whether Home Assistant reports a semantically usable state."""
        if state is None:
            return False
        return getattr(state, "state", None) not in {
            None,
            STATE_UNKNOWN,
            STATE_UNAVAILABLE,
        }

    def _actuator_state_on(self, entity_id: str, state: Any) -> bool | None:
        raw = getattr(state, "state", None) if state is not None else None
        if raw in {STATE_UNAVAILABLE, STATE_UNKNOWN, None}:
            return None
        domain = entity_id.split(".", 1)[0]
        if domain in {"switch", "light", "input_boolean"}:
            if raw == STATE_ON:
                return True
            if raw == STATE_OFF:
                return False
            return None
        if domain == "climate":
            return (
                False
                if raw == STATE_OFF
                else (None if raw in {STATE_UNKNOWN, STATE_UNAVAILABLE} else True)
            )
        return None

    def _logical_device_state(self, device: ManagedDevice) -> bool | None:
        states = [
            self._actuator_state_on(entity_id, self.hass.states.get(entity_id))
            for entity_id in device.control_entity_ids
        ]
        if not states or any(value is None for value in states):
            return None
        if all(states):
            return True
        if not any(states):
            return False
        return None

    def _logical_device_reported_at(self, device: ManagedDevice) -> float | None:
        timestamps = [
            self._state_reported_timestamp(self.hass.states.get(entity_id))
            for entity_id in device.control_entity_ids
        ]
        valid = [timestamp for timestamp in timestamps if timestamp is not None]
        return max(valid) if valid else None

    def _logical_device_confirmed_off(self, device: ManagedDevice) -> bool:
        return self._logical_device_state(device) is False

    async def _handle_grid_loss(
        self,
        *,
        reason_code: ReasonCode = ReasonCode.GRID_LOSS,
        action_label: str = "grid loss",
    ) -> None:
        """Attempt an emergency OFF for every non-confirmed-off logical load."""
        if self.execution_mode_is_observe:
            self._status = STATUS_OBSERVE
            self._last_action = f"Observe: {action_label} would stop optional loads"
            await self._record_observe_only_action(
                action="grid_loss_all_stop",
                reason=reason_code.value,
                source="grid_loss",
            )
            return
        failed: list[str] = []
        for device in self._model.get_sorted_devices_reversed():
            if self._logical_device_confirmed_off(device):
                continue
            if await self._command_off(device, emergency=True, source="grid_loss"):
                self._grid_loss_expected_off.add(device.device_id)
                self._pause_device(device)
            else:
                failed.append(device.name)
                self._quarantined.add(device.device_id)
                self._faulted.add(device.device_id)
                self._fault_reasons[device.device_id] = (
                    self._safety_fault_reason or ReasonCode.RELAY_READBACK_TIMEOUT.value
                )
                self._fault_state_dirty = True
        if failed:
            self._status = STATUS_SAFETY_BLOCKED
            self._last_action = f"{action_label} — OFF failed for: " + ", ".join(failed)
        else:
            self._last_action = f"{action_label} — optional loads are off"
        await self._persist_runtime_if_dirty()

    async def _perform_emergency_all_stop(self) -> None:
        """Best-effort emergency stop of every logical load."""
        for device in self._model.get_sorted_devices_reversed():
            if self._logical_device_confirmed_off(device):
                continue
            if await self._command_off(device, emergency=True, source="emergency"):
                self._pause_device(device)
            else:
                self._quarantined.add(device.device_id)
                self._faulted.add(device.device_id)
                self._fault_reasons[device.device_id] = (
                    self._safety_fault_reason or ReasonCode.RELAY_READBACK_TIMEOUT.value
                )
                self._fault_state_dirty = True
        await self._persist_runtime_if_dirty()

    def _shed_candidate_snapshot(
        self,
    ) -> tuple[list[ManagedDevice], dict[str, int]]:
        """Evaluate candidates and rejection reasons from one telemetry snapshot."""
        now = time.time()
        candidates: list[ManagedDevice] = []
        counts: dict[str, int] = {}
        details: list[dict[str, Any]] = []
        for device in self._model.get_shed_devices():
            reason: str | None = None
            if device.is_on is False:
                reason = "off"
            elif device.is_on is not True:
                reason = "state_unavailable"
            elif device.device_id in self._quarantined:
                reason = "quarantined"
            elif device.power_sensor_id is not None and not device.measured_power_valid:
                reason = f"power_{device.measured_power_reason}"
            elif (
                device.power_sensor_id is not None
                and device.measured_power <= QUARANTINE_CLEAR_MAX_POWER_W
            ):
                reason = "inactive_power"
            elif (
                device.ownership is Ownership.EXTERNAL
                and device.ownership_until is not None
                and now < device.ownership_until
            ):
                reason = "external_ownership_grace"

            if reason is None:
                candidates.append(device)
                continue
            counts[reason] = counts.get(reason, 0) + 1
            if len(details) < _MAX_SHED_REJECTION_DETAILS:
                details.append(
                    {
                        "device_id": device.device_id,
                        "name": device.name[:80],
                        "reason": reason,
                        "measured_power_w": (
                            device.measured_power if device.measured_power_valid else None
                        ),
                    }
                )

        total = sum(counts.values())
        self._shed_rejection_evaluated_at = now
        if candidates:
            self._shed_rejection_counts = {}
            self._shed_rejection_devices = []
            self._shed_rejection_total = 0
            self._shed_rejection_truncated = 0
        else:
            self._shed_rejection_counts = dict(sorted(counts.items()))
            self._shed_rejection_devices = details
            self._shed_rejection_total = total
            self._shed_rejection_truncated = max(0, total - len(details))
        return candidates, counts

    @staticmethod
    def _shed_rejection_summary(counts: Mapping[str, int]) -> str:
        """Return a bounded state-safe summary of candidate rejection reasons."""
        if not counts:
            return "no configured devices"
        summary = ", ".join(f"{reason}={count}" for reason, count in counts.items())
        return summary[:180]

    async def _perform_shedding(
        self,
        load_w: float,
        *,
        decision: PolicyDecision,
    ) -> None:
        """Switch off exactly one eligible active logical load."""
        candidates, rejection_counts = self._shed_candidate_snapshot()
        if not candidates:
            summary = self._shed_rejection_summary(rejection_counts)
            self._last_action = (
                f"Load shedding required at {load_w:.0f} W; no eligible load ({summary})"
            )[:_MAX_LAST_ACTION_LENGTH]
            self._status = STATUS_SAFETY_BLOCKED
            return
        device = candidates[0]
        reason = decision.reason_code.value
        if self.execution_mode_is_observe:
            self._status = STATUS_OBSERVE
            self._last_action = f"Observe: would switch off {device.name} ({reason})"
            await self._record_observe_only_action(
                action="shed",
                reason=reason,
                device_id=device.device_id,
                source="policy",
            )
            return
        if not await self._command_off(device, source="policy"):
            self._status = STATUS_SAFETY_BLOCKED
            self._quarantined.add(device.device_id)
            self._faulted.add(device.device_id)
            self._fault_reasons[device.device_id] = (
                self._safety_fault_reason or ReasonCode.RELAY_READBACK_TIMEOUT.value
            )
            self._fault_state_dirty = True
            self._last_action = f"Load shedding OFF failed for {device.name}"
            await self._persist_runtime_if_dirty()
            return

        self._pause_device(device)
        operation_id = self._last_operation_id or "unknown"
        if self._policy_enabled:
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
        """Wait within a fixed bound for a causal logical OFF report."""
        del operation_id
        deadline = time.monotonic() + RELAY_READBACK_TIMEOUT_SECONDS
        expected_on = expected_state != STATE_OFF
        while time.monotonic() <= deadline:
            logical = self._logical_device_state(device)
            reported_at = self._logical_device_reported_at(device)
            if logical is expected_on and reported_at is not None:
                if pre_reported_at is None or reported_at > pre_reported_at:
                    self._last_confirmed_reported_at[device.device_id] = reported_at
                    return True
                if reported_at >= command_issued_at:
                    self._last_confirmed_reported_at[device.device_id] = reported_at
                    return True
            await asyncio.sleep(RELAY_READBACK_POLL_INTERVAL_SECONDS)
        return False

    def _latch_device_fault(self, device: ManagedDevice, reason: str) -> None:
        """Make an unconfirmed physical stop durable and safety-blocked."""
        device.is_on = None
        self._quarantined.add(device.device_id)
        self._faulted.add(device.device_id)
        self._fault_reasons[device.device_id] = str(reason)[:160]
        self._fault_state_dirty = True
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
        self._record_action({**base, "phase": "dispatched", "result": "dispatched"})
        try:
            pre_reported_at = self._logical_device_reported_at(device)
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
            device.ownership = Ownership.PLANNER
            device.ownership_until = None
            self._last_operation_result = "confirmed"
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

    def _pause_device(self, device: ManagedDevice) -> None:
        device.pause_until = time.time() + self._pause_period
        device.last_turn_off_time = time.time()
        setter = getattr(self._store, "set_pause", None)
        if callable(setter):
            setter(device.device_id, device.pause_until)

    def _new_action_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    def _next_operation_id(self, device: ManagedDevice) -> int:
        del device
        self._next_operation += 1
        return self._next_operation

    def _record_action(self, event: dict[str, Any]) -> None:
        action_id = event.get("action_id")
        if not isinstance(action_id, str) or not action_id:
            return
        record = dict(event)
        record.setdefault("event_schema", EVENT_SCHEMA_VERSION)
        record.setdefault("timestamp", time.time())
        writer = getattr(self._store, "record_action", None)
        if callable(writer):
            writer(record)

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

    def _emit_event(self, event_type: str, data: dict[str, Any]) -> None:
        bus = getattr(self.hass, "bus", None)
        emitter = getattr(bus, "async_fire", None)
        event = {
            **data,
            "schema_version": EVENT_SCHEMA_VERSION,
            "event_type": event_type,
            "entry_id": self._entry_id,
            "execution_mode": self._execution_mode,
            "mode": self._mode,
        }
        if callable(emitter):
            try:
                emitter(event_type, event)
            except Exception:  # pragma: no cover - event delivery is non-safety-critical
                _LOGGER.debug("Power Orchestrator event delivery failed", exc_info=True)

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
            stopped = await self._command_off(
                device,
                action_id=self._new_action_id("intent"),
                source=source,
                actor_id=actor_id,
                context_id=context_id,
            )
            if stopped:
                self._pause_device(device)
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
            if device_id not in self._quarantined and device_id not in self._faulted:
                return False
            if self._logical_device_state(device) is not False:
                return False
            load = self._read_load_sensor()
            if not self._load_sensor_valid or load > self._max_load:
                return False
            if device.power_sensor_id:
                if (
                    not device.measured_power_valid
                    or device.measured_power > QUARANTINE_CLEAR_MAX_POWER_W
                ):
                    return False
            self._quarantined.discard(device_id)
            self._faulted.discard(device_id)
            self._fault_reasons.pop(device_id, None)
            self._fault_state_dirty = True
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
                self._quarantined.add(device_id)
                self._faulted.add(device_id)
                self._fault_state_dirty = True
                raise
            return True

    async def async_set_execution_mode(self, value: str, *, confirm_live: bool = False) -> None:
        """Change observe/live physical execution policy with persistence."""
        if value not in (EXECUTION_MODE_LIVE, EXECUTION_MODE_OBSERVE):
            raise ValueError("unsupported execution mode")
        if value == EXECUTION_MODE_LIVE and not confirm_live:
            raise ValueError("live execution requires explicit confirmation")
        async with self._evaluation_lock:
            previous = self._execution_mode
            self._execution_mode = value
            setter = getattr(self._store, "set_execution_mode", None)
            if callable(setter):
                setter(value)
            try:
                self._save_runtime_snapshot()
                await self._store.async_save()
            except Exception:
                self._execution_mode = previous
                if callable(setter):
                    setter(previous)
                raise
        await self._evaluate_safely()
        self.async_set_updated_data(self._build_data())

    async def async_set_mode(self, value: str) -> None:
        """Persist auto/off across restart; off never disables emergency safety."""
        if value not in (MODE_AUTO, MODE_OFF):
            raise ValueError(f"Unsupported mode: {value}")
        async with self._evaluation_lock:
            try:
                self.mode = value
                self._save_runtime_snapshot()
                await self._store.async_save()
            except Exception:
                self._mode = MODE_OFF
                setter = getattr(self._store, "set_mode", None)
                if callable(setter):
                    setter(MODE_OFF)
                self._last_action = "Mode persistence failed; mode forced to off"
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
                self._quarantined.add(device_id)
                self._faulted.add(device_id)
                self._fault_reasons[device_id] = ReasonCode.PERSISTED_RUNTIME_INVALID.value
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
        self._faulted.update(device_id for device_id in faulted_devices if device_id in configured)
        self._quarantined.update(
            device_id for device_id in quarantined_devices if device_id in configured
        )
        self._fault_reasons = {
            device_id: reason[:160]
            for device_id, reason in (fault_reasons or {}).items()
            if device_id in configured and isinstance(reason, str) and reason.strip()
        }
        for device_id in self._faulted | self._quarantined:
            device = self._model.get_device(device_id)
            if device is not None:
                device.is_on = None

    def restore_policy_runtime(self, runtime: Any) -> None:
        """Compatibility hook for callers that restore through the store."""
        del runtime

    def _save_runtime_snapshot(self) -> None:
        setter = getattr(self._store, "set_execution_mode", None)
        if callable(setter):
            setter(self._execution_mode)
        self._store.save_policy_runtime(self._policy_engine)
        saver = getattr(self._store, "save_device_runtime", None)
        if callable(saver):
            saver(
                self._model,
                faulted_devices=self._faulted,
                quarantined_devices=self._quarantined,
                fault_reasons=self._fault_reasons,
            )
        notification_saver = getattr(self._store, "save_fault_notification_state", None)
        if callable(notification_saver):
            notification_saver(
                self._fault_notification_fingerprints,
                self._fault_notification_pending_fingerprints,
            )

    async def _persist_runtime_if_dirty(self) -> bool:
        if not (
            self._fault_state_dirty
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
        self._fault_state_dirty = False
        self._fault_notification_dirty = False
        self._journal_persistence_blocked = False
        return True

    async def _notify_faults(self) -> None:
        """Keep fault notification bookkeeping bounded and retryable."""
        if not self._faulted:
            return
        for device_id in sorted(self._faulted):
            reason = self._fault_reasons.get(device_id, ReasonCode.FAULT.value)
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
            "execution_mode": self._execution_mode,
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
            "shed_barrier_pending": self._policy_engine.runtime.pending_post_shed_generation
            is not None,
            "last_operation_id": self._last_operation_id,
            "last_operation_result": self._last_operation_result,
            "last_action_id": self._last_action_id,
            "journal_unresolved_count": len(unresolved),
            "action_journal_invalid": self._action_journal_invalid,
            "journal_persistence_blocked": self._journal_persistence_blocked,
            "faulted_devices": sorted(self._faulted),
            "quarantined_devices": sorted(self._quarantined),
            "fault_reasons": dict(sorted(self._fault_reasons.items())),
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
