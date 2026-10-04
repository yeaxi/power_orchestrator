"""Logical load model for the load-shedding controller."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

from .device_configuration import model_configuration
from .device_configuration import parse_battery_min_soc as parse_battery_min_soc


def _finite_timestamp(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    converted = float(value)
    return converted if math.isfinite(converted) else None


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
    command_entity_id: str | None = None
    readback_entity_ids: tuple[str, ...] = ()
    emergency_off_entity_ids: tuple[str, ...] = ()
    # None keeps the grid-loss all-stop. A value lets the load run on battery
    # while charge is at least this percentage; its owner decides resumption.
    battery_min_soc: float | None = None

    # Runtime state is always reconciled from Home Assistant telemetry.
    is_on: bool | None = None
    measured_power: float = 0.0
    measured_power_valid: bool = False
    measured_power_reason: str = "not_sampled"
    pause_until: float | None = None
    last_turn_off_time: float | None = None

    @property
    def control_entity_ids(self) -> tuple[str, ...]:
        """Compatibility projection of every configured physical member."""
        return tuple(dict.fromkeys((self.entity_id, *self.actuator_entity_ids)))

    @property
    def command_entity(self) -> str:
        """Return the single normal command target for this logical load."""
        if self.command_entity_id:
            return self.command_entity_id
        climate = next(
            (entity for entity in self.actuator_entity_ids if entity.startswith("climate.")),
            None,
        )
        return climate or self.entity_id

    @property
    def readback_entities(self) -> tuple[str, ...]:
        """Return required readback members, falling back to legacy configuration."""
        if self.readback_entity_ids:
            return tuple(dict.fromkeys(self.readback_entity_ids))
        if self.command_entity.startswith("climate.") and self.entity_id != self.command_entity:
            return (self.entity_id,)
        return self.control_entity_ids

    @property
    def emergency_off_entities(self) -> tuple[str, ...]:
        """Return fallback OFF targets used only after the normal command fails."""
        if self.emergency_off_entity_ids:
            return tuple(dict.fromkeys(self.emergency_off_entity_ids))
        if self.entity_id != self.command_entity:
            return (self.entity_id,)
        return ()

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
            "command_entity": self.command_entity,
            "readback_entities": list(self.readback_entities),
            "emergency_off_entities": list(self.emergency_off_entities),
            "battery_min_soc": self.battery_min_soc,
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
        return cls(
            **model_configuration(data),
            pause_until=_finite_timestamp(data.get("pause_until")),
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
                device.shed_priority if device.shed_priority is not None else device.priority,
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
