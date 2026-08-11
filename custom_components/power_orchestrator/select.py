"""Select platform for Power Orchestrator."""

from typing import Any, cast

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MODE_AUTO, MODE_OFF
from .coordinator import PowerOrchestratorCoordinator

# The single mode select drives actions through the coordinator, not direct
# per-entity I/O, so its updates do not need to be serialized.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up select entities."""
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        raise RuntimeError("Power Orchestrator runtime data is unavailable")
    coordinator = runtime.coordinator
    async_add_entities(
        [
            PowerOrchestratorModeSelect(coordinator, entry),
        ]
    )


class PowerOrchestratorModeSelect(CoordinatorEntity, SelectEntity):  # type: ignore[misc]
    """Mode selector: auto / off."""

    _attr_has_entity_name = True
    _attr_translation_key = "mode"

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_select_mode"
        self._attr_options = [MODE_AUTO, MODE_OFF]
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
    def current_option(self) -> str:
        return cast(str, self._power_coordinator.mode)

    async def async_select_option(self, option: str) -> None:
        """Change the mode."""
        if option not in self._attr_options:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_select_option",
                translation_placeholders={"option": option},
            )
        await self._power_coordinator.async_set_mode(option)
        self.async_write_ha_state()