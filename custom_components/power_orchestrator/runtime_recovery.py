"""Recover durable runtime state through one lifecycle operation.

The existing RuntimeStore owns durable validation and migration. This module
owns reconstruction order and mode commitment. It never samples telemetry,
evaluates policy, sends notifications, or dispatches physical commands.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from .const import MAX_RUNTIME_PAUSE_SECONDS, MODE_OBSERVE, MODE_OFF, MODES, NOTIFY_TELEMETRY_ID
from .policy import ReasonCode
from .requests import RestoreTicket

if TYPE_CHECKING:
    from .coordinator import PowerOrchestratorCoordinator

_LOGGER = logging.getLogger(__name__)
RECOVERABLE_TELEMETRY_REASONS = frozenset(
    {
        "safety_telemetry_unavailable",
        "load_unavailable",
        "load_unsupported_unit",
        "load_invalid_value",
        "load_stale",
    }
)


class RuntimeRecovery:
    """Concentrate reconstruction and fail-closed mode commitment at one seam."""

    def __init__(self, coordinator: PowerOrchestratorCoordinator) -> None:
        self.coordinator = coordinator

    async def async_recover(
        self, data: Mapping[str, Any], reconfiguration_required: bool = False
    ) -> None:
        """Recover a loaded store, commit mode, and leave first evaluation to setup."""
        coordinator = self.coordinator
        store = coordinator._store
        model = coordinator._model
        coordinator._reconfiguration_required = reconfiguration_required
        store.restore_pause_timestamps(model, MAX_RUNTIME_PAUSE_SECONDS)
        faulted, quarantined = store.restore_device_runtime(model)
        self.restore_device_runtime(
            faulted, quarantined,
            fault_reasons=store.restore_fault_reasons(model),
            storage_invalid=store.safety_storage_invalid,
        )
        active, pending = store.restore_fault_notification_state(
            model, telemetry_notification_id=f"{NOTIFY_TELEMETRY_ID}_{coordinator._entry_id}"
        )
        self.restore_fault_notification_state(active, pending)
        latched, reason = store.restore_telemetry_fault()
        self.restore_telemetry_fault(
            latched, reason, emergency_handled=store.restore_telemetry_emergency_handled()
        )
        self.restore_action_journal(store.unresolved_actions())
        store.restore_policy_runtime(coordinator._policy_engine, model)
        coordinator.restore_requests(store.restore_requests(model))
        self.restore_restore_tickets(store.restore_restore_tickets(model))
        # Readers can discover malformed safety data late in reconstruction.
        # Commit only after all readers have contributed to the sticky gate.
        coordinator._safety_storage_invalid |= store.safety_storage_invalid
        try:
            if coordinator._safety_storage_invalid:
                mode = MODE_OFF
            elif reconfiguration_required:
                mode = MODE_OBSERVE
            else:
                mode = store.resolve_unified_mode(data.get("execution_mode"))
                if mode not in MODES:
                    mode = MODE_OBSERVE
            coordinator.mode = mode
            coordinator._save_runtime_snapshot()
            await store.async_save()
        except Exception:
            coordinator._mode = MODE_OBSERVE
            store.set_mode(MODE_OBSERVE)
            coordinator._journal_persistence_blocked = True
            coordinator._last_action = "Mode persistence failed; defaulting to observe"
            _LOGGER.exception("Unified mode could not be persisted; defaulting to observe")

    def restore_telemetry_fault(
        self, latched: bool, reason: str | None, *, emergency_handled: bool = True
    ) -> None:
        """Restore legacy telemetry state without replaying its former OFF action."""
        self.coordinator._persisted_telemetry_fault = (latched, reason, latched and emergency_handled)
        self.coordinator._telemetry_fault_latched = latched
        self.coordinator._telemetry_fault_reason = reason if latched else None
        self.coordinator._telemetry_emergency_handled = latched and emergency_handled
        if latched:
            if reason in RECOVERABLE_TELEMETRY_REASONS:
                self.coordinator._pending_report_fault = reason
            else:
                self.coordinator._safety_storage_invalid = True

    def restore_fault_notification_state(
        self,
        sent: Mapping[str, str] | None,
        pending: Mapping[str, str] | None,
    ) -> None:
        self.coordinator._fault_notification_fingerprints = {
            str(key): str(value)[:160]
            for key, value in (sent or {}).items()
            if isinstance(key, str) and isinstance(value, str)
        }
        self.coordinator._fault_notification_pending_fingerprints = {
            str(key): str(value)[:160]
            for key, value in (pending or {}).items()
            if isinstance(key, str) and isinstance(value, str)
        }
        telemetry_id = f"{NOTIFY_TELEMETRY_ID}_{self.coordinator._entry_id}"
        self.coordinator._telemetry_notification_active = telemetry_id in self.coordinator._fault_notification_fingerprints

    def restore_action_journal(self, unresolved: list[dict[str, Any]] | None) -> None:
        """Treat unfinished physical actions as ambiguous and quarantine them."""
        if not isinstance(unresolved, list):
            self.coordinator._action_journal_invalid = True
            return
        for record in unresolved:
            if not isinstance(record, dict):
                self.coordinator._action_journal_invalid = True
                continue
            device_id = record.get("device_id")
            if isinstance(device_id, str) and self.coordinator._model.get_device(device_id) is not None:
                self.coordinator._faults.quarantined.add(device_id)
                self.coordinator._faults.faulted.add(device_id)
                self.coordinator._faults.reasons[device_id] = ReasonCode.PERSISTED_RUNTIME_INVALID.value
        self.coordinator._action_journal_invalid = bool(unresolved)

    def restore_device_runtime(
        self,
        faulted_devices: set[str] | frozenset[str] | list[str],
        quarantined_devices: set[str] | frozenset[str] | list[str],
        *,
        fault_reasons: Mapping[str, str] | None = None,
        storage_invalid: bool = False,
    ) -> None:
        """Restore validated persisted fault/quarantine sets."""
        configured = {device.device_id for device in self.coordinator._model.all_devices()}
        self.coordinator._safety_storage_invalid |= bool(storage_invalid)
        self.coordinator._faults.faulted.update(
            device_id for device_id in faulted_devices if device_id in configured
        )
        self.coordinator._faults.quarantined.update(
            device_id for device_id in quarantined_devices if device_id in configured
        )
        self.coordinator._faults.reasons = {
            device_id: reason[:160]
            for device_id, reason in (fault_reasons or {}).items()
            if device_id in configured and isinstance(reason, str) and reason.strip()
        }
        for device_id in self.coordinator._faults.faulted | self.coordinator._faults.quarantined:
            device = self.coordinator._model.get_device(device_id)
            if device is not None:
                device.is_on = None

    def restore_restore_tickets(self, tickets: Mapping[str, RestoreTicket]) -> None:
        """Restore validated tickets in their persisted shedding order."""
        configured = {device.device_id for device in self.coordinator._model.all_devices()}
        now = time.time()
        validated_tickets = {
            device_id: ticket
            for device_id, ticket in tickets.items()
            if device_id in configured and not ticket.expired(now)
        }
        self.coordinator._restore_transactions.hydrate(validated_tickets)
