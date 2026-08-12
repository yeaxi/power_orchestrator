"""Durable fault/quarantine bookkeeping for managed loads.

Groups the fault, quarantine, and reason state (and its dirty flag) into one
object with a single latch/clear surface, so the coordinator no longer scatters
the same three-set mutation across every command path.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_MAX_REASON_LENGTH = 160


@dataclass
class FaultRegistry:
    """Mutable set of faulted/quarantined device IDs with their reasons."""

    faulted: set[str] = field(default_factory=set)
    quarantined: set[str] = field(default_factory=set)
    reasons: dict[str, str] = field(default_factory=dict)
    dirty: bool = False

    def latch(self, device_id: str, reason: str) -> None:
        """Mark a device faulted and quarantined with a bounded reason."""
        self.quarantined.add(device_id)
        self.faulted.add(device_id)
        self.reasons[device_id] = str(reason)[:_MAX_REASON_LENGTH]
        self.dirty = True

    def clear(self, device_id: str) -> None:
        """Clear a device's fault/quarantine state."""
        self.quarantined.discard(device_id)
        self.faulted.discard(device_id)
        self.reasons.pop(device_id, None)
        self.dirty = True

    def is_flagged(self, device_id: str) -> bool:
        """Return whether the device is faulted or quarantined."""
        return device_id in self.faulted or device_id in self.quarantined
