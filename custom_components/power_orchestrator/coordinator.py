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
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .actuator import (
    async_issue_emergency_fallback,
    async_issue_off,
    async_issue_restore,
    capture_restore_state,
)
from .const import (
    DOMAIN,
    EVALUATION_INTERVAL,
    EVENT_ACTION,
    EVENT_DECISION,
    LOAD_TELEMETRY_MAX_AGE_SECONDS,
    MODE_AUTO,
    MODE_OBSERVE,
    MODE_OFF,
    MODES,
    NOTIFY_MANUAL_ON_PREFIX,
    NOTIFY_TELEMETRY_ID,
    QUARANTINE_CLEAR_MAX_POWER_W,
    STARTUP_TELEMETRY_GRACE_SECONDS,
    STATUS_GRID_LOSS,
    STATUS_LOAD_RESTORING,
    STATUS_LOAD_SHEDDING,
    STATUS_MONITORING,
    STATUS_OBSERVE,
    STATUS_SAFETY_BLOCKED,
    STATUS_STARTUP_WAIT,
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
from .requests import (
    MAX_INTENT_TTL_S,
    RestoreIntent,
    RestoreIntentRegistry,
    RestoreTicket,
)
from .selection import restore_candidates, shed_candidates, shed_rejection_summary
from .states import (
    actuator_state_on,
    logical_device_confirmed_off,
    logical_device_report_timestamps,
    logical_device_reported_at,
    logical_device_state,
    state_is_available,
)
from .storage import RuntimeStore
from .telemetry import SafetySource, read_load_sensor, read_load_state

_LOGGER = logging.getLogger(__name__)
_MAX_LAST_ACTION_LENGTH = 255
_TELEMETRY_NOTIFICATION_TITLE = "Power Orchestrator: telemetry unavailable"
_TELEMETRY_NOTIFICATION_MESSAGE = (
    "Telemetry fault: {reason}. Managed devices keep their current state. "
    "Monitoring and the fault recover automatically when valid telemetry returns."
)
_RECOVERABLE_TELEMETRY_REASONS = frozenset(
    {
        "safety_telemetry_unavailable",
        "load_unavailable",
        "load_unsupported_unit",
        "load_invalid_value",
        "load_stale",
    }
)


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
        self._restore_tickets: dict[str, RestoreTicket] = {}
        self._intents = RestoreIntentRegistry()
        self._stopping = False
        self._mode = MODE_OBSERVE
        self._startup_safe = True
        self._startup_telemetry_ready = False
        self._startup_telemetry_deadline = time.monotonic() + STARTUP_TELEMETRY_GRACE_SECONDS
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
        self._pending_report_fault: str | None = None
        self._pending_source_loss = False

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
        self._telemetry_fault_latched = False
        self._telemetry_fault_reason: str | None = None
        self._telemetry_emergency_handled = False
        self._telemetry_incident_recorded = False
        self._persisted_telemetry_fault: tuple[bool, str | None, bool] = (False, None, False)
        self._handled_emergency_incident: str | None = None
        self._grid_loss_expected_off: set[str] = set()
        self._grid_loss_deferred_off: set[str] = set()
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
            not self._stopping
            and self._mode == MODE_AUTO
            and not self._safety_storage_invalid
            and not self._reconfiguration_required
            and self._load_sensor_valid
            and self.grid_safety_source_available
            and self.grid_ok
            and not self._telemetry_fault_latched
            and not self._pending_source_loss
        )

    @property
    def emergency_commands_allowed(self) -> bool:
        """Emergency OFF is allowed in Auto even when safety telemetry failed."""
        return (
            not self._stopping
            and self._mode == MODE_AUTO
            and not self._safety_storage_invalid
            and not self._reconfiguration_required
        )

    @property
    def restore_commands_allowed(self) -> bool:
        """Automatic restore requires Auto and clear post-action fences."""
        return (
            self.physical_commands_allowed
            and not self._telemetry_fault_latched
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
        if (
            value == MODE_AUTO
            and self._telemetry_fault_latched
            and not self._telemetry_emergency_handled
        ):
            self._pending_report_fault = self._telemetry_fault_reason or "persisted_runtime_invalid"
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
        if self._pending_report_fault is not None:
            # A report may invalidate recovery while its cleared state is saved.
            # Reconcile that loss before publication without a second action cycle.
            async with self._evaluation_lock:
                self._ingest_load_telemetry()
                await self._handle_report_invalidations()
                await self._persist_runtime_if_dirty()
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
        if self._stopping:
            return
        if self._telemetry_notification_active and not self._telemetry_fault_latched:
            await self._dismiss_telemetry_notification()
        await self._refresh_device_states()
        self._ingest_load_telemetry()
        self._reconcile_intents()
        if await self._safety_lane_handled():
            return
        current, average = self._required_loads()
        decision, planner_disabled = self._policy_decision(current)
        self._last_policy_decision = decision
        self._emit_policy_decision(decision, current)
        if await self._shedding_lane_handled(decision, current, average):
            return
        if self.restore_commands_allowed and self._policy_engine.can_restore_again(
            self._load_generation
        ):
            if await self._perform_restore(current):
                return
        self._finish_monitoring_status(planner_disabled)

    def observe_entity_report(
        self, entity_id: str, state: State | None, previous_reported_at: float | None
    ) -> None:
        if self._stopping:
            return
        if entity_id == self._load_sensor:
            self.observe_aggregate_report(state, previous_reported_at)
        elif entity_id == self._safety_source.entity_id:
            self._observe_safety_report(state)

    def _observe_safety_report(self, state: State | None) -> None:
        if self._safety_source.ok_state(state):
            return
        self._policy_engine.reset_restore_window()
        if not self._safety_source.available_state(state):
            self._latch_report_fault("safety_telemetry_unavailable")
        else:
            self._pending_source_loss = True

    def _latch_report_fault(self, reason: str) -> None:
        if not self._startup_telemetry_ready or self._telemetry_fault_latched:
            return
        self._pending_report_fault = reason
        self._telemetry_emergency_handled = False
        self._telemetry_fault_latched = True
        self._telemetry_fault_reason = reason

    def observe_aggregate_report(
        self, state: State | None, previous_reported_at: float | None = None
    ) -> None:
        """Command-free immediate invalidation, including while evaluation awaits I/O."""
        if self._stopping:
            return
        reading = read_load_state(state)
        gap = (
            previous_reported_at is not None
            and reading.reported_at is not None
            and reading.reported_at - previous_reported_at > LOAD_TELEMETRY_MAX_AGE_SECONDS
        )
        self._policy_engine.observe_report(
            reading.value if reading.valid and not gap else math.nan, now=time.monotonic()
        )
        if gap:
            self._latch_report_fault("load_stale")
        elif not reading.valid:
            self._latch_report_fault(f"load_{reading.reason}")

    def _ingest_load_telemetry(self) -> float:
        """Read aggregate telemetry and advance causal report fences."""
        load = self._read_load_sensor()
        if not self._load_sensor_valid:
            self._load_samples.clear()
            self._load_sample_times.clear()
            self._policy_engine.reset_restore_window()
            return load
        self._accept_load_report()
        self._append_load_sample(load)
        if self._policy_engine.runtime.pending_post_shed_generation is not None:
            self._policy_engine.reconcile_shed(
                self._load_generation,
                reported_at=self._load_reported_at,
            )
        if self._policy_engine.runtime.pending_post_restore_generation is not None:
            self._policy_engine.reconcile_restore(
                self._load_generation,
                reported_at=self._load_reported_at,
            )
        return load

    async def _handle_report_invalidations(self) -> None:
        """A recovered source invalidates dwell, never queues a retrospective OFF."""
        if self._pending_source_loss:
            self._pending_source_loss = False
            self._policy_engine.reset_restore_window()
        if self._pending_report_fault is not None:
            reason = self._pending_report_fault
            self._pending_report_fault = None
            await self._handle_telemetry_fault(reason)

    async def _safety_lane_handled(self) -> bool:
        """Run fail-safe handling before any policy action."""
        await self._handle_report_invalidations()
        if self._wait_for_startup_telemetry():
            return True
        if not self.grid_safety_source_available:
            await self._handle_telemetry_fault("safety_telemetry_unavailable")
            return True
        if self._load_sensor_valid and self._telemetry_fault_latched:
            if not await self._recover_telemetry_fault():
                if self._safety_storage_invalid:
                    self._project_latched_telemetry_fault()
                else:
                    reason = self._telemetry_fault_reason or (
                        "safety_telemetry_unavailable"
                        if not self.grid_safety_source_available
                        else f"load_{self._load_sensor_reason}"
                    )
                    await self._handle_telemetry_fault(reason)
                return True
        if not self.grid_ok:
            self._status = STATUS_GRID_LOSS
            self._policy_engine.runtime.phase = PolicyPhase.GRID_LOSS
            self._policy_engine.runtime.last_reason_code = ReasonCode.GRID_LOSS
            self._policy_engine.reset_restore_window()
            await self._handle_grid_loss(incident="power_source_unavailable")
            return True
        self._grid_loss_expected_off.clear()
        self._grid_loss_deferred_off.clear()
        self._manual_override_notified.clear()
        if not self._telemetry_fault_latched:
            self._handled_emergency_incident = None
        if (
            self._load_sensor_valid
            and self.current_load is not None
            and self.average_load is not None
        ):
            return False
        await self._handle_telemetry_fault(f"load_{self._load_sensor_reason}")
        return True

    def _wait_for_startup_telemetry(self) -> bool:
        """Allow only initial missing reports a bounded, command-free startup window."""
        if self._startup_telemetry_ready or self._telemetry_fault_latched:
            return False
        if not self._safety_source.configured:
            return False
        if self._load_sensor_valid and self.grid_safety_source_available:
            self._startup_telemetry_ready = True
            return False
        if self.grid_safety_source_available and not self.grid_ok:
            return False
        if not self._load_sensor_valid and self._load_sensor_reason not in {
            "missing",
            "unknown",
            "unavailable",
            "not_sampled",
        }:
            return False
        if time.monotonic() >= self._startup_telemetry_deadline:
            return False
        self._status = STATUS_STARTUP_WAIT
        self._last_action = "Waiting for initial telemetry; no physical commands"
        self._policy_engine.reset_restore_window()
        return True

    def _project_latched_telemetry_fault(self) -> None:
        """Keep an unclassified persisted fault blocked for explicit reconciliation."""
        self._status = STATUS_SAFETY_BLOCKED
        self._safety_fault_reason = self._telemetry_fault_reason
        self._policy_engine.runtime.phase = PolicyPhase.FAULT
        self._policy_engine.runtime.last_reason_code = (
            ReasonCode.TELEMETRY_STALE
            if "stale" in (self._telemetry_fault_reason or "")
            else ReasonCode.TELEMETRY_INVALID
        )
        self._policy_engine.reset_restore_window()
        self._last_action = "Unclassified persisted fault; explicit reconciliation required"

    async def _recover_telemetry_fault(self) -> bool:
        """Clear only an identified telemetry incident; keep independent safety state."""
        reason = self._telemetry_fault_reason
        if self._safety_storage_invalid or reason not in _RECOVERABLE_TELEMETRY_REASONS:
            return False
        self._read_load_sensor()
        if not self._load_sensor_valid or not self.grid_safety_source_available:
            return False
        self._telemetry_fault_latched = False
        self._telemetry_fault_reason = None
        self._telemetry_emergency_handled = False
        self._telemetry_incident_recorded = False
        self._pending_report_fault = None
        if self._safety_fault_reason == reason:
            self._safety_fault_reason = None
        self._policy_engine.reset_restore_window()
        await self._dismiss_telemetry_notification()
        self._read_load_sensor()
        if (
            self._telemetry_fault_latched
            or not self._load_sensor_valid
            or not self.grid_safety_source_available
        ):
            return False
        self._record_action(
            {
                "action_id": self._new_action_id("telemetry_recovered"),
                "action": "telemetry_recovered",
                "reason": "telemetry_recovered",
                "source": "telemetry_fault",
                "phase": "observed",
                "result": "observed",
            }
        )
        return True

    def _emit_policy_decision(self, decision: PolicyDecision, current: float) -> None:
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

    def _required_loads(self) -> tuple[float, float]:
        assert self.current_load is not None
        assert self.average_load is not None
        return self.current_load, self.average_load

    async def _shedding_lane_handled(
        self,
        decision: PolicyDecision,
        current: float,
        average: float,
    ) -> bool:
        if not decision.triggered:
            return False
        self._status = STATUS_LOAD_SHEDDING
        if not self._policy_engine.can_shed_again():
            self._last_action = "Waiting for a newer aggregate report after the previous shed"
            return True
        await self._perform_shedding(
            max(current, average),
            decision=decision,
        )
        return True

    def _finish_monitoring_status(self, planner_disabled: bool) -> None:
        self._status = STATUS_OBSERVE if self.mode_is_observe else STATUS_MONITORING
        if planner_disabled:
            self._last_action = "Mode off; normal load shedding disabled"
        elif self.mode_is_observe:
            self._last_action = "Observe: monitoring without physical commands"
        else:
            self._last_action = "Monitoring load"

    def _policy_decision(self, current: float) -> tuple[PolicyDecision, bool]:
        if self._mode != MODE_OFF:
            return self._policy_engine.observe_load(current, now=time.monotonic()), False
        runtime = self._policy_engine.runtime
        runtime.active_tier = None
        runtime.tier_started_at = None
        runtime.tier_since.clear()
        self._policy_engine.reset_restore_window()
        runtime.phase = (
            PolicyPhase.WAITING_LOAD_RECONCILIATION
            if runtime.pending_post_shed_generation is not None
            else PolicyPhase.MONITORING
        )
        runtime.last_reason_code = ReasonCode.NORMAL_MONITORING
        runtime.decision_sequence += 1
        decision = PolicyDecision(False, None, ReasonCode.NORMAL_MONITORING)
        self._policy_engine.last_decision = decision
        return decision, True

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
            await self._refresh_device_state(device)
        self._initial_device_reconciliation_complete = True

    async def _refresh_device_state(self, device: ManagedDevice) -> None:
        previous = self._last_observed_state.get(device.device_id, device.is_on)
        logical_state = logical_device_state(self.hass, device)
        if device.device_id in self._faults.quarantined:
            # Quarantine isolates the controller; it is not an OFF command.
            device.is_on = None
        else:
            device.is_on = logical_state
            if self._manual_pending_on_observed(device, previous, logical_state):
                await self._handle_manual_on_pending(device)
        self._last_observed_state[device.device_id] = device.is_on
        self._refresh_measured_power(device)

    def _manual_pending_on_observed(
        self,
        device: ManagedDevice,
        previous: bool | None,
        logical_state: bool | None,
    ) -> bool:
        if previous is True or logical_state is not True:
            return False
        if not self._initial_device_reconciliation_complete and previous is None:
            return False
        return device.device_id in self._pending_restore

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

        grid_unsafe = not self.grid_ok and not self._battery_permits(device)
        enforced = False
        if not grid_unsafe:
            enforced = self._overload_enforced(load, now=time.monotonic())
        if grid_unsafe or enforced:
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
                    self._last_action = (
                        f"Manual ON of {device.name} re-shed under unsafe conditions"
                    )
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
        except TypeError, ValueError:
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

    async def _handle_telemetry_fault(self, reason: str) -> None:
        """Block and notify about missing evidence without changing any load."""
        if self._telemetry_fault_latched and self._telemetry_fault_reason is not None:
            reason = self._telemetry_fault_reason
        elif not self._telemetry_fault_latched:
            self._telemetry_emergency_handled = False
            self._telemetry_incident_recorded = False
        self._status = STATUS_SAFETY_BLOCKED
        self._telemetry_fault_latched = True
        self._telemetry_fault_reason = reason[:160]
        self._safety_fault_reason = reason[:160]
        reason_code = (
            ReasonCode.TELEMETRY_STALE if "stale" in reason else ReasonCode.TELEMETRY_INVALID
        )
        self._policy_engine.observe_invalid_load(reason_code, now=time.monotonic())
        self._last_action = f"Safety blocked — {reason[:120]}"
        await self._ensure_telemetry_notification(reason)
        if self._telemetry_incident_recorded:
            return
        self._telemetry_incident_recorded = True
        await self._record_observe_only_action(
            action="telemetry_blocked",
            reason=reason,
            source="telemetry_fault",
        )

    async def _handle_grid_loss(
        self,
        *,
        reason_code: ReasonCode = ReasonCode.GRID_LOSS,
        action_label: str = "grid loss",
        incident: str = "grid_loss",
    ) -> None:
        """Attempt an emergency OFF for every non-confirmed-off logical load.

        A load with a battery policy is skipped while the battery charge allows it,
        and is rechecked on every later grid-loss cycle.
        """
        retry_deferred = self._handled_emergency_incident == incident
        if retry_deferred:
            await self._enforce_battery_minimum()
            if not self._grid_loss_deferred_off:
                self._last_action = f"{action_label} — emergency action already handled"
                return
        else:
            self._grid_loss_deferred_off.clear()
        self._handled_emergency_incident = incident
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
        for device in self._model.get_shed_devices():
            if retry_deferred and device.device_id not in self._grid_loss_deferred_off:
                continue
            if logical_device_confirmed_off(self.hass, device):
                self._grid_loss_deferred_off.discard(device.device_id)
                continue
            if self._battery_policy_active(device):
                if not self._battery_permits(device) and not await self._stop_on_low_battery(
                    device
                ):
                    failed.append(device.name)
                continue
            restore_state = capture_restore_state(self.hass, device)
            self._ensure_manual_intent(device.device_id)
            if await self._command_off(device, emergency=True, source="grid_loss"):
                self._grid_loss_deferred_off.discard(device.device_id)
                self._grid_loss_expected_off.add(device.device_id)
                self._pause_device(device)
                self._create_restore_ticket(
                    device,
                    cause=reason_code.value,
                    restore_state=restore_state,
                )
            else:
                failed.append(device.name)
                if self._last_operation_result == "rejected":
                    self._grid_loss_deferred_off.add(device.device_id)
                else:
                    self._grid_loss_deferred_off.discard(device.device_id)
                    self._faults.latch(
                        device.device_id,
                        self._safety_fault_reason or ReasonCode.RELAY_READBACK_TIMEOUT.value,
                    )
        if failed:
            self._status = STATUS_SAFETY_BLOCKED
            self._last_action = f"{action_label} — OFF not confirmed for: " + ", ".join(failed)
        else:
            self._last_action = f"{action_label} — optional loads are off"
        await self._persist_runtime_if_dirty()

    def _battery_charge(self) -> float | None:
        """Return the configured battery charge in percent, or None when unusable."""
        if not self._battery_soc_sensor:
            return None
        state = self.hass.states.get(self._battery_soc_sensor)
        if not state_is_available(state):
            return None
        unit = getattr(state, "attributes", {}).get("unit_of_measurement")
        if str(unit).strip() not in {"%", "percent"}:
            return None
        try:
            charge = float(getattr(state, "state", ""))
        except TypeError, ValueError:
            return None
        return charge if math.isfinite(charge) and 0 <= charge <= 100 else None

    def _battery_policy_active(self, device: ManagedDevice) -> bool:
        """A policy needs both the per-load minimum and a configured charge sensor."""
        return device.battery_min_soc is not None and bool(self._battery_soc_sensor)

    def _battery_permits(self, device: ManagedDevice) -> bool:
        """Return whether a battery-policy load may keep running during grid loss."""
        if not self._battery_policy_active(device) or not self._load_sensor_valid:
            return False
        charge = self._battery_charge()
        minimum = device.battery_min_soc
        return charge is not None and minimum is not None and charge >= minimum

    async def _stop_on_low_battery(self, device: ManagedDevice) -> bool:
        """Stop a battery-policy load without a restore ticket; its owner resumes it."""
        if await self._command_off(device, emergency=True, source="battery_minimum"):
            self._pause_device(device)
            return True
        if self._last_operation_result == "rejected":
            return False
        self._faults.latch(
            device.device_id,
            self._safety_fault_reason or ReasonCode.RELAY_READBACK_TIMEOUT.value,
        )
        return False

    async def _enforce_battery_minimum(self) -> None:
        """During grid loss, stop battery-policy loads found on below their minimum."""
        if self._mode != MODE_AUTO:
            return
        for device in self._model.get_shed_devices():
            if (
                not self._battery_policy_active(device)
                or self._battery_permits(device)
                or self._faults.is_flagged(device.device_id)
                or logical_device_confirmed_off(self.hass, device)
            ):
                continue
            if await self._stop_on_low_battery(device):
                self._last_action = f"Grid loss — {device.name} stopped below its battery minimum"
        await self._persist_runtime_if_dirty()

    async def _perform_emergency_all_stop(self, *, cause: str = "emergency") -> None:
        """Best-effort emergency stop of every logical load."""
        for device in self._model.get_shed_devices():
            if logical_device_confirmed_off(self.hass, device):
                continue
            restore_state = capture_restore_state(self.hass, device)
            self._ensure_manual_intent(device.device_id)
            if await self._command_off(device, emergency=True, source="emergency"):
                self._pause_device(device)
                self._create_restore_ticket(device, cause=cause, restore_state=restore_state)
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
        restore_state = capture_restore_state(self.hass, device)
        self._ensure_manual_intent(device.device_id)
        if not await self._command_off(device, source="policy"):
            self._status = STATUS_SAFETY_BLOCKED
            if self._last_operation_result == "rejected":
                self._last_action = "Load shedding deferred; OFF permission changed before dispatch"
                await self._persist_runtime_if_dirty()
                return
            self._faults.latch(
                device.device_id,
                self._safety_fault_reason or ReasonCode.RELAY_READBACK_TIMEOUT.value,
            )
            self._last_action = f"Load shedding OFF failed for {device.name}"
            await self._persist_runtime_if_dirty()
            return

        self._pause_device(device)
        operation_id = self._last_operation_id or "unknown"
        self._create_restore_ticket(
            device,
            cause=reason,
            restore_state=restore_state,
            operation_id=operation_id,
        )
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
        pre_reported_by_entity: Mapping[str, float | None] | None = None,
    ) -> bool:
        """Wait within a fixed bound for a causal logical state report."""
        del operation_id
        confirmed_at = await confirm_device_state(
            self.hass,
            device,
            expected_state,
            command_issued_at=command_issued_at,
            pre_reported_at=pre_reported_at,
            pre_reported_by_entity=pre_reported_by_entity,
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

    def _off_dispatch_permitted(
        self, device: ManagedDevice, *, emergency: bool, source: str
    ) -> bool:
        """Revalidate the decision's evidence after every pre-dispatch await."""
        if not emergency:
            self._read_load_sensor()
            return self.physical_commands_allowed
        if not self.emergency_commands_allowed:
            return False
        if source == "emergency":
            # Independent evaluator/action failures keep their existing stop path.
            return True
        if not self.grid_safety_source_available or self.grid_ok:
            return False
        return source not in {
            "grid_loss",
            "battery_minimum",
            "manual_on_reshed",
        } or not self._battery_permits(device)

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
        if device.device_id in self._faults.quarantined and not emergency:
            return False
        if not self._off_dispatch_permitted(device, emergency=emergency, source=source):
            self._last_operation_result = "rejected"
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
        if not await self._persist_runtime_if_dirty():
            return False
        if not self._off_dispatch_permitted(device, emergency=emergency, source=source):
            self._last_operation_result = "rejected"
            self._record_action(
                {
                    **base,
                    "phase": "rejected",
                    "result": "rejected",
                    "reason": "off_permission_withdrawn",
                }
            )
            return False
        self._record_action({**base, "phase": "dispatched", "result": "dispatched"})
        try:
            pre_reported_at = logical_device_reported_at(self.hass, device)
            pre_reported_by_entity = logical_device_report_timestamps(self.hass, device)
            command_issued_at = time.time()
            await async_issue_off(self.hass, device.command_entity)
            confirmed = await self._confirm_device_state(
                device,
                STATE_OFF,
                operation_id=operation_id,
                command_issued_at=command_issued_at,
                pre_reported_at=pre_reported_at,
                pre_reported_by_entity=pre_reported_by_entity,
            )
            if not confirmed and emergency and device.emergency_off_entities:
                fallback_issued_at = time.time()
                fallback_previous = logical_device_report_timestamps(self.hass, device)
                await async_issue_emergency_fallback(self.hass, device.emergency_off_entities)
                confirmed = await self._confirm_device_state(
                    device,
                    STATE_OFF,
                    operation_id=operation_id,
                    command_issued_at=fallback_issued_at,
                    pre_reported_at=pre_reported_at,
                    pre_reported_by_entity=fallback_previous,
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
        candidates = restore_candidates(
            self.hass,
            self._model,
            planner_shed=self._pending_restore,
            faulted=self._faults.faulted,
            quarantined=self._faults.quarantined,
            lowest_limit_w=self._policy.lowest_limit_w,
            current_load=current_load,
        )
        return [
            device
            for device in candidates
            if (ticket := self._restore_tickets.get(device.device_id)) is None
            or ticket.cause not in _RECOVERABLE_TELEMETRY_REASONS
        ]

    async def _perform_restore(self, current_load: float) -> bool:
        """Attempt at most one automatic restore of a pending load.

        Returns whether the restore lane acted this cycle (so evaluation stops).
        """
        candidates = [
            d
            for d in self._restore_candidate_snapshot(current_load)
            if self._request_permits(d.device_id)
        ]
        if not candidates:
            self._policy_engine.reset_restore_window()
            self._policy_engine.runtime.last_reason_code = ReasonCode.RESTORE_BLOCKED_NO_CANDIDATES
            return False
        device = candidates[0]
        ticket = self._restore_tickets.get(device.device_id)
        if ticket is None:
            self._remove_pending_restore(device.device_id)
            return False
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
            outcome = "deferred" if self._last_operation_result == "rejected" else "ON failed"
            self._last_action = f"Automatic restore {outcome} for {device.name}"
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
        self._last_action = f"Automatic restore: switched on {device.name} at {current_load:.0f} W"
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
        ticket = self._restore_tickets.get(device.device_id)
        if ticket is None:
            return False
        action_id = action_id or self._new_action_id("restore")
        self._last_action_id = action_id
        if not self.restore_commands_allowed or not self._request_permits(device.device_id):
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
        current = self._read_load_sensor()
        if (
            not self._load_sensor_valid
            or current + device.expected_power > self._policy.lowest_limit_w
        ):
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
        if not await self._persist_runtime_if_dirty():
            return False
        self._record_action({**base, "phase": "dispatched", "result": "dispatched"})
        try:
            pre_reported_at = logical_device_reported_at(self.hass, device)
            pre_reported_by_entity = logical_device_report_timestamps(self.hass, device)
            command_issued_at = time.time()
            dispatched = await async_issue_restore(
                self.hass,
                device,
                ticket.restore_state,
                permitted=lambda: self._restore_dispatch_permitted(device, ticket),
            )
            if not dispatched:
                self._last_operation_result = "rejected"
                self._record_action(
                    {
                        **base,
                        "phase": "rejected",
                        "result": "rejected",
                        "reason": "restore_permission_withdrawn",
                    }
                )
                self._policy_engine.reset_restore_window()
                return False
            confirmed = await self._confirm_device_state(
                device,
                STATE_ON,
                operation_id=operation_id,
                command_issued_at=command_issued_at,
                pre_reported_at=pre_reported_at,
                pre_reported_by_entity=pre_reported_by_entity,
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
            self._record_action({**base, "phase": "failed", "result": "failed", "reason": reason})
            _LOGGER.error("Failed to switch on %s: %s", device.name, exc)
            return False

    def _restore_dispatch_permitted(self, device: ManagedDevice, ticket: RestoreTicket) -> bool:
        """Fresh synchronous permission at each adapter service boundary."""
        current = self._read_load_sensor()
        return (
            self.restore_commands_allowed
            and ticket.cause not in _RECOVERABLE_TELEMETRY_REASONS
            and self._restore_tickets.get(device.device_id) is ticket
            and not ticket.expired(time.time())
            and not self._faults.is_flagged(device.device_id)
            and not device.pause_active
            and logical_device_state(self.hass, device) is False
            and actuator_state_on(
                device.command_entity, self.hass.states.get(device.command_entity)
            )
            is False
            and self._request_permits(device.device_id)
            and current + device.expected_power < self._policy.lowest_limit_w
            and self._policy_engine.restore_window_matured(
                candidate_expected_w=device.expected_power,
                lowest_limit_w=self._policy.lowest_limit_w,
                now=time.monotonic(),
            )
        )

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
        fingerprint = hashlib.sha256(f"{self._mode}:{reason}".encode()).hexdigest()[:16]
        if (
            self._telemetry_notification_active
            and self._fault_notification_fingerprints.get(notification_id) == fingerprint
        ):
            return
        title, message = await self._telemetry_notification_text(reason)
        try:
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "notification_id": notification_id,
                    "title": title,
                    "message": message,
                },
                blocking=True,
            )
        except Exception:  # pragma: no cover - notification is non-safety-critical
            _LOGGER.debug("Unable to create telemetry notification", exc_info=True)
            return
        self._telemetry_notification_active = True
        self._fault_notification_fingerprints[notification_id] = fingerprint
        self._fault_notification_dirty = True
        try:
            has_service = getattr(self.hass.services, "has_service", lambda *_: False)
            if has_service("notify", "mobile_app_iphone_rostyslav_pro"):
                await self.hass.services.async_call(
                    "notify",
                    "mobile_app_iphone_rostyslav_pro",
                    {
                        "title": title,
                        "message": message,
                        "data": {"tag": notification_id},
                    },
                    blocking=True,
                )
        except Exception:  # pragma: no cover - notification is non-safety-critical
            _LOGGER.debug("Unable to deliver mobile telemetry notification", exc_info=True)
        try:
            self.hass.bus.async_fire(
                "jarvis_household_event",
                {
                    "version": 1,
                    "kind": "power_orchestrator_fault",
                    "source": self._load_sensor,
                    "value": reason[:120],
                    "occurred_at": time.time(),
                },
            )
        except Exception:  # pragma: no cover - notification is non-safety-critical
            _LOGGER.debug("Unable to emit telemetry notification event", exc_info=True)

    async def _telemetry_notification_text(self, reason: str) -> tuple[str, str]:
        """Use HA translations, with safe text if resource loading is unavailable."""
        translations: dict[str, str] = {}
        try:
            from homeassistant.helpers.translation import async_get_translations

            translations = await async_get_translations(
                self.hass, self.hass.config.language, "issues", {DOMAIN}
            )
            prefix = f"component.{DOMAIN}.issues.telemetry_unavailable"
            title = translations.get(f"{prefix}.title", _TELEMETRY_NOTIFICATION_TITLE)
            template = translations.get(f"{prefix}.description", _TELEMETRY_NOTIFICATION_MESSAGE)
            return title, template.format(reason=reason[:120])
        except Exception:
            _LOGGER.debug("Unable to load telemetry notification translations", exc_info=True)
        return _TELEMETRY_NOTIFICATION_TITLE, _TELEMETRY_NOTIFICATION_MESSAGE.format(
            reason=reason[:120]
        )

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
            return
        self._telemetry_notification_active = False
        self._fault_notification_fingerprints.pop(notification_id, None)
        self._fault_notification_dirty = True

    async def _ensure_manual_on_notification(self, device: ManagedDevice, outcome: str) -> None:
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

    def restore_requests(self, requests: Mapping[tuple[str, str], RestoreIntent]) -> None:
        self._intents = RestoreIntentRegistry(requests)

    def _request_permits(self, device_id: str) -> bool:
        return self._intents.permits(device_id, time.time(), self.hass.states.get)

    def _restore_intent_eligibility(self) -> tuple[tuple[str, bool], ...]:
        """Snapshot pending order and effective permissions, not lease deadlines."""
        now = time.time()
        return tuple(
            (device_id, self._intents.permits(device_id, now, self.hass.states.get))
            for device_id in self._pending_restore
        )

    def _ensure_manual_intent(self, device_id: str) -> None:
        now = time.time()
        if self._intents.active_sources(device_id, now):
            return
        self._intents.set(
            device_id,
            RestoreIntent("manual_observed", True, now + MAX_INTENT_TTL_S),
        )

    async def async_set_restore_intent(
        self,
        device_id: str,
        *,
        source: str,
        active: bool,
        expires_at: float,
        permit_entity: str | None = None,
        request_entity: str | None = None,
        request_data: dict[str, Any] | None = None,
        expected_intent: dict[str, Any] | None = None,
    ) -> None:
        """Persist restore eligibility without issuing or queuing physical actions."""
        now = time.time()
        intent = RestoreIntent(
            source,
            active,
            expires_at,
            permit_entity,
            request_entity,
            request_data,
            revision=uuid.uuid4().hex if active else None,
        )
        if active and not now < expires_at <= now + MAX_INTENT_TTL_S:
            raise ValueError("deadline must be within the next 24 hours")
        async with self._evaluation_lock:
            if self._stopping:
                raise ValueError("controller is stopping")
            if self._model.get_device(device_id) is None:
                raise ValueError("unknown device_id")
            if not self._intents.matches_snapshot(device_id, source, expected_intent):
                self.async_set_updated_data(self._build_data())
                return
            previous_eligibility = self._restore_intent_eligibility()
            if active:
                self._intents.set(device_id, intent)
            else:
                self._intents.remove(device_id, source)
            self._reconcile_intents()
            if self._restore_intent_eligibility() != previous_eligibility:
                self._policy_engine.reset_restore_window()
            self._save_runtime_snapshot()
            try:
                await self._store.async_save()
            except Exception:
                self._safety_storage_invalid = True
                raise
        self.async_set_updated_data(self._build_data())

    async def async_set_request(
        self,
        device_id: str,
        *,
        source: str,
        active: bool,
        expires_at: float,
        permit_entity: str | None,
    ) -> None:
        """Compatibility alias for the 0.6.1 service contract."""
        await self.async_set_restore_intent(
            device_id,
            source=source,
            active=active,
            expires_at=expires_at,
            permit_entity=permit_entity,
        )

    def _reconcile_intents(self) -> None:
        """Prune expired intents and orphan tickets without physical commands."""
        now = time.time()
        changed = self._intents.prune(now)
        for device_id in tuple(self._restore_tickets):
            ticket = self._restore_tickets[device_id]
            if ticket.expired(now) or not self._intents.active_sources(device_id, now):
                self._remove_pending_restore(device_id)
                changed = True
        if changed:
            self._journal_dirty = True

    async def async_cancel_restore(self, device_id: str) -> bool:
        """Explicitly cancel a ticket while leaving external device state alone."""
        async with self._evaluation_lock:
            if self._model.get_device(device_id) is None:
                raise ValueError("unknown device_id")
            existed = device_id in self._restore_tickets
            pending_changed = existed or device_id in self._pending_restore
            self._remove_pending_restore(device_id)
            if pending_changed:
                self._policy_engine.reset_restore_window()
            self._save_runtime_snapshot()
            await self._store.async_save()
        self.async_set_updated_data(self._build_data())
        return existed

    async def async_shutdown(self) -> None:
        """Fence new commands immediately, then persist without changing loads."""
        self._stopping = True
        await super().async_shutdown()
        async with self._evaluation_lock:
            await self.async_persist_runtime()

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
            restore_state = capture_restore_state(self.hass, device)
            self._ensure_manual_intent(device_id)
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
                self._create_restore_ticket(
                    device,
                    cause="requested_stop",
                    restore_state=restore_state,
                )
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

    async def async_clear_fault(self) -> bool:
        """Clear the latched telemetry fault only after fresh safe reconciliation."""
        async with self._evaluation_lock:
            load = self._read_load_sensor()
            safe = (
                self.grid_safety_source_available
                and self.grid_ok
                and self._load_sensor_valid
                and math.isfinite(load)
            )
            if not safe:
                return False
            self._telemetry_fault_latched = False
            self._telemetry_fault_reason = None
            self._telemetry_emergency_handled = False
            self._telemetry_incident_recorded = False
            self._pending_report_fault = None
            self._pending_source_loss = False
            self._policy_engine.reset_restore_window()
            self._safety_fault_reason = None
            self._handled_emergency_incident = None
            await self._dismiss_telemetry_notification()
            self._save_runtime_snapshot()
            await self._store.async_save()
        await self._evaluate_safely()
        self.async_set_updated_data(self._build_data())
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
        self.async_set_updated_data(await self._async_update_data())

    async def async_force_evaluate(self) -> None:
        """Run one serialized evaluation immediately."""
        self.async_set_updated_data(await self._async_update_data())

    def restore_telemetry_fault(
        self, latched: bool, reason: str | None, *, emergency_handled: bool = True
    ) -> None:
        """Restore legacy telemetry state without replaying its former OFF action."""
        self._persisted_telemetry_fault = (latched, reason, latched and emergency_handled)
        self._telemetry_fault_latched = latched
        self._telemetry_fault_reason = reason if latched else None
        self._telemetry_emergency_handled = latched and emergency_handled
        if latched:
            if reason in _RECOVERABLE_TELEMETRY_REASONS:
                self._pending_report_fault = reason
            else:
                self._safety_storage_invalid = True

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
        self._faults.faulted.update(
            device_id for device_id in faulted_devices if device_id in configured
        )
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
        """Discard unsafe legacy bare restore IDs during the 0.7 migration."""
        del device_ids
        self._pending_restore = []
        self._restore_tickets = {}

    def restore_restore_tickets(self, tickets: Mapping[str, RestoreTicket]) -> None:
        """Restore validated tickets in their persisted shedding order."""
        configured = {device.device_id for device in self._model.all_devices()}
        now = time.time()
        self._restore_tickets = {
            device_id: ticket
            for device_id, ticket in tickets.items()
            if device_id in configured and not ticket.expired(now)
        }
        self._pending_restore = list(self._restore_tickets)

    def _create_restore_ticket(
        self,
        device: ManagedDevice,
        *,
        cause: str,
        restore_state: Mapping[str, Any],
        operation_id: str | None = None,
    ) -> None:
        now = time.time()
        ticket = RestoreTicket(
            device_id=device.device_id,
            cause=cause,
            operation_id=operation_id or self._last_operation_id or "unknown",
            created_at=now,
            expires_at=now + MAX_INTENT_TTL_S,
            restore_state=dict(restore_state),
            intent_sources=self._intents.active_sources(device.device_id, now),
        )
        if device.device_id in self._pending_restore:
            self._pending_restore.remove(device.device_id)
        self._pending_restore.append(device.device_id)
        self._restore_tickets[device.device_id] = ticket

    def _remove_pending_restore(self, device_id: str) -> None:
        if device_id in self._pending_restore:
            self._pending_restore.remove(device_id)
        self._restore_tickets.pop(device_id, None)

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
        self._store.save_requests(self._intents.as_dict())
        self._store.save_policy_runtime(self._policy_engine)
        ticket_saver = getattr(self._store, "save_restore_tickets", None)
        if callable(ticket_saver):
            ticket_saver(self._restore_tickets)
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
        telemetry_saver = getattr(self._store, "save_telemetry_fault", None)
        if callable(telemetry_saver):
            telemetry_saver(
                self._telemetry_fault_latched,
                self._telemetry_fault_reason,
                emergency_handled=self._telemetry_emergency_handled,
            )

    async def _persist_runtime_if_dirty(self) -> bool:
        telemetry_state = (
            self._telemetry_fault_latched,
            self._telemetry_fault_reason,
            self._telemetry_emergency_handled,
        )
        if not (
            self._faults.dirty
            or self._journal_dirty
            or self._action_journal_invalid
            or self._journal_persistence_blocked
            or self._fault_notification_dirty
            or telemetry_state != self._persisted_telemetry_fault
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
        self._persisted_telemetry_fault = telemetry_state
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
            "restore_tickets": [
                self._restore_tickets[device_id].to_dict()
                for device_id in self._pending_restore
                if device_id in self._restore_tickets
            ],
            "restore_intents": [
                {"device_id": device_id, **intent.to_dict()}
                for (device_id, _), intent in sorted(self._intents.as_dict().items())
            ],
            "telemetry_fault_latched": self._telemetry_fault_latched,
            "telemetry_fault_reason": self._telemetry_fault_reason,
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
