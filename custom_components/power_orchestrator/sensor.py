"""Sensor platform for Power Orchestrator."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors."""
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        raise RuntimeError("Power Orchestrator runtime data is unavailable")
    coordinator = runtime.coordinator
    async_add_entities(
        [
            PowerOrchestratorStatusSensor(coordinator, entry),
            PowerOrchestratorCurrentLoadSensor(coordinator, entry),
            PowerOrchestratorAverageLoadSensor(coordinator, entry),
            PowerOrchestratorAvailableCapacitySensor(coordinator, entry),
            PowerOrchestratorLastActionSensor(coordinator, entry),
        ]
    )


class PowerOrchestratorSensorBase(CoordinatorEntity, SensorEntity):
    """Base sensor for Power Orchestrator."""

    _attr_has_entity_name = True
    _requires_valid_load = False

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Power Orchestrator",
            "manufacturer": "Power Orchestrator",
            "model": "v0.5.0",
        }

    @property
    def _coordinator(self):
        return self.coordinator

    @property
    def available(self) -> bool:
        """Expose numeric telemetry only when its source is safe."""
        if self._requires_valid_load:
            return bool(self.coordinator.load_sensor_valid)
        return True


class PowerOrchestratorStatusSensor(PowerOrchestratorSensorBase):
    """Current orchestrator status."""

    _attr_translation_key = "status"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_sensor_status"
        self._attr_icon = "mdi:heart-pulse"

    @property
    def native_value(self) -> str:
        return self.coordinator.status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "mode": self.coordinator.mode,
            "grid_ok": self.coordinator.grid_ok,
            "grid_safety_source_configured": self.coordinator.grid_safety_source_configured,
            "load_sensor_valid": self.coordinator.load_sensor_valid,
            "load_sensor_reason": self.coordinator.load_sensor_reason,
            "startup_safe": self.coordinator.startup_safe,
            "pending_start_power": self.coordinator.pending_start_power,
            "faulted_devices": list((self.coordinator.data or {}).get("faulted_devices", ())),
            "recovery_blocked_devices": list(
                (self.coordinator.data or {}).get("recovery_blocked_devices", ())
            ),
            "fault_reasons": dict((self.coordinator.data or {}).get("fault_reasons", {})),
            "safety_fault_reason": (self.coordinator.data or {}).get("safety_fault_reason"),
        }


class PowerOrchestratorCurrentLoadSensor(PowerOrchestratorSensorBase):
    """Current load sensor."""

    _requires_valid_load = True

    _attr_translation_key = "current_load"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_sensor_current_load"
        self._attr_icon = "mdi:flash"

    @property
    def native_value(self) -> float | None:
        return self.coordinator.current_load


class PowerOrchestratorAverageLoadSensor(PowerOrchestratorSensorBase):
    """Average load over period."""

    _requires_valid_load = True

    _attr_translation_key = "average_load"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_sensor_average_load"
        self._attr_icon = "mdi:chart-bell-curve"

    @property
    def native_value(self) -> float | None:
        return self.coordinator.average_load


class PowerOrchestratorAvailableCapacitySensor(PowerOrchestratorSensorBase):
    """Available capacity before hitting the limit."""

    _requires_valid_load = True

    _attr_translation_key = "available_capacity"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_sensor_available_capacity"
        self._attr_icon = "mdi:battery-positive"

    @property
    def native_value(self) -> float | None:
        return self.coordinator.available_capacity


class PowerOrchestratorLastActionSensor(PowerOrchestratorSensorBase):
    """Last action text."""

    _attr_translation_key = "last_action"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_sensor_last_action"
        self._attr_icon = "mdi:clipboard-text-clock"

    @property
    def native_value(self) -> str:
        return self.coordinator.last_action