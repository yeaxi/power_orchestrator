"""Bounded restore intents and tickets.

An intent records that an external owner would still like a load to run. It
never authorizes a normal ON/OFF command by itself. A restore ticket records
that Power Orchestrator actually shed the load; both are required for restore.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping

from .intent_binding import binding_permits, validate_binding

MAX_INTENT_TTL_S = 24 * 60 * 60


def _bounded_text(value: Any, *, maximum: int = 80) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("value must be non-empty text")
    return value.strip()[:maximum]


def _validate_revision(value: str | None) -> None:
    if value is None:
        return
    if not isinstance(value, str) or re.fullmatch("[0-9a-f]{32}", value) is None:
        raise ValueError("invalid intent revision")


@dataclass(frozen=True)
class RestoreIntent:
    """One source-specific, time-bounded request to permit later restoration."""

    source: str
    active: bool
    expires_at: float
    permit_entity: str | None = None
    request_entity: str | None = None
    request_data: dict[str, Any] | None = None
    revision: str | None = None

    def __post_init__(self) -> None:
        _bounded_text(self.source)
        if not isinstance(self.active, bool):
            raise ValueError("active must be boolean")
        if (
            isinstance(self.expires_at, bool)
            or not isinstance(self.expires_at, (int, float))
            or not math.isfinite(self.expires_at)
        ):
            raise ValueError("invalid intent deadline")
        if self.permit_entity is not None and not self.permit_entity.startswith("binary_sensor."):
            raise ValueError("permit_entity must be a binary sensor")
        object.__setattr__(self, "request_data", validate_binding(self.request_entity, self.request_data))
        _validate_revision(self.revision)

    def validate_recovery(self, now: float) -> None:
        """Reject a durable deadline outside the supported publication horizon."""
        if self.expires_at > now + MAX_INTENT_TTL_S:
            raise ValueError("persisted intent deadline exceeds maximum lifetime")

    def due(self, now: float) -> bool:
        return not self.active or now >= self.expires_at

    def permitted(self, now: float, state_lookup: Callable[[str], Any]) -> bool:
        if self.due(now):
            return False
        if not binding_permits(self.request_entity, self.request_data, state_lookup):
            return False
        if self.permit_entity is None:
            return True
        state = state_lookup(self.permit_entity)
        return state is not None and getattr(state, "state", None) == "on"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.request_entity is None:
            data.pop("request_entity")
            data.pop("request_data")
        if self.revision is None:
            data.pop("revision")
        return data

    @classmethod
    def from_dict(cls, raw: Any) -> RestoreIntent:
        if not isinstance(raw, Mapping):
            raise ValueError("invalid persisted intent")
        allowed = {"source", "active", "expires_at", "permit_entity", "request_entity", "request_data", "revision"}
        if not set(raw) <= allowed or not {"source", "active", "expires_at"} <= set(raw):
            raise ValueError("invalid persisted intent")
        return cls(
            source=raw["source"],
            active=raw["active"],
            expires_at=raw["expires_at"],
            permit_entity=raw.get("permit_entity"),
            request_entity=raw.get("request_entity"),
            request_data=raw.get("request_data"),
            revision=raw.get("revision"),
        )


# Compatibility name used by the 0.6.1 storage migration and external imports.
RunRequest = RestoreIntent


@dataclass(frozen=True)
class RestoreTicket:
    """Durable proof that this controller switched one logical load off."""

    device_id: str
    cause: str
    operation_id: str
    created_at: float
    expires_at: float
    restore_state: dict[str, Any] = field(default_factory=dict)
    intent_sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _bounded_text(self.device_id)
        _bounded_text(self.cause, maximum=160)
        _bounded_text(self.operation_id)
        for value in (self.created_at, self.expires_at):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("invalid ticket timestamp")
            if not math.isfinite(value):
                raise ValueError("invalid ticket timestamp")
        if self.expires_at <= self.created_at:
            raise ValueError("ticket must expire after creation")
        if self.created_at < 0 or self.expires_at > self.created_at + MAX_INTENT_TTL_S:
            raise ValueError("ticket lifetime is outside the supported range")
        if not isinstance(self.restore_state, dict):
            raise ValueError("invalid restore state")
        for source in self.intent_sources:
            _bounded_text(source)

    def validate_recovery(self, now: float) -> None:
        """A recovered ticket must prove an action that already happened."""
        if self.created_at > now:
            raise ValueError("persisted ticket creation is in the future")

    def expired(self, now: float) -> bool:
        return now >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["intent_sources"] = list(self.intent_sources)
        return payload

    @classmethod
    def from_dict(cls, raw: Any) -> RestoreTicket:
        if not isinstance(raw, Mapping):
            raise ValueError("invalid persisted restore ticket")
        required = {"device_id", "cause", "operation_id", "created_at", "expires_at"}
        if not required <= set(raw):
            raise ValueError("invalid persisted restore ticket")
        sources = raw.get("intent_sources", ())
        if not isinstance(sources, (list, tuple)):
            raise ValueError("invalid ticket intent sources")
        state = raw.get("restore_state", {})
        return cls(
            device_id=raw["device_id"],
            cause=raw["cause"],
            operation_id=raw["operation_id"],
            created_at=raw["created_at"],
            expires_at=raw["expires_at"],
            restore_state=dict(state) if isinstance(state, Mapping) else {},
            intent_sources=tuple(sources),
        )


class RestoreIntentRegistry:
    """Small state owner for concurrent source-specific restore intents."""

    def __init__(self, values: Mapping[tuple[str, str], RestoreIntent] | None = None) -> None:
        self._values = dict(values or {})

    def set(self, device_id: str, intent: RestoreIntent) -> None:
        self._values[(_bounded_text(device_id), intent.source)] = intent

    def remove(self, device_id: str, source: str) -> None:
        self._values.pop((device_id, source), None)

    def matches(self, device_id: str, source: str, expected: RestoreIntent | None) -> bool:
        """Compare exact publication snapshots, including unique ABA revision."""
        current = self._values.get((device_id, source))
        if current is None or expected is None:
            return current is expected
        return json.dumps(current.to_dict(), sort_keys=True) == json.dumps(expected.to_dict(), sort_keys=True)

    def matches_snapshot(self, device_id: str, source: str, snapshot: dict[str, Any] | None) -> bool:
        """Omission retains compatibility; an empty object expects absence."""
        if snapshot is None:
            return True
        expected = RestoreIntent.from_dict(snapshot) if snapshot else None
        return self.matches(device_id, source, expected)

    def prune(self, now: float) -> bool:
        before = len(self._values)
        self._values = {key: intent for key, intent in self._values.items() if not intent.due(now)}
        return len(self._values) != before

    def permits(self, device_id: str, now: float, state_lookup: Callable[[str], Any]) -> bool:
        return any(
            key_device == device_id and intent.permitted(now, state_lookup)
            for (key_device, _), intent in self._values.items()
        )

    def active_sources(self, device_id: str, now: float) -> tuple[str, ...]:
        return tuple(
            sorted(
                source
                for (key_device, source), intent in self._values.items()
                if key_device == device_id and not intent.due(now)
            )
        )

    def as_dict(self) -> dict[tuple[str, str], RestoreIntent]:
        return dict(self._values)
