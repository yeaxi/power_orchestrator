"""Runtime state persistence — survives HA restarts with fail-closed parsing."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import time
import uuid
from typing import Any, Mapping

from homeassistant.helpers.storage import Store

from .const import (
    DEVICE_RUNTIME_SCHEMA_VERSION,
    EVENT_ACTION,
    EVENT_SCHEMA_VERSION,
    EXECUTION_MODE_LIVE,
    EXECUTION_MODE_OBSERVE,
    EXTERNAL_OWNERSHIP_GRACE_SECONDS,
    MAX_RUNTIME_PAUSE_SECONDS,
    MODE_AUTO,
    MODE_OFF,
)
from .policy import (
    Ownership,
    PolicyEngine,
    PolicyPhase,
    ReasonCode,
    ShedStackEntry,
    TelemetryValidity,
)
from .power_model import PowerModel

_MAX_AUDIT_ENTRIES = 100
_MAX_ACTION_FIELD_LENGTH = 256
_MAX_FAULT_REASON_LENGTH = 160


class RuntimeStore:
    """Persist pauses, typed policy runtime, and a bounded action journal."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._data: dict[str, Any] = {}
        self._safety_storage_invalid = False

    @property
    def safety_storage_invalid(self) -> bool:
        """Return whether persisted safety runtime must remain quarantined."""
        return self._safety_storage_invalid
    async def async_load(self) -> None:
        """Load persisted state."""
        data = await self._store.async_load()
        if isinstance(data, dict):
            self._data = data
            self._data["audit_history"] = self._normalize_history(
                self._data.get("audit_history", [])
            )
        else:
            # Corrupt/non-object storage must never influence control decisions.
            self._data = {}

    async def async_save(self) -> None:
        """Persist current state atomically through HA's Store."""
        await self._store.async_save(self._data)

    def snapshot(self) -> dict[str, Any]:
        """Return a detached transaction snapshot of committed runtime data."""
        return copy.deepcopy(self._data)

    def restore_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        """Restore a previously committed runtime snapshot after save failure."""
        self._data = copy.deepcopy(dict(snapshot))

    def restore_pause_timestamps(
        self,
        model: PowerModel,
        max_pause_seconds: float = MAX_RUNTIME_PAUSE_SECONDS,
    ) -> None:
        """Restore only finite, near-future pauses for configured devices."""
        pauses = self._data.get("pause_timestamps", {})
        if not isinstance(pauses, dict):
            self._data["pause_timestamps"] = {}
            return

        now = time.time()
        try:
            max_pause = float(max_pause_seconds)
        except (TypeError, ValueError):
            max_pause = MAX_RUNTIME_PAUSE_SECONDS
        if not math.isfinite(max_pause) or max_pause < 0:
            max_pause = MAX_RUNTIME_PAUSE_SECONDS
        for device_id, timestamp in list(pauses.items()):
            valid = (
                isinstance(timestamp, (int, float))
                and not isinstance(timestamp, bool)
                and math.isfinite(timestamp)
                and now < timestamp <= now + max_pause
            )
            device = model.get_device(device_id)
            if not valid or device is None:
                pauses.pop(device_id, None)
                continue
            device.pause_until = float(timestamp)

    def set_mode(self, mode: str) -> None:
        """Persist a validated runtime mode in memory."""
        if mode in (MODE_AUTO, MODE_OFF):
            self._data["mode"] = mode

    def restore_mode(self) -> str | None:
        """Return a persisted valid mode; corrupt explicit mode fails to off."""
        if "mode" not in self._data:
            return None
        mode = self._data.get("mode")
        return mode if mode in (MODE_AUTO, MODE_OFF) else MODE_OFF

    def set_execution_mode(self, mode: str) -> None:
        """Persist only a validated physical execution boundary."""
        if mode in (EXECUTION_MODE_LIVE, EXECUTION_MODE_OBSERVE):
            self._data["execution_mode"] = mode

    def restore_execution_mode(self) -> str | None:
        """Return a persisted execution boundary or fail closed to config fallback."""
        mode = self._data.get("execution_mode")
        return mode if mode in (EXECUTION_MODE_LIVE, EXECUTION_MODE_OBSERVE) else None

    def clear_execution_mode(self) -> None:
        """Remove a transient execution-mode override after failed persistence."""
        self._data.pop("execution_mode", None)

    def update_pause_timestamp(self, device_id: str, pause_until: float | None) -> None:
        """Update one device's pause timestamp in memory."""
        if "pause_timestamps" not in self._data:
            self._data["pause_timestamps"] = {}
        if pause_until is None:
            self._data["pause_timestamps"].pop(device_id, None)
        else:
            self._data["pause_timestamps"][device_id] = pause_until

    def set_pause(self, device_id: str, duration_s: float) -> None:
        """Set a finite, non-negative pause timer for a device."""
        try:
            duration = float(duration_s)
        except (TypeError, ValueError):
            return
        if not math.isfinite(duration) or duration < 0:
            return
        pause_until = time.time() + min(duration, MAX_RUNTIME_PAUSE_SECONDS)
        self.update_pause_timestamp(device_id, pause_until)

    def clear_pause(self, device_id: str) -> None:
        self.update_pause_timestamp(device_id, None)

    def save_device_runtime(
        self,
        model: PowerModel,
        *,
        faulted_devices: set[str] | frozenset[str] | list[str] | tuple[str, ...] = (),
        recovery_blocked_devices: set[str] | frozenset[str] | list[str] | tuple[str, ...] = (),
        fault_reasons: Mapping[str, str] | None = None,
    ) -> None:
        """Persist bounded ownership and quarantine state for configured devices."""
        configured_ids = {device.device_id for device in model.all_devices()}
        records: dict[str, dict[str, Any]] = {}
        now = time.time()
        for device in model.all_devices():
            ownership = device.ownership
            ownership_until = device.ownership_until
            if ownership is Ownership.EXTERNAL:
                if (
                    not isinstance(ownership_until, (int, float))
                    or isinstance(ownership_until, bool)
                    or not math.isfinite(float(ownership_until))
                    or float(ownership_until) <= now
                    or float(ownership_until) > now + EXTERNAL_OWNERSHIP_GRACE_SECONDS
                ):
                    ownership = Ownership.PLANNER
                    ownership_until = None
            elif ownership not in {Ownership.UNKNOWN, Ownership.PLANNER, Ownership.MANUAL}:
                ownership = Ownership.UNKNOWN
                ownership_until = None
            records[device.device_id] = {
                "ownership": ownership.value,
                "ownership_until": ownership_until,
            }
        normalized_reasons = {
            device_id: reason[:_MAX_FAULT_REASON_LENGTH]
            for device_id, reason in (fault_reasons or {}).items()
            if isinstance(device_id, str)
            and device_id in configured_ids
            and isinstance(reason, str)
            and reason.strip()
        }
        self._data["device_runtime"] = {
            "schema_version": DEVICE_RUNTIME_SCHEMA_VERSION,
            "devices": records,
            "faulted_devices": sorted(
                device_id for device_id in faulted_devices if device_id in configured_ids
            ),
            "recovery_blocked_devices": sorted(
                device_id
                for device_id in recovery_blocked_devices
                if device_id in configured_ids
            ),
            "fault_reasons": normalized_reasons,
        }

    def restore_device_runtime(
        self,
        model: PowerModel,
    ) -> tuple[set[str], set[str]]:
        """Restore ownership/quarantine state and return validated device sets."""
        raw = self._data.get("device_runtime")
        configured_ids = {device.device_id for device in model.all_devices()}
        if raw is None:
            self._safety_storage_invalid = False
            return set(), set()
        if not self._device_runtime_envelope_is_valid(raw):
            self._safety_storage_invalid = True
            return set(), configured_ids
        self._safety_storage_invalid = False
        now = time.time()
        max_lease = now + EXTERNAL_OWNERSHIP_GRACE_SECONDS
        raw_devices = raw.get("devices", {})
        if isinstance(raw_devices, dict):
            for device_id, item in raw_devices.items():
                if not isinstance(device_id, str) or not isinstance(item, dict):
                    continue
                device = model.get_device(device_id)
                if device is None:
                    continue
                try:
                    ownership = Ownership(item.get("ownership", Ownership.UNKNOWN.value))
                except ValueError:
                    ownership = Ownership.UNKNOWN
                until = self._finite_or_none(item.get("ownership_until"))
                if ownership is Ownership.EXTERNAL:
                    if until is None or not now < until <= max_lease:
                        ownership = Ownership.PLANNER
                        until = None
                else:
                    until = None
                device.ownership = ownership
                device.ownership_until = until
        faulted = self._validated_device_set(raw.get("faulted_devices"), configured_ids)
        recovery_blocked = self._validated_device_set(
            raw.get("recovery_blocked_devices"), configured_ids
        )
        for device_id in faulted | recovery_blocked:
            device = model.get_device(device_id)
            if device is not None:
                device.is_on = None
        return faulted, recovery_blocked

    def restore_fault_reasons(self, model: PowerModel) -> dict[str, str]:
        """Return only bounded reasons for configured quarantined devices."""
        raw = self._data.get("device_runtime")
        if raw is None:
            self._safety_storage_invalid = False
            return {}
        configured_ids = {device.device_id for device in model.all_devices()}
        if not self._device_runtime_envelope_is_valid(raw):
            self._safety_storage_invalid = True
            return {
                device_id: ReasonCode.PERSISTED_RUNTIME_INVALID.value
                for device_id in configured_ids
            }
        reasons = raw.get("fault_reasons")
        if not isinstance(reasons, dict):
            return {}
        return {
            device_id: reason[:_MAX_FAULT_REASON_LENGTH]
            for device_id, reason in reasons.items()
            if isinstance(device_id, str)
            and device_id in configured_ids
            and isinstance(reason, str)
            and reason.strip()
        }

    @staticmethod
    def _device_runtime_envelope_is_valid(value: Any) -> bool:
        """Validate the safety envelope shape before restoring any healthy state."""
        if not isinstance(value, dict):
            return False
        schema_version = value.get("schema_version")
        if schema_version is not None and (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != DEVICE_RUNTIME_SCHEMA_VERSION
        ):
            return False
        if not isinstance(value.get("devices"), dict):
            return False
        for key in ("faulted_devices", "recovery_blocked_devices"):
            if not isinstance(value.get(key), (list, tuple, set, frozenset)):
                return False
        reasons = value.get("fault_reasons", {})
        return isinstance(reasons, dict)

    @staticmethod
    def _validated_device_set(value: Any, configured_ids: set[str]) -> set[str]:
        if not isinstance(value, (list, tuple, set, frozenset)):
            return set()
        return {
            device_id
            for device_id in value
            if isinstance(device_id, str) and device_id in configured_ids
        }

    def save_policy_runtime(self, engine: PolicyEngine) -> None:
        """Serialize only JSON-safe policy state and confirmed stack entries."""
        runtime = engine.runtime
        stack: list[dict[str, Any]] = []
        for entry in runtime.shed_stack:
            if not entry.device_id or not entry.operation_id or entry.load_generation < 0:
                continue
            stack.append(
                {
                    "device_id": entry.device_id,
                    "operation_id": entry.operation_id,
                    "pre_state": bool(entry.pre_state),
                    "snapshot": entry.snapshot,
                    "load_generation": entry.load_generation,
                    "reason_code": entry.reason_code.value,
                    "created_at": entry.created_at,
                }
            )
        self._data["policy_runtime"] = {
            "phase": runtime.phase.value,
            "active_tier": runtime.active_tier,
            "recovery_low_since": runtime.recovery_low_since,
            "stabilize_until": runtime.stabilize_until,
            "shed_stack": stack,
            "restore_target": runtime.restore_target,
            "last_shed_load_generation": runtime.last_shed_load_generation,
            "pending_post_shed_generation": runtime.pending_post_shed_generation,
            "pending_post_shed_after_reported_at": runtime.pending_post_shed_after_reported_at,
            "pending_operation_id": runtime.pending_operation_id,
            "manual_start_blocked_count": runtime.manual_start_blocked_count,
            "last_telemetry_validity": runtime.last_telemetry_validity.value,
            "last_reason_code": runtime.last_reason_code.value,
            "decision_sequence": runtime.decision_sequence,
        }

    def restore_policy_runtime(
        self,
        engine: PolicyEngine,
        model: PowerModel | None = None,
    ) -> None:
        """Restore only validated controller-owned policy state."""
        raw = self._data.get("policy_runtime")
        if not isinstance(raw, dict):
            return
        runtime = engine.runtime
        try:
            runtime.phase = PolicyPhase(raw.get("phase", PolicyPhase.STARTUP.value))
        except ValueError:
            runtime.phase = PolicyPhase.FAULT
        runtime.active_tier = raw.get("active_tier") if isinstance(raw.get("active_tier"), str) else None
        runtime.recovery_low_since = self._finite_or_none(raw.get("recovery_low_since"))
        runtime.stabilize_until = self._finite_or_none(raw.get("stabilize_until"))
        runtime.restore_target = raw.get("restore_target") if isinstance(raw.get("restore_target"), str) else None
        runtime.last_shed_load_generation = self._nonnegative_int_or_none(raw.get("last_shed_load_generation"))
        raw_pending_generation = self._nonnegative_int_or_none(
            raw.get("pending_post_shed_generation")
        )
        # Generations are process-local. A restored barrier waits for a
        # report newer than the persisted causal fence. Legacy records without
        # that fence remain fail-closed until the coordinator establishes one.
        runtime.pending_post_shed_generation = 0 if raw_pending_generation is not None else None
        runtime.pending_post_shed_after_reported_at = self._finite_or_none(
            raw.get("pending_post_shed_after_reported_at")
        )
        raw_pending_operation = raw.get("pending_operation_id")
        runtime.pending_operation_id = (
            raw_pending_operation if isinstance(raw_pending_operation, str) else None
        )
        count = raw.get("manual_start_blocked_count", 0)
        runtime.manual_start_blocked_count = count if isinstance(count, int) and count >= 0 else 0
        try:
            runtime.last_telemetry_validity = TelemetryValidity(
                raw.get("last_telemetry_validity", TelemetryValidity.UNKNOWN.value)
            )
        except ValueError:
            runtime.last_telemetry_validity = TelemetryValidity.INVALID
        try:
            runtime.last_reason_code = ReasonCode(
                raw.get("last_reason_code", ReasonCode.FAULT.value)
            )
        except ValueError:
            runtime.last_reason_code = ReasonCode.FAULT
        sequence = raw.get("decision_sequence", 0)
        runtime.decision_sequence = sequence if isinstance(sequence, int) and sequence >= 0 else 0

        restored: list[ShedStackEntry] = []
        restored_device_ids: set[str] = set()
        raw_stack = raw.get("shed_stack", [])
        if isinstance(raw_stack, list):
            for item in raw_stack:
                if not isinstance(item, dict):
                    continue
                device_id = item.get("device_id")
                operation_id = item.get("operation_id")
                generation = item.get("load_generation")
                snapshot = item.get("snapshot")
                if (
                    not isinstance(device_id, str)
                    or not device_id
                    or (model is not None and model.get_device(device_id) is None)
                    or device_id in restored_device_ids
                    or not isinstance(operation_id, str)
                    or not operation_id
                    or not isinstance(generation, int)
                    or generation < 0
                    or item.get("pre_state") is not True
                    or not isinstance(snapshot, dict)
                    or not snapshot
                ):
                    continue
                try:
                    reason = ReasonCode(item.get("reason_code", ReasonCode.FAULT.value))
                except ValueError:
                    continue
                restored.append(
                    ShedStackEntry(
                        device_id=device_id,
                        operation_id=operation_id,
                        pre_state=True,
                        snapshot=snapshot,
                        load_generation=generation,
                        reason_code=reason,
                        created_at=self._finite_or_none(item.get("created_at")),
                    )
                )
                restored_device_ids.add(device_id)
        runtime.shed_stack = restored

    @staticmethod
    def _finite_or_none(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = float(value)
        return value if math.isfinite(value) else None

    @staticmethod
    def _nonnegative_int_or_none(value: Any) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    @staticmethod
    def _normalize_action_event(
        event: Any,
        *,
        legacy_key: str | None = None,
    ) -> dict[str, Any] | None:
        """Return one bounded, versioned, idempotent scalar action record."""
        if not isinstance(event, dict):
            return None
        schema = event.get("event_schema")
        if schema is not None and (
            isinstance(schema, bool)
            or not isinstance(schema, int)
            or schema != EVENT_SCHEMA_VERSION
        ):
            return None
        action_id = event.get("action_id")
        if action_id is not None and (
            not isinstance(action_id, str) or not action_id.strip()
        ):
            return None
        normalized = {
            key: value[:_MAX_ACTION_FIELD_LENGTH] if isinstance(value, str) else value
            for key, value in event.items()
            if isinstance(key, str)
            and isinstance(value, (str, int, float, bool, type(None)))
        }
        timestamp = normalized.get("timestamp")
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or not math.isfinite(float(timestamp))
        ):
            normalized["timestamp"] = time.time()
        if action_id is None:
            if legacy_key is not None:
                digest = hashlib.sha256(
                    json.dumps(normalized, sort_keys=True, default=str).encode()
                ).hexdigest()[:24]
                normalized["action_id"] = f"legacy-{legacy_key}-{digest}"
            else:
                normalized["action_id"] = uuid.uuid4().hex
        normalized.setdefault("event_schema", EVENT_SCHEMA_VERSION)
        normalized.setdefault("event_type", EVENT_ACTION)
        normalized.setdefault("operation_id", "unknown")
        normalized.setdefault("policy_phase", "unknown")
        normalized.setdefault("execution_mode", "unknown")
        normalized.setdefault("source", "planner")
        normalized.setdefault("actor_id", None)
        normalized.setdefault("context_id", None)
        return normalized

    @classmethod
    def _normalize_history(cls, value: Any) -> list[dict[str, Any]]:
        """Sanitize and bound persisted action history before runtime use."""
        if not isinstance(value, list):
            return []
        normalized = [
            event
            for index, item in enumerate(value)
            if (event := cls._normalize_action_event(item, legacy_key=str(index))) is not None
        ]
        return normalized[-_MAX_AUDIT_ENTRIES:]

    def record_action(self, event: dict[str, Any]) -> None:
        """Upsert one bounded action record by stable action_id."""
        normalized = self._normalize_action_event(event)
        if normalized is None:
            return
        history = self._data.setdefault("audit_history", [])
        if not isinstance(history, list):
            history = []
            self._data["audit_history"] = history
        action_id = normalized["action_id"]
        for index, existing in enumerate(history):
            if isinstance(existing, dict) and existing.get("action_id") == action_id:
                merged = dict(existing)
                merged.update(normalized)
                history[index] = merged
                return
        history.append(normalized)
        del history[:-_MAX_AUDIT_ENTRIES]

    def audit_history(self) -> list[dict[str, Any]]:
        """Return a defensive copy of the bounded action journal."""
        history = self._normalize_history(self._data.get("audit_history", []))
        self._data["audit_history"] = history
        return [dict(item) for item in history]
