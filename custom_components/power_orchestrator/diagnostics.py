"""Bounded diagnostics for Power Orchestrator."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_TO_REDACT = {
    "access_token", "api_key", "authorization", "client_id", "client_secret",
    "connection_string", "credentials", "email", "latitude", "longitude",
    "password", "refresh_token", "secret", "token",
}


def _count(raw: dict[str, Any], key: str) -> int:
    value = raw.get(key)
    return len(value) if isinstance(value, (list, tuple, set, frozenset)) else 0


def _bounded_runtime_data(coordinator: Any) -> dict[str, Any]:
    raw = getattr(coordinator, "data", None) or {}
    if not isinstance(raw, dict):
        return {"value_type": type(raw).__name__}
    keys = (
        "status", "mode", "policy_phase", "reason_code", "grid_ok",
        "load_sensor_valid", "load_sensor_reason", "startup_safe", "physical_commands_allowed",
        "journal_persistence_blocked", "action_journal_invalid", "journal_unresolved_count",
        "audit_history_total", "audit_history_truncated", "faulted_devices_count",
        "quarantined_devices_count", "safety_fault_reason", "pending_restore_count",
    )
    projected = {key: raw[key] for key in keys if key in raw}
    projected.setdefault("faulted_devices_count", _count(raw, "faulted_devices"))
    projected.setdefault("quarantined_devices_count", _count(raw, "quarantined_devices"))
    projected.setdefault("pending_restore_count", _count(raw, "pending_restore_ids"))
    return projected


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Return bounded diagnostics without entity IDs or audit identities."""
    del hass
    runtime = getattr(entry, "runtime_data", None)
    coordinator = getattr(runtime, "coordinator", None)
    data = getattr(entry, "data", {}) or {}
    options = getattr(entry, "options", {}) or {}
    return {
        "integration": DOMAIN,
        "entry_data": async_redact_data(dict(data) if isinstance(data, dict) else {}, _TO_REDACT),
        "options": async_redact_data(dict(options) if isinstance(options, dict) else {}, _TO_REDACT),
        "runtime": {"loaded": runtime is not None, "data": _bounded_runtime_data(coordinator)},
    }
