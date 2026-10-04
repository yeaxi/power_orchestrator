"""Audit-journal writes and structured event emission.

Centralizes action-record normalization and Home Assistant event construction so
the coordinator only decides *what* to record, not the record/event shape.
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from typing import Any

from homeassistant.core import HomeAssistant

from .const import EVENT_SCHEMA_VERSION, MAX_CUSTOM_THRESHOLDS

_LOGGER = logging.getLogger(__name__)

_SNAPSHOT_FIELDS = {
    "captured_at",
    "integration_version",
    "policy_version",
    "load_w",
    "load_valid",
    "load_reason",
    "load_reported_at",
    "load_age_s",
    "load_max_age_s",
    "safety_state",
    "safety_available",
    "safety_ok",
    "safety_reported_at",
    "battery_threshold",
    "battery_charge",
    "battery_min_soc",
    "mode",
}


def normalize_input_snapshot(value: Any) -> dict[str, Any] | None:
    """Keep bounded evidence fields; arbitrary nested data and identities are dropped."""
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    for key in _SNAPSHOT_FIELDS:
        if key not in value:
            continue
        item = value[key]
        if isinstance(item, str):
            result[key] = item[:128]
        elif item is None or isinstance(item, bool):
            result[key] = item
        elif isinstance(item, (int, float)) and math.isfinite(item):
            result[key] = item
    thresholds = value.get("thresholds")
    if isinstance(thresholds, list):
        result["thresholds"] = [
            {key: tier[key] for key in ("limit_w", "duration_s")}
            for tier in thresholds[:MAX_CUSTOM_THRESHOLDS]
            if isinstance(tier, dict)
            and all(
                isinstance(tier.get(key), (int, float))
                and not isinstance(tier[key], bool)
                and math.isfinite(tier[key])
                and tier[key] >= 0
                for key in ("limit_w", "duration_s")
            )
        ]
    return result


def new_action_id(prefix: str) -> str:
    """Return a unique, prefixed action identifier."""
    return f"{prefix}_{uuid.uuid4().hex}"


def record_action(store: Any, event: dict[str, Any]) -> bool:
    """Normalize and persist one action-journal record.

    Returns whether a record was written (so the caller can mark the journal
    dirty). Records without a non-empty ``action_id`` are ignored.
    """
    action_id = event.get("action_id")
    if not isinstance(action_id, str) or not action_id:
        return False
    record = dict(event)
    record.setdefault("event_schema", EVENT_SCHEMA_VERSION)
    record.setdefault("timestamp", time.time())
    writer = getattr(store, "record_action", None)
    if callable(writer):
        writer(record)
        return True
    return False


def emit_event(
    hass: HomeAssistant,
    event_type: str,
    data: dict[str, Any],
    *,
    entry_id: str,
    mode: str,
) -> None:
    """Fire a bounded structured event on the Home Assistant bus (best effort)."""
    bus = getattr(hass, "bus", None)
    emitter = getattr(bus, "async_fire", None)
    event = {
        **data,
        "schema_version": EVENT_SCHEMA_VERSION,
        "event_type": event_type,
        "entry_id": entry_id,
        "mode": mode,
    }
    if callable(emitter):
        try:
            emitter(event_type, event)
        except Exception:  # pragma: no cover - event delivery is non-safety-critical
            _LOGGER.debug("Power Orchestrator event delivery failed", exc_info=True)
