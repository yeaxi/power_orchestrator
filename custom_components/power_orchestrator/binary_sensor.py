"""Binary sensor platform for Power Orchestrator."""

from __future__ import annotations

from typing import Any, cast

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PowerOrchestratorCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up load-shedding safety diagnostics."""
    del hass
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        raise RuntimeError("Power Orchestrator runtime data is unavailable")
    coordinator = runtime.coordinator
    async_add_entities(
        [
            PowerOrchestratorGridOkSensor(coordinator, entry),
            PowerOrchestratorFaultSensor(coordinator, entry),
            PowerOrchestratorActionJournalHealthySensor(coordinator, entry),
        ]
    )


class PowerOrchestratorGridOkSensor(CoordinatorEntity, BinarySensorEntity):  # type: ignore[misc]
    """Grid-safety source state."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.POWER
    _attr_translation_key = "grid_ok"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_binary_sensor_grid_ok"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Power Orchestrator",
            "manufacturer": "Power Orchestrator",
            "model": "v0.5.0",
        }

    @property
    def _power_coordinator(self) -> PowerOrchestratorCoordinator:
        return cast(PowerOrchestratorCoordinator, self.coordinator)

    @property
    def available(self) -> bool:
        return bool(self._power_coordinator.grid_safety_source_configured)

    @property
    def is_on(self) -> bool:
        return cast(bool, self._power_coordinator.grid_ok)


class _DiagnosticSensorBase(CoordinatorEntity, BinarySensorEntity):  # type: ignore[misc]
    """Base class for persisted safety diagnostics."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: Any, entry: ConfigEntry, suffix: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_binary_sensor_{suffix}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Power Orchestrator",
            "manufacturer": "Power Orchestrator",
            "model": "v0.5.0",
        }

    @property
    def _power_coordinator(self) -> PowerOrchestratorCoordinator:
        return cast(PowerOrchestratorCoordinator, self.coordinator)

    @property
    def available(self) -> bool:
        return True
class PowerOrchestratorFaultSensor(_DiagnosticSensorBase):
    """True when at least one logical device has a persistent fault."""

    _attr_translation_key = "faulted"

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "faulted")

    @property
    def is_on(self) -> bool:
        return bool((self._power_coordinator.data or {}).get("faulted_devices", ()))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self._power_coordinator.data or {}
        return {
            "device_ids": list(data.get("faulted_devices", ())),
            "device_reasons": dict(data.get("fault_reasons", {})),
        }


class PowerOrchestratorActionJournalHealthySensor(_DiagnosticSensorBase):
    """True only when journal-backed safety state is healthy."""

    _attr_translation_key = "action_journal_healthy"

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "action_journal_healthy")

    @property
    def is_on(self) -> bool:
        data = self._power_coordinator.data or {}
        return not (data.get("action_journal_invalid", False) or data.get("journal_persistence_blocked", False))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self._power_coordinator.data or {}
        return {
            "unresolved_count": data.get("journal_unresolved_count", 0),
            "invalid": data.get("action_journal_invalid", False),
            "persistence_blocked": data.get("journal_persistence_blocked", False),
        }
