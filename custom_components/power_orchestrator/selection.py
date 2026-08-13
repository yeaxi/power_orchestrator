"""Deterministic shed/restore candidate selection.

Pure eligibility filters over the logical-device model. Shed selection also
produces a bounded rejection summary for diagnostics. None of these functions
mutate coordinator state; the coordinator assigns the returned diagnostics.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Sequence

from homeassistant.core import HomeAssistant

from .const import QUARANTINE_CLEAR_MAX_POWER_W
from .power_model import ManagedDevice, PowerModel
from .states import logical_device_state

MAX_SHED_REJECTION_DETAILS = 12


@dataclass(frozen=True)
class ShedRejections:
    """Bounded, state-safe projection of why no load could be shed."""

    counts: dict[str, int] = field(default_factory=dict)
    devices: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    truncated: int = 0
    evaluated_at: float | None = None


def shed_candidates(
    model: PowerModel,
    quarantined: set[str],
    *,
    now: float,
) -> tuple[list[ManagedDevice], ShedRejections]:
    """Return sheddable loads in order, plus a rejection summary if none qualify."""
    candidates: list[ManagedDevice] = []
    counts: dict[str, int] = {}
    details: list[dict[str, Any]] = []
    for device in model.get_shed_devices():
        reason: str | None = None
        if device.is_on is False:
            reason = "off"
        elif device.is_on is not True:
            reason = "state_unavailable"
        elif device.device_id in quarantined:
            reason = "quarantined"
        elif device.power_sensor_id is not None and not device.measured_power_valid:
            reason = f"power_{device.measured_power_reason}"
        elif (
            device.power_sensor_id is not None
            and device.measured_power <= QUARANTINE_CLEAR_MAX_POWER_W
        ):
            reason = "inactive_power"

        if reason is None:
            candidates.append(device)
            continue
        counts[reason] = counts.get(reason, 0) + 1
        if len(details) < MAX_SHED_REJECTION_DETAILS:
            details.append(
                {
                    "device_id": device.device_id,
                    "name": device.name[:80],
                    "reason": reason,
                    "measured_power_w": (
                        device.measured_power if device.measured_power_valid else None
                    ),
                }
            )

    total = sum(counts.values())
    if candidates:
        return candidates, ShedRejections(evaluated_at=now)
    return candidates, ShedRejections(
        counts=dict(sorted(counts.items())),
        devices=details,
        total=total,
        truncated=max(0, total - len(details)),
        evaluated_at=now,
    )


def shed_rejection_summary(counts: Mapping[str, int]) -> str:
    """Return a bounded state-safe summary of candidate rejection reasons."""
    if not counts:
        return "no configured devices"
    summary = ", ".join(f"{reason}={count}" for reason, count in counts.items())
    return summary[:180]


def restore_candidates(
    hass: HomeAssistant,
    model: PowerModel,
    *,
    planner_shed: Sequence[str],
    faulted: set[str],
    quarantined: set[str],
    cooldown_until: Mapping[str, float],
    restore_threshold_w: float,
    safety_reserve: float,
    current_load: float,
    now: float,
) -> list[ManagedDevice]:
    """Return planner-shed loads eligible for one guarded restore, in order.

    Fail-closed: the load must have been shed by the planner itself, be opted
    in, be confirmed OFF, not faulted/quarantined/paused/in cooldown, be a
    simple switchable actuator (climate is out of scope), and still fit under
    the restore threshold with the safety reserve once its expected power
    returns.
    """
    candidates: list[ManagedDevice] = []
    for device_id in reversed(planner_shed):
        device = model.get_device(device_id)
        if device is None:
            continue
        if not device.restore_enabled:
            continue
        if device.device_id in faulted or device.device_id in quarantined:
            continue
        if logical_device_state(hass, device) is not False:
            continue
        if device.pause_active:
            continue
        cooldown = cooldown_until.get(device.device_id)
        if cooldown is not None and now < cooldown:
            continue
        if any(
            entity_id.split(".", 1)[0] == "climate" for entity_id in device.control_entity_ids
        ):
            continue
        projected = current_load + max(0.0, float(device.expected_power)) + safety_reserve
        if projected > restore_threshold_w:
            continue
        candidates.append(device)
    return candidates
