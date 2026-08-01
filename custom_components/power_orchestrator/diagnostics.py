"""Diagnostics support for Power Orchestrator."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

try:
    from homeassistant.components.diagnostics import async_redact_data
except ImportError:  # pragma: no cover - local Home Assistant test doubles
    def async_redact_data(data: Any, to_redact: set[str]) -> Any:
        """Redact sensitive keys in local test environments."""
        if isinstance(data, dict):
            return {
                key: "**REDACTED**" if key in to_redact else async_redact_data(value, to_redact)
                for key, value in data.items()
            }
        if isinstance(data, list):
            return [async_redact_data(value, to_redact) for value in data]
        return data

from .const import DOMAIN

_TO_REDACT = {
    "access_token",
    "api_key",
    "authorization",
    "client_id",
    "client_secret",
    "connection_string",
    "credentials",
    "email",
    "latitude",
    "longitude",
    "password",
    "refresh_token",
    "secret",
    "token",
}


def _bounded_count(raw: dict[str, Any], key: str) -> int:
    """Return a safe count for a persisted collection."""
    value = raw.get(key)
    if isinstance(value, (list, tuple, set, frozenset)):
        return len(value)
    return 0


def _bounded_runtime_data(coordinator: Any) -> dict[str, Any]:
    """Project runtime state without exposing audit/user/entity identities."""
    raw = getattr(coordinator, "data", None) or {}
    if not isinstance(raw, dict):
        return {"value_type": type(raw).__name__}

    safe_keys = (
        "status",
        "mode",
        "execution_mode",
        "policy_phase",
        "reason_code",
        "grid_ok",
        "load_sensor_valid",
        "load_sensor_reason",
        "startup_safe",
        "physical_commands_allowed",
        "journal_persistence_blocked",
        "action_journal_invalid",
        "journal_unresolved_count",
        "audit_history_total",
        "audit_history_truncated",
        "faulted_devices_count",
        "recovery_blocked_devices_count",
        "safety_fault_reason",
    )
    projected = {key: raw[key] for key in safe_keys if key in raw}
    # These counts are deliberately derived from persisted sets rather than
    # returning device IDs, entity IDs, action IDs, actor IDs, or contexts.
    if "faulted_devices_count" not in projected:
        projected["faulted_devices_count"] = _bounded_count(raw, "faulted_devices")
    if "recovery_blocked_devices_count" not in projected:
        projected["recovery_blocked_devices_count"] = _bounded_count(
            raw, "recovery_blocked_devices"
        )
    return projected


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return bounded, redacted diagnostics for a config entry."""
    del hass
    runtime = getattr(entry, "runtime_data", None)
    coordinator = getattr(runtime, "coordinator", None)
    entry_data = getattr(entry, "data", {}) or {}
    options = getattr(entry, "options", {}) or {}
    return {
        "integration": DOMAIN,
        "entry_data": async_redact_data(
            dict(entry_data) if isinstance(entry_data, dict) else {},
            _TO_REDACT,
        ),
        "options": async_redact_data(
            dict(options) if isinstance(options, dict) else {},
            _TO_REDACT,
        ),
        "runtime": {
            "loaded": runtime is not None,
            "data": _bounded_runtime_data(coordinator),
        },
    }
