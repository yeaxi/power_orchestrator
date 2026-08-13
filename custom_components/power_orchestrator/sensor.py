"""Sensor platform for Power Orchestrator."""

from __future__ import annotations

from typing import Any, Optional, cast

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PowerOrchestratorCoordinator

# Entities are pushed from a single coordinator and perform no per-entity I/O,
# so Home Assistant does not need to serialize their updates.
PARALLEL_UPDATES = 0

_MAX_AUDIT_HISTORY_ATTRIBUTES = 12


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
            PowerOrchestratorReasonCodeSensor(coordinator, entry),
            PowerOrchestratorLastOperationSensor(coordinator, entry),
        ]
    )


class PowerOrchestratorSensorBase(CoordinatorEntity, SensorEntity):  # type: ignore[misc]
    """Base sensor for Power Orchestrator."""

    _attr_has_entity_name = True
    _requires_valid_load = False

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Power Orchestrator",
            "manufacturer": "Power Orchestrator",
            "model": "v0.5.0",
        }

    @property
    def _coordinator(self) -> PowerOrchestratorCoordinator:
        return cast(PowerOrchestratorCoordinator, self.coordinator)

    @property
    def available(self) -> bool:
        """Expose numeric telemetry only when its source is safe."""
        if self._requires_valid_load:
            return bool(self._coordinator.load_sensor_valid)
        return True


class PowerOrchestratorStatusSensor(PowerOrchestratorSensorBase):
    """Current orchestrator status."""

    _attr_translation_key = "status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_sensor_status"

    @property
    def native_value(self) -> str:
        return cast(str, self._coordinator.status)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "mode": self._coordinator.mode,
            "grid_ok": self._coordinator.grid_ok,
            "grid_safety_source_configured": self._coordinator.grid_safety_source_configured,
            "grid_safety_source_available": self._coordinator.grid_safety_source_available,
            "load_sensor_valid": self._coordinator.load_sensor_valid,
            "load_sensor_reason": self._coordinator.load_sensor_reason,
            "startup_safe": self._coordinator.startup_safe,
            "faulted_devices": list((self._coordinator.data or {}).get("faulted_devices", ())),
            "quarantined_devices": list(
                (self._coordinator.data or {}).get("quarantined_devices", ())
            ),
            "fault_reasons": dict((self._coordinator.data or {}).get("fault_reasons", {})),
            "safety_fault_reason": (self._coordinator.data or {}).get("safety_fault_reason"),
            "shed_rejection_counts": dict(
                (self._coordinator.data or {}).get("shed_rejection_counts", {})
            ),
            "shed_rejection_total": (self._coordinator.data or {}).get("shed_rejection_total", 0),
            "shed_rejection_truncated": (self._coordinator.data or {}).get(
                "shed_rejection_truncated", 0
            ),
            "shed_rejection_devices": list(
                (self._coordinator.data or {}).get("shed_rejection_devices", ())
            ),
            "shed_rejection_evaluated_at": (self._coordinator.data or {}).get(
                "shed_rejection_evaluated_at"
            ),
            "pending_restore_ids": list(
                (self._coordinator.data or {}).get("pending_restore_ids", ())
            ),
            "pending_restore_names": list(
                (self._coordinator.data or {}).get("pending_restore_names", ())
            ),
        }


class PowerOrchestratorCurrentLoadSensor(PowerOrchestratorSensorBase):
    """Current load sensor."""

    _requires_valid_load = True

    _attr_translation_key = "current_load"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_sensor_current_load"

    @property
    def native_value(self) -> float | None:
        return cast(Optional[float], self._coordinator.current_load)


class PowerOrchestratorAverageLoadSensor(PowerOrchestratorSensorBase):
    """Average load over period."""

    _requires_valid_load = True

    _attr_translation_key = "average_load"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_sensor_average_load"

    @property
    def native_value(self) -> float | None:
        return cast(Optional[float], self._coordinator.average_load)


class PowerOrchestratorAvailableCapacitySensor(PowerOrchestratorSensorBase):
    """Available capacity before hitting the limit."""

    _requires_valid_load = True

    _attr_translation_key = "available_capacity"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_sensor_available_capacity"

    @property
    def native_value(self) -> float | None:
        return cast(Optional[float], self._coordinator.available_capacity)


class PowerOrchestratorLastActionSensor(PowerOrchestratorSensorBase):
    """Last action text."""

    _attr_translation_key = "last_action"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_sensor_last_action"

    @property
    def native_value(self) -> str:
        return cast(str, self._coordinator.last_action)


class PowerOrchestratorReasonCodeSensor(PowerOrchestratorSensorBase):
    """Typed policy/safety reason for the current decision."""

    _attr_translation_key = "reason_code"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_sensor_reason_code"

    @property
    def native_value(self) -> str:
        return cast(str, self._coordinator.reason_code)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self._coordinator.data or {}
        return {
            "status": data.get("status"),
            "policy_phase": data.get("policy_phase"),
            "safety_fault_reason": data.get("safety_fault_reason"),
            "load_sensor_reason": data.get("load_sensor_reason"),
        }


class PowerOrchestratorLastOperationSensor(PowerOrchestratorSensorBase):
    """Last guarded action result and bounded journal diagnostics."""

    _attr_translation_key = "last_operation"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_sensor_last_operation"

    @property
    def native_value(self) -> str:
        return str((self._coordinator.data or {}).get("last_operation_result", "none"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self._coordinator.data or {}
        raw_history = data.get("audit_history", [])
        history = [dict(item) for item in raw_history if isinstance(item, dict)]
        history_tail = history[-_MAX_AUDIT_HISTORY_ATTRIBUTES:]
        return {
            "action_id": data.get("last_action_id"),
            "operation_id": data.get("last_operation_id"),
            "journal_unresolved_count": data.get("journal_unresolved_count", 0),
            "action_journal_invalid": data.get("action_journal_invalid", False),
            "journal_persistence_blocked": data.get("journal_persistence_blocked", False),
            "audit_history": history_tail,
            "audit_history_total": len(history),
            "audit_history_truncated": max(0, len(history) - len(history_tail)),
        }
