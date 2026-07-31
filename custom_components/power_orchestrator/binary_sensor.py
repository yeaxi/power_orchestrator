"""Binary sensor platform for Power Orchestrator."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensors."""
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        raise RuntimeError("Power Orchestrator runtime data is unavailable")
    coordinator = runtime.coordinator
    async_add_entities(
        [
            PowerOrchestratorGridOkSensor(coordinator, entry),
            PowerOrchestratorFaultSensor(coordinator, entry),
            PowerOrchestratorRecoveryBlockedSensor(coordinator, entry),
        ]
    )


class PowerOrchestratorGridOkSensor(CoordinatorEntity, BinarySensorEntity):
    """Grid OK binary sensor."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.POWER
    _attr_translation_key = "grid_ok"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_binary_sensor_grid_ok"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Power Orchestrator",
            "manufacturer": "Power Orchestrator",
            "model": "v0.5.0",
        }

    @property
    def available(self) -> bool:
        """A missing safety source is configuration fault, not grid loss."""
        return bool(self.coordinator.grid_safety_source_configured)

    @property
    def is_on(self) -> bool:
        return self.coordinator.grid_ok


class _QuarantineSensorBase(CoordinatorEntity, BinarySensorEntity):
    """Base class for typed persisted safety-state diagnostics."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:alert-circle"

    def __init__(self, coordinator, entry, suffix: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_binary_sensor_{suffix}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Power Orchestrator",
            "manufacturer": "Power Orchestrator",
            "model": "v0.5.0",
        }

    @property
    def available(self) -> bool:
        return True


class PowerOrchestratorFaultSensor(_QuarantineSensorBase):
    """True when at least one logical device has a persistent fault."""

    _attr_translation_key = "faulted"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "faulted")

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data or {}
        return bool(data.get("faulted_devices", ()))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        return {
            "device_ids": list(data.get("faulted_devices", ())),
            "device_reasons": dict(data.get("fault_reasons", {})),
        }


class PowerOrchestratorRecoveryBlockedSensor(_QuarantineSensorBase):
    """True when at least one logical device is blocked from recovery."""

    _attr_translation_key = "recovery_blocked"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "recovery_blocked")

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data or {}
        return bool(data.get("recovery_blocked_devices", ()))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        return {
            "device_ids": list(data.get("recovery_blocked_devices", ())),
            "device_reasons": dict(data.get("fault_reasons", {})),
            "next_restore_target": data.get("next_restore_target"),
        }
