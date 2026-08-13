"""Logical load model for the load-shedding controller."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class ManagedDevice:
    """A logical optional load that the controller may switch off."""

    device_id: str
    name: str
    entity_id: str
    expected_power: int = 0
    power_sensor_id: str | None = None
    priority: int = 1
    shed_priority: int | None = None
    actuator_entity_ids: tuple[str, ...] = ()
    # Per-load opt-in for guarded restore. Off by default; only loads with this
    # set may ever be re-enabled by the planner after it shed them.
    restore_enabled: bool = False

    # Runtime state is always reconciled from Home Assistant telemetry.
    is_on: bool | None = None
    measured_power: float = 0.0
    measured_power_valid: bool = False
    measured_power_reason: str = "not_sampled"
    pause_until: float | None = None
    last_turn_off_time: float | None = None

    @property
    def control_entity_ids(self) -> tuple[str, ...]:
        """Return every physical member of this logical load exactly once."""
        return tuple(dict.fromkeys((self.entity_id, *self.actuator_entity_ids)))

    @property
    def pause_active(self) -> bool:
        """Return whether the load is temporarily protected from rapid cycling."""
        return self.pause_until is not None and time.time() < self.pause_until

    def to_dict(self) -> dict[str, Any]:
        """Serialize configuration and bounded diagnostic projections."""
        return {
            "device_id": self.device_id,
            "name": self.name,
            "entity": self.entity_id,
            "expected_power": self.expected_power,
            "power_sensor": self.power_sensor_id,
            "priority": self.priority,
            "shed_priority": self.shed_priority,
            "actuators": list(self.actuator_entity_ids),
            "restore_enabled": self.restore_enabled,
            "is_on": self.is_on,
            "measured_power": self.measured_power if self.measured_power_valid else None,
            "measured_power_valid": self.measured_power_valid,
            "measured_power_reason": self.measured_power_reason,
            "pause_until": self.pause_until,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ManagedDevice":
        """Create a device from a normalized current or legacy config record.

        Unknown legacy policy fields are deliberately ignored.  In particular,
        the runtime model contains only device state and shedding metadata; no activation policy.
        """
        raw_actuators = data.get("actuators", ())
        if isinstance(raw_actuators, str):
            raw_actuators = (raw_actuators,)
        if not isinstance(raw_actuators, (list, tuple)):
            raw_actuators = ()
        actuators = tuple(
            value for value in raw_actuators if isinstance(value, str) and value
        )

        def finite_timestamp(value: Any) -> float | None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            converted = float(value)
            return converted if math.isfinite(converted) else None

        expected_raw = data.get("expected_power", 0)
        try:
            expected_power = int(float(expected_raw))
        except (TypeError, ValueError):
            expected_power = 0
        expected_power = max(0, min(expected_power, 50000))

        priority_raw = data.get("priority", 1)
        try:
            priority = max(1, int(float(priority_raw)))
        except (TypeError, ValueError):
            priority = 1
        shed_raw = data.get("shed_priority")
        try:
            shed_priority = max(1, int(float(shed_raw))) if shed_raw is not None else None
        except (TypeError, ValueError):
            shed_priority = None

        entity_id = data.get("entity")
        device_id = data.get("device_id")
        name = data.get("name")
        if not isinstance(entity_id, str) or not entity_id:
            raise ValueError("device record is missing device_id")
        if not isinstance(device_id, str) or not device_id:
            raise ValueError("device record is missing device_id")
        if not isinstance(name, str) or not name:
            raise ValueError("device record is missing name")

        return cls(
            device_id=device_id,
            name=name,
            entity_id=entity_id,
            expected_power=expected_power,
            power_sensor_id=(
                data.get("power_sensor")
                if isinstance(data.get("power_sensor"), str)
                else None
            ),
            priority=priority,
            shed_priority=shed_priority,
            actuator_entity_ids=actuators,
            restore_enabled=bool(data.get("restore_enabled", False)),
            pause_until=finite_timestamp(data.get("pause_until")),
        )


class PowerModel:
    """Tracks logical loads for one whole-house controller."""

    def __init__(self) -> None:
        self._devices: dict[str, ManagedDevice] = {}

    def add_device(self, device: ManagedDevice) -> None:
        self._devices[device.device_id] = device

    def get_device(self, device_id: str) -> ManagedDevice | None:
        return self._devices.get(device_id)

    def get_shed_devices(self) -> list[ManagedDevice]:
        """Return configured loads in deterministic shedding order."""
        return sorted(
            self._devices.values(),
            key=lambda device: (
                device.shed_priority
                if device.shed_priority is not None
                else device.priority,
                device.device_id,
            ),
        )

    def get_sorted_devices(self) -> list[ManagedDevice]:
        """Compatibility alias for callers that need configured priority order."""
        return self.get_shed_devices()

    def get_sorted_devices_reversed(self) -> list[ManagedDevice]:
        """Return the inverse deterministic order for emergency iteration."""
        return list(reversed(self.get_shed_devices()))

    def get_on_devices(self) -> list[ManagedDevice]:
        return [device for device in self._devices.values() if device.is_on is True]

    def get_off_devices(self) -> list[ManagedDevice]:
        return [device for device in self._devices.values() if device.is_on is False]

    @property
    def total_measured_power(self) -> float:
        return sum(
            device.measured_power
            for device in self._devices.values()
            if device.is_on is True and device.measured_power_valid
        )

    @property
    def total_expected_power(self) -> int:
        return sum(
            device.expected_power
            for device in self._devices.values()
            if device.is_on is True and not device.pause_active
        )

    def all_devices(self) -> list[ManagedDevice]:
        return list(self._devices.values())
