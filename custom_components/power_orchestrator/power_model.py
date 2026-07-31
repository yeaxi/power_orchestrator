"""Logical load model — expected power, measured telemetry, and ownership."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .policy import Ownership


@dataclass
class ManagedDevice:
    """A logical optional load with one or more physical actuators."""

    # Config (persisted in config entry)
    device_id: str
    name: str
    entity_id: str
    expected_power: int = 0  # W, used for admission decisions
    only_from_solar: bool = False
    power_sensor_id: str | None = None
    priority: int = 1  # normal-start priority; lower starts first
    shed_priority: int | None = None  # lower sheds first; independent from start priority
    restore_priority: int | None = None  # optional fallback; normal recovery is LIFO
    actuator_entity_ids: tuple[str, ...] = ()
    hvac_mode_on: str = "heat"

    # Runtime state (reconciled from HA after start/restart)
    is_on: bool | None = None  # None = state unknown/unavailable
    measured_power: float = 0.0  # compatibility projection; validity is separate
    measured_power_valid: bool = False
    measured_power_reason: str = "not_sampled"
    pause_until: float | None = None  # utc timestamp
    last_turn_off_time: float | None = None
    ownership: Ownership = Ownership.UNKNOWN
    ownership_until: float | None = None
    snapshot: dict[str, Any] | None = None

    @property
    def control_entity_ids(self) -> tuple[str, ...]:
        """Return the physical members of this logical load exactly once."""
        return tuple(dict.fromkeys((self.entity_id, *self.actuator_entity_ids)))

    @property
    def pause_active(self) -> bool:
        """Return True if the device is in pause period."""
        if self.pause_until is None:
            return False
        return time.time() < self.pause_until

    def capture_runtime_snapshot(self, states: dict[str, Any]) -> dict[str, Any]:
        """Capture only the actuator state needed for exact recovery."""
        self.snapshot = {
            entity_id: {
                "state": getattr(states.get(entity_id), "state", None),
                "attributes": dict(
                    getattr(states.get(entity_id), "attributes", {}) or {}
                ),
            }
            for entity_id in self.control_entity_ids
        }
        return self.snapshot

    def to_dict(self) -> dict[str, Any]:
        """Serialize config and safe diagnostic projections."""
        return {
            "device_id": self.device_id,
            "name": self.name,
            "entity": self.entity_id,
            "expected_power": self.expected_power,
            "only_from_solar": self.only_from_solar,
            "power_sensor": self.power_sensor_id,
            "priority": self.priority,
            "shed_priority": self.shed_priority,
            "restore_priority": self.restore_priority,
            "actuators": list(self.actuator_entity_ids),
            "is_on": self.is_on,
            "measured_power": self.measured_power if self.measured_power_valid else None,
            "measured_power_valid": self.measured_power_valid,
            "measured_power_reason": self.measured_power_reason,
            "ownership": self.ownership.value,
            "ownership_until": self.ownership_until,
            "pause_until": self.pause_until,
            "has_snapshot": self.snapshot is not None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ManagedDevice":
        """Create from a validated or legacy config dict."""
        raw_actuators = data.get("actuators", ())
        if isinstance(raw_actuators, str):
            raw_actuators = (raw_actuators,)
        if not isinstance(raw_actuators, (list, tuple)):
            raw_actuators = ()
        actuators = tuple(
            value for value in raw_actuators if isinstance(value, str) and value
        )
        ownership = data.get("ownership", Ownership.UNKNOWN)
        try:
            ownership = Ownership(ownership)
        except ValueError:
            ownership = Ownership.UNKNOWN
        return cls(
            device_id=data["device_id"],
            name=data["name"],
            entity_id=data["entity"],
            expected_power=data.get("expected_power", 0),
            only_from_solar=data.get("only_from_solar", False),
            power_sensor_id=data.get("power_sensor"),
            priority=data.get("priority", 1),
            shed_priority=data.get("shed_priority"),
            restore_priority=data.get("restore_priority"),
            actuator_entity_ids=actuators,
            hvac_mode_on=data.get("hvac_mode_on", "heat"),
            ownership=ownership,
            ownership_until=(
                float(data["ownership_until"])
                if isinstance(data.get("ownership_until"), (int, float))
                and not isinstance(data.get("ownership_until"), bool)
                else None
            ),
        )


class PowerModel:
    """Tracks logical loads for one whole-house coordinator."""

    def __init__(self) -> None:
        self._devices: dict[str, ManagedDevice] = {}

    def add_device(self, device: ManagedDevice) -> None:
        self._devices[device.device_id] = device

    def get_device(self, device_id: str) -> ManagedDevice | None:
        return self._devices.get(device_id)

    def get_sorted_devices(self) -> list[ManagedDevice]:
        """Return devices sorted by normal-start priority (lowest first)."""
        return sorted(self._devices.values(), key=lambda d: (d.priority, d.device_id))

    def get_shed_devices(self) -> list[ManagedDevice]:
        """Return devices sorted by independent safety-shed rank."""
        return sorted(
            self._devices.values(),
            key=lambda d: (
                d.shed_priority if d.shed_priority is not None else d.priority,
                d.device_id,
            ),
        )

    def get_sorted_devices_reversed(self) -> list[ManagedDevice]:
        """Legacy reverse-start order, retained for emergency compatibility."""
        return sorted(
            self._devices.values(),
            key=lambda d: (d.priority, d.device_id),
            reverse=True,
        )

    def get_on_devices(self) -> list[ManagedDevice]:
        return [d for d in self._devices.values() if d.is_on is True]

    def get_off_devices(self) -> list[ManagedDevice]:
        return [d for d in self._devices.values() if d.is_on is False]

    @property
    def total_measured_power(self) -> float:
        return sum(
            d.measured_power
            for d in self._devices.values()
            if d.is_on is True and d.measured_power_valid
        )

    @property
    def total_expected_power(self) -> int:
        return sum(
            d.expected_power
            for d in self._devices.values()
            if d.is_on is True and not d.pause_active
        )

    def all_devices(self) -> list[ManagedDevice]:
        return list(self._devices.values())
