"""Audit-journal writes and structured event emission.

Centralizes action-record normalization and Home Assistant event construction so
the coordinator only decides *what* to record, not the record/event shape.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from homeassistant.core import HomeAssistant

from .const import EVENT_SCHEMA_VERSION

_LOGGER = logging.getLogger(__name__)


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
