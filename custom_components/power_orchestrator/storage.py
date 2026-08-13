"""Bounded runtime persistence for the load-shedding controller."""

from __future__ import annotations

import copy
import math
import time
from typing import Any, Mapping

from homeassistant.helpers.storage import Store

from .const import (
    DEFAULT_MODE,
    DEVICE_RUNTIME_SCHEMA_VERSION,
    FAULT_NOTIFICATION_SCHEMA_VERSION,
    MAX_RUNTIME_PAUSE_SECONDS,
    MODE_AUTO,
    MODE_OBSERVE,
    MODE_OFF,
    MODES,
    STORAGE_VERSION,
)
from .policy import PolicyEngine, PolicyPhase, ReasonCode, TelemetryValidity
from .power_model import PowerModel

_MAX_AUDIT_ENTRIES = 100
_MAX_UNRESOLVED_ACTIONS = 16
_MAX_ACTION_FIELD_LENGTH = 256
_MAX_FAULT_REASON_LENGTH = 160


class RuntimeStore:
    """Persist mode, pauses, policy fences, faults, and a bounded action journal."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._data: dict[str, Any] = {}
        self._safety_storage_invalid = False
        self._action_journal_invalid = False

    @property
    def safety_storage_invalid(self) -> bool:
        return self._safety_storage_invalid

    @property
    def action_journal_invalid(self) -> bool:
        return self._action_journal_invalid

    async def async_load(self) -> None:
        raw = await self._store.async_load()
        if not isinstance(raw, dict):
            self._data = {}
            self._safety_storage_invalid = bool(raw is not None)
            self._action_journal_invalid = False
            return
        self._data = copy.deepcopy(raw)
        self._migrate_device_runtime_payload()
        self._action_journal_invalid = bool(self._data.get("action_journal_invalid"))
        self._data["audit_history"] = self._normalize_history(self._data.get("audit_history", []))

    async def async_save(self) -> None:
        self._data["storage_version"] = STORAGE_VERSION
        await self._store.async_save(self._data)

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def restore_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        self._data = copy.deepcopy(dict(snapshot))

    def set_mode(self, mode: str) -> None:
        if mode in MODES:
            self._data["mode"] = mode
            self._data.pop("execution_mode", None)

    def restore_mode(self) -> str | None:
        if "mode" not in self._data:
            return None
        value = self._data.get("mode")
        return value if value in MODES else DEFAULT_MODE

    def resolve_unified_mode(self, config_execution: Any = None) -> str:
        """Map legacy planner/execution pairs onto one persisted mode.

        Old observe execution becomes observe. Otherwise retain stored planner
        auto/off when present. New default is observe. Legacy execution_mode is
        cleared from the payload. Config execution is consulted only while the
        store still carries the dual-mode shape or has no usable mode yet.
        """
        stored = self._data.get("mode")
        legacy_execution = self._data.get("execution_mode")
        has_legacy_execution = "execution_mode" in self._data
        if legacy_execution not in {"observe", "live"}:
            if has_legacy_execution or stored not in MODES:
                legacy_execution = (
                    config_execution if config_execution in {"observe", "live"} else None
                )
            else:
                legacy_execution = None
        if legacy_execution == "observe":
            mode = MODE_OBSERVE
        elif stored in (MODE_AUTO, MODE_OFF):
            mode = stored
        elif stored == MODE_OBSERVE:
            mode = MODE_OBSERVE
        else:
            mode = DEFAULT_MODE
        self.set_mode(mode)
        return mode

    def save_pending_restore(self, device_ids: list[str]) -> None:
        """Persist the ordered, unique restore queue."""
        seen: set[str] = set()
        pending: list[str] = []
        for device_id in device_ids:
            if not isinstance(device_id, str) or not device_id or device_id in seen:
                continue
            seen.add(device_id)
            pending.append(device_id)
        self._data["pending_restore"] = pending

    def restore_pending_restore(self, model: PowerModel) -> list[str]:
        """Return configured restore candidates in their durable shed order."""
        raw = self._data.get("pending_restore", [])
        if not isinstance(raw, list):
            return []
        configured = {device.device_id for device in model.all_devices()}
        seen: set[str] = set()
        pending: list[str] = []
        for device_id in raw:
            if (
                not isinstance(device_id, str)
                or device_id not in configured
                or device_id in seen
            ):
                continue
            seen.add(device_id)
            pending.append(device_id)
        return pending

    def update_pause_timestamp(self, device_id: str, pause_until: float | None) -> None:
        pauses = self._data.setdefault("pause_timestamps", {})
        if not isinstance(pauses, dict):
            pauses = {}
            self._data["pause_timestamps"] = pauses
        if pause_until is None:
            pauses.pop(device_id, None)
            return
        if isinstance(pause_until, bool) or not isinstance(pause_until, (int, float)):
            return
        value = float(pause_until)
        if math.isfinite(value):
            pauses[device_id] = value

    def set_pause(self, device_id: str, pause_until_or_duration: float) -> None:
        """Persist an absolute pause timestamp, accepting legacy duration calls.

        Durations are at most ``MAX_RUNTIME_PAUSE_SECONDS``. Larger values are
        treated as absolute epoch timestamps so a zero-length pause (until=now)
        is not reinterpreted as a multi-year duration.
        """
        if isinstance(pause_until_or_duration, bool):
            return
        try:
            value = float(pause_until_or_duration)
        except (TypeError, ValueError):
            return
        if not math.isfinite(value) or value < 0:
            return
        now = time.time()
        if value <= MAX_RUNTIME_PAUSE_SECONDS:
            absolute = now + value
        else:
            absolute = value
        self.update_pause_timestamp(device_id, min(absolute, now + MAX_RUNTIME_PAUSE_SECONDS))

    def clear_pause(self, device_id: str) -> None:
        self.update_pause_timestamp(device_id, None)

    def restore_pause_timestamps(
        self,
        model: PowerModel,
        max_pause_seconds: float = MAX_RUNTIME_PAUSE_SECONDS,
    ) -> None:
        pauses = self._data.get("pause_timestamps", {})
        if not isinstance(pauses, dict):
            self._data["pause_timestamps"] = {}
            return
        now = time.time()
        try:
            maximum = float(max_pause_seconds)
        except (TypeError, ValueError):
            maximum = MAX_RUNTIME_PAUSE_SECONDS
        if not math.isfinite(maximum) or maximum < 0:
            maximum = MAX_RUNTIME_PAUSE_SECONDS
        for device_id, value in list(pauses.items()):
            device = model.get_device(device_id)
            valid = (
                device is not None
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
                and now < float(value) <= now + maximum
            )
            if not valid:
                pauses.pop(device_id, None)
            elif device is not None:
                device.pause_until = float(value)

    def save_device_runtime(
        self,
        model: PowerModel,
        *,
        faulted_devices: set[str] | frozenset[str] | list[str] | tuple[str, ...] = (),
        quarantined_devices: set[str] | frozenset[str] | list[str] | tuple[str, ...] = (),
        fault_reasons: Mapping[str, str] | None = None,
    ) -> None:
        """Persist fault and quarantine state for configured loads."""
        configured = {device.device_id for device in model.all_devices()}
        devices: dict[str, dict[str, Any]] = {
            device.device_id: {} for device in model.all_devices()
        }
        reasons = {
            device_id: value[:_MAX_FAULT_REASON_LENGTH]
            for device_id, value in (fault_reasons or {}).items()
            if isinstance(device_id, str)
            and device_id in configured
            and isinstance(value, str)
            and value.strip()
        }
        self._data["device_runtime"] = {
            "schema_version": DEVICE_RUNTIME_SCHEMA_VERSION,
            "devices": devices,
            "faulted_devices": sorted(device_id for device_id in faulted_devices if device_id in configured),
            "quarantined_devices": sorted(
                device_id for device_id in quarantined_devices if device_id in configured
            ),
            "fault_reasons": reasons,
        }

    def restore_device_runtime(self, model: PowerModel) -> tuple[set[str], set[str]]:
        """Return validated fault/quarantine sets from persisted runtime state."""
        raw = self._data.get("device_runtime")
        configured = {device.device_id for device in model.all_devices()}
        if raw is None:
            self._safety_storage_invalid = False
            return set(), set()
        if not self._device_runtime_envelope_is_valid(raw):
            self._safety_storage_invalid = True
            return set(), configured
        self._safety_storage_invalid = False
        faulted = self._validated_device_set(raw.get("faulted_devices"), configured)
        quarantined = self._validated_device_set(
            raw.get("quarantined_devices"), configured
        )
        for device_id in faulted | quarantined:
            device = model.get_device(device_id)
            if device is not None:
                device.is_on = None
        return faulted, quarantined

    def _migrate_device_runtime_payload(self) -> None:
        """Translate the pre-quarantine runtime key without weakening fail-closed checks."""
        raw = self._data.get("device_runtime")
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            return
        migrated = copy.deepcopy(raw)
        if "quarantined_devices" not in migrated:
            legacy = migrated.get("recovery_blocked_devices")
            if not isinstance(legacy, list):
                return
            migrated["quarantined_devices"] = legacy
        migrated.pop("recovery_blocked_devices", None)
        migrated["schema_version"] = DEVICE_RUNTIME_SCHEMA_VERSION
        self._data["device_runtime"] = migrated

    def restore_fault_reasons(self, model: PowerModel) -> dict[str, str]:
        raw = self._data.get("device_runtime")
        configured = {device.device_id for device in model.all_devices()}
        if raw is None:
            return {}
        if not self._device_runtime_envelope_is_valid(raw):
            self._safety_storage_invalid = True
            return {
                device_id: ReasonCode.PERSISTED_RUNTIME_INVALID.value
                for device_id in configured
            }
        values = raw.get("fault_reasons", {})
        if not isinstance(values, dict):
            return {}
        return {
            device_id: reason[:_MAX_FAULT_REASON_LENGTH]
            for device_id, reason in values.items()
            if isinstance(device_id, str)
            and device_id in configured
            and isinstance(reason, str)
            and reason.strip()
        }

    def save_fault_notification_state(
        self,
        active_fingerprints: Mapping[str, str],
        pending_dismissal_fingerprints: Mapping[str, str],
    ) -> None:
        self._data["fault_notifications"] = {
            "schema_version": FAULT_NOTIFICATION_SCHEMA_VERSION,
            "active": self._bounded_string_map(active_fingerprints),
            "pending_dismissal": self._bounded_string_map(pending_dismissal_fingerprints),
        }

    def restore_fault_notification_state(
        self,
        model: PowerModel,
    ) -> tuple[dict[str, str], dict[str, str]]:
        raw = self._data.get("fault_notifications")
        if not isinstance(raw, dict) or raw.get("schema_version") != FAULT_NOTIFICATION_SCHEMA_VERSION:
            return {}, {}
        configured = {device.device_id for device in model.all_devices()}

        def valid(value: Any) -> dict[str, str]:
            if not isinstance(value, dict):
                return {}
            return {
                key: item[:128]
                for key, item in value.items()
                if isinstance(key, str)
                and key in configured
                and isinstance(item, str)
                and item.strip()
            }

        return valid(raw.get("active")), valid(raw.get("pending_dismissal"))

    def save_policy_runtime(self, engine: PolicyEngine) -> None:
        runtime = engine.runtime
        self._data["policy_runtime"] = {
            "phase": runtime.phase.value,
            "pending_post_shed_generation": runtime.pending_post_shed_generation,
            "pending_post_shed_after_reported_at": runtime.pending_post_shed_after_reported_at,
            "pending_operation_id": runtime.pending_operation_id,
            "last_shed_load_generation": runtime.last_shed_load_generation,
            "pending_post_restore_generation": runtime.pending_post_restore_generation,
            "pending_post_restore_after_reported_at": runtime.pending_post_restore_after_reported_at,
            "pending_restore_operation_id": runtime.pending_restore_operation_id,
            "last_restore_load_generation": runtime.last_restore_load_generation,
            "last_telemetry_validity": runtime.last_telemetry_validity.value,
            "last_reason_code": runtime.last_reason_code.value,
            "decision_sequence": runtime.decision_sequence,
        }

    def restore_policy_runtime(self, engine: PolicyEngine, model: PowerModel | None = None) -> None:
        del model
        raw = self._data.get("policy_runtime")
        if not isinstance(raw, dict):
            return
        runtime = engine.runtime
        try:
            runtime.phase = PolicyPhase(raw.get("phase", PolicyPhase.STARTUP.value))
        except (TypeError, ValueError):
            runtime.phase = PolicyPhase.FAULT
        # Monotonic dwell timestamps are process-local. A host reboot changes
        # their clock origin, so every dwell starts from fresh observations.
        runtime.active_tier = None
        runtime.tier_started_at = None
        runtime.tier_since = {}
        pending = raw.get("pending_post_shed_generation")
        runtime.pending_post_shed_generation = (
            pending if isinstance(pending, int) and not isinstance(pending, bool) and pending >= 0 else None
        )
        runtime.pending_post_shed_after_reported_at = self._finite_or_none(
            raw.get("pending_post_shed_after_reported_at")
        )
        runtime.pending_operation_id = raw.get("pending_operation_id") if isinstance(raw.get("pending_operation_id"), str) else None
        last_generation = raw.get("last_shed_load_generation")
        runtime.last_shed_load_generation = last_generation if isinstance(last_generation, int) and last_generation >= 0 else None
        restore_pending = raw.get("pending_post_restore_generation")
        runtime.pending_post_restore_generation = (
            restore_pending
            if isinstance(restore_pending, int)
            and not isinstance(restore_pending, bool)
            and restore_pending >= 0
            else None
        )
        runtime.pending_post_restore_after_reported_at = self._finite_or_none(
            raw.get("pending_post_restore_after_reported_at")
        )
        runtime.pending_restore_operation_id = (
            raw.get("pending_restore_operation_id")
            if isinstance(raw.get("pending_restore_operation_id"), str)
            else None
        )
        # Monotonic restore windows are never restored across process restart.
        runtime.restore_since = None
        last_restore_generation = raw.get("last_restore_load_generation")
        runtime.last_restore_load_generation = (
            last_restore_generation
            if isinstance(last_restore_generation, int)
            and not isinstance(last_restore_generation, bool)
            and last_restore_generation >= 0
            else None
        )
        try:
            runtime.last_telemetry_validity = TelemetryValidity(
                raw.get("last_telemetry_validity", TelemetryValidity.UNKNOWN.value)
            )
        except (TypeError, ValueError):
            runtime.last_telemetry_validity = TelemetryValidity.INVALID
        try:
            runtime.last_reason_code = ReasonCode(
                raw.get("last_reason_code", ReasonCode.FAULT.value)
            )
        except (TypeError, ValueError):
            runtime.last_reason_code = ReasonCode.FAULT
        sequence = raw.get("decision_sequence", 0)
        runtime.decision_sequence = sequence if isinstance(sequence, int) and sequence >= 0 else 0

    def record_action(self, event: dict[str, Any]) -> None:
        normalized = self._normalize_action_event(event)
        if normalized is None:
            return
        history = self._normalize_history(self._data.get("audit_history", []))
        action_id = normalized["action_id"]
        for index, previous in enumerate(history):
            if previous.get("action_id") == action_id:
                merged = dict(previous)
                merged.update(normalized)
                history[index] = merged
                self._data["audit_history"] = self._normalize_history(history)
                return
        history.append(normalized)
        self._data["audit_history"] = self._normalize_history(history)

    def unresolved_actions(self) -> list[dict[str, Any]]:
        history = self._normalize_history(self._data.get("audit_history", []))
        return [
            event for event in history
            if event.get("phase") in {"prepared", "dispatched"}
            or event.get("result") in {"prepared", "dispatched"}
        ][-_MAX_UNRESOLVED_ACTIONS:]

    def audit_history(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._normalize_history(self._data.get("audit_history", [])))

    def _device_runtime_envelope_is_valid(self, raw: Any) -> bool:
        return (
            isinstance(raw, dict)
            and raw.get("schema_version") in {1, DEVICE_RUNTIME_SCHEMA_VERSION}
            and isinstance(raw.get("devices"), dict)
            and isinstance(raw.get("faulted_devices"), list)
            and isinstance(raw.get("quarantined_devices"), list)
        )

    @staticmethod
    def _validated_device_set(value: Any, configured: set[str]) -> set[str]:
        return {
            item for item in value
            if isinstance(item, str) and item in configured
        } if isinstance(value, list) else set()

    @staticmethod
    def _finite_or_none(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        converted = float(value)
        return converted if math.isfinite(converted) else None

    @staticmethod
    def _bounded_string_map(value: Mapping[str, str]) -> dict[str, str]:
        return {
            key: item[:128]
            for key, item in value.items()
            if isinstance(key, str) and key.strip() and isinstance(item, str) and item.strip()
        }

    def _normalize_action_event(self, event: Any) -> dict[str, Any] | None:
        if not isinstance(event, dict):
            return None
        action_id = event.get("action_id")
        action = event.get("action")
        if not isinstance(action_id, str) or not action_id.strip() or not isinstance(action, str) or not action.strip():
            return None
        normalized: dict[str, Any] = {
            "action_id": action_id[:_MAX_ACTION_FIELD_LENGTH],
            "action": action[:_MAX_ACTION_FIELD_LENGTH],
        }
        for key, value in event.items():
            if key in {"action_id", "action"}:
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                if isinstance(value, str):
                    normalized[key] = value[:_MAX_ACTION_FIELD_LENGTH]
                elif isinstance(value, float) and not math.isfinite(value):
                    continue
                else:
                    normalized[key] = value
        normalized.setdefault("timestamp", time.time())
        return normalized

    def _normalize_history(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        normalized: list[dict[str, Any]] = []
        for item in value[-_MAX_AUDIT_ENTRIES:]:
            event = self._normalize_action_event(item)
            if event is not None:
                normalized.append(event)
        return normalized
