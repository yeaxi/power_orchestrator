"""Config flow for Power Orchestrator — v2 with correct discovery."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_ADD_ANOTHER,
    CONF_ADD_CUSTOM_DEVICE,
    CONF_AVERAGING_PERIOD,
    CONF_BATTERY_SOC,
    CONF_BATTERY_THRESHOLD,
    CONF_DEVICE_ACTUATORS,
    CONF_DEVICE_ENTITY,
    CONF_DEVICE_EXPECTED_POWER,
    CONF_DEVICE_HVAC_MODE_ON,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_DEVICE_ONLY_SOLAR,
    CONF_DEVICE_POWER_SENSOR,
    CONF_DEVICES,
    CONF_DISCOVERED_DEVICES,
    CONF_GRID_LOSS_MODE,
    CONF_GRID_LOSS_SENSOR,
    CONF_HYSTERESIS,
    CONF_LOAD_SENSOR,
    CONF_MAX_LOAD,
    CONF_PAUSE_PERIOD,
    CONF_PRIORITY,
    CONF_RESTORE_PRIORITY,
    CONF_SAFETY_RESERVE,
    CONF_SHED_PRIORITY,
    CONF_THRESHOLD_COUNT,
    CONF_THRESHOLDS,
    DEFAULT_AVERAGING_PERIOD,
    DEFAULT_HARD_INTERLOCK,
    DEFAULT_HYSTERESIS,
    DEFAULT_PAUSE_PERIOD,
    DEFAULT_SAFETY_RESERVE,
    DOMAIN,
    GRID_LOSS_MODE_SENSOR,
    GRID_LOSS_MODE_THRESHOLD,
    MAX_CUSTOM_THRESHOLDS,
)
from .forecast import resolve_current_power_forecast_entity
from .policy import validate_threshold_pair

_LOGGER = logging.getLogger(__name__)


def _gen_id() -> str:
    import uuid
    return uuid.uuid4().hex[:8]


async def _discover_energy(hass):
    """Discover sensors from Energy Dashboard."""
    result: dict[str, Any] = {
        "grid_power": None,
        "solar_power": None,
        "solar_forecast_entity": None,
        "solar_forecast_entry": None,
        "battery_soc": None,
        "battery_power": None,
        "devices": [],
    }
    try:
        from homeassistant.components.energy import async_get_manager
        manager = await async_get_manager(hass)
        data = manager.data
        if not data:
            return result

        for src in data.get("energy_sources", []):
            t = src.get("type")
            if t == "grid":
                result["grid_power"] = (
                    src.get("stat_rate") or
                    src.get("power_config", {}).get("stat_rate")
                )
            elif t == "solar":
                result["solar_power"] = src.get("stat_rate")
                forecast_entries = src.get("config_entry_solar_forecast") or []
                for ce_id in forecast_entries:
                    if result["solar_forecast_entry"] is None:
                        result["solar_forecast_entry"] = ce_id
                    entity_id = resolve_current_power_forecast_entity(hass, ce_id)
                    if entity_id:
                        result["solar_forecast_entry"] = ce_id
                        result["solar_forecast_entity"] = entity_id
                        break
            elif t == "battery":
                result["battery_soc"] = src.get("stat_soc")
                result["battery_power"] = src.get("stat_rate")

        for dev in data.get("device_consumption", []):
            entity_id = dev.get("stat_consumption")
            if entity_id:
                result["devices"].append({
                    "entity_id": entity_id,
                    "name": dev.get("name") or None,
                    "power_sensor": _sensor_entity_id(dev.get("stat_rate")),
                })
    except Exception as exc:
        _LOGGER.debug("Energy discovery failed: %s", exc)

    return result


def _friendly(hass, eid):
    """Get friendly name or fallback."""
    if not eid:
        return ""
    s = hass.states.get(eid)
    if not s:
        return eid
    name = (s.attributes or {}).get("friendly_name")
    return name if isinstance(name, str) and name else eid


def _sensor_entity_id(value: Any) -> str | None:
    """Return a valid sensor entity ID or None.

    Energy Dashboard's ``stat_rate`` is optional and is telemetry only.  Do
    not allow a malformed or non-sensor value to become a runtime sensor
    reference; the user can select a replacement explicitly in the flow.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value.startswith("sensor.") or len(value) <= len("sensor."):
        return None
    return value


_CANONICAL_THRESHOLD_INPUTS: tuple[dict[str, float], ...] = (
    {"power_limit": 6500.0, "duration_s": 300.0},
    {"power_limit": 7000.0, "duration_s": 30.0},
    {"power_limit": 8000.0, "duration_s": 5.0},
)


def _threshold_field(index: int, kind: str) -> str:
    """Return the stable UI key for one custom threshold pair."""
    return f"threshold_{index}_{kind}"


def _threshold_defaults(value: Any) -> list[dict[str, float]]:
    """Normalize persisted threshold pairs for form defaults."""
    if not isinstance(value, (list, tuple)) or not value:
        return [dict(item) for item in _CANONICAL_THRESHOLD_INPUTS]
    normalized: list[dict[str, float]] = []
    for item in value[:MAX_CUSTOM_THRESHOLDS]:
        if not isinstance(item, Mapping):
            continue
        try:
            power_limit = float(item.get("power_limit", item.get("limit_w")))
            duration_s = float(item.get("duration_s", item.get("time_s")))
        except (TypeError, ValueError):
            continue
        if math.isfinite(power_limit) and math.isfinite(duration_s):
            normalized.append(
                {"power_limit": power_limit, "duration_s": duration_s}
            )
    return normalized or [dict(item) for item in _CANONICAL_THRESHOLD_INPUTS]


def _parse_threshold_input(
    user_input: Mapping[str, Any],
    defaults: list[dict[str, float]] | None = None,
) -> tuple[list[dict[str, float]] | None, str | None]:
    """Parse one-to-ten threshold pairs and return a localized error key."""
    default_pairs = defaults or [dict(item) for item in _CANONICAL_THRESHOLD_INPUTS]
    raw_count = user_input.get(CONF_THRESHOLD_COUNT, len(default_pairs))
    if isinstance(raw_count, bool):
        return None, "invalid_thresholds"
    try:
        count_float = float(raw_count)
    except (TypeError, ValueError):
        return None, "invalid_thresholds"
    if (
        not math.isfinite(count_float)
        or count_float != int(count_float)
        or not 1 <= int(count_float) <= MAX_CUSTOM_THRESHOLDS
    ):
        return None, "invalid_thresholds"
    count = int(count_float)

    parsed: list[dict[str, float]] = []
    previous_power = 0.0
    for index in range(1, count + 1):
        default = default_pairs[index - 1] if index <= len(default_pairs) else {}
        raw_power = user_input.get(
            _threshold_field(index, "power"), default.get("power_limit")
        )
        raw_duration = user_input.get(
            _threshold_field(index, "time"), default.get("duration_s")
        )
        if isinstance(raw_power, bool) or isinstance(raw_duration, bool):
            return None, "invalid_thresholds"
        try:
            power_limit = float(raw_power)
            duration_s = float(raw_duration)
            power_limit, duration_s = validate_threshold_pair(
                power_limit,
                duration_s,
                previous_power,
            )
        except (TypeError, ValueError):
            return None, "invalid_thresholds"
        parsed.append({"power_limit": power_limit, "duration_s": duration_s})
        previous_power = power_limit
    return parsed, None


def _threshold_form_fields(
    defaults: list[dict[str, float]] | None = None,
) -> dict[Any, Any]:
    """Build up to ten editable power/time pairs for HA's config form."""
    pairs = defaults or [dict(item) for item in _CANONICAL_THRESHOLD_INPUTS]
    fields: dict[Any, Any] = {
        vol.Required(
            CONF_THRESHOLD_COUNT,
            default=min(len(pairs), MAX_CUSTOM_THRESHOLDS),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1,
                max=MAX_CUSTOM_THRESHOLDS,
                mode="box",
            )
        )
    }
    for index in range(1, MAX_CUSTOM_THRESHOLDS + 1):
        default = pairs[index - 1] if index <= len(pairs) else None
        power_key = _threshold_field(index, "power")
        time_key = _threshold_field(index, "time")
        power_schema = (
            vol.Optional(power_key, default=default["power_limit"])
            if default is not None
            else vol.Optional(power_key)
        )
        time_schema = (
            vol.Optional(time_key, default=default["duration_s"])
            if default is not None
            else vol.Optional(time_key)
        )
        fields[power_schema] = selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1,
                max=DEFAULT_HARD_INTERLOCK,
                mode="box",
                unit_of_measurement="W",
            )
        )
        fields[time_schema] = selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1,
                max=86400,
                mode="box",
                unit_of_measurement="s",
            )
        )
    return fields


def _entity_id(value: Any, domains: frozenset[str]) -> str | None:
    """Return a syntactically valid entity ID in one of the allowed domains."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    domain, separator, object_id = value.partition(".")
    if not separator or domain not in domains or not object_id:
        return None
    return value


def _normalize_options_devices(value: Any) -> list[dict[str, Any]]:
    """Validate and normalize structured device mappings from Options Flow."""
    if not isinstance(value, list):
        raise ValueError("devices must be a list")

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_entities: set[str] = set()
    seen_priorities: set[int] = set()
    actuator_domains = frozenset({"switch", "light", "input_boolean", "climate"})
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise ValueError("device mapping must be an object")
        device_id = raw.get(CONF_DEVICE_ID)
        if not isinstance(device_id, str) or not device_id.strip():
            raise ValueError("device ID is required")
        device_id = device_id.strip()
        if device_id in seen_ids:
            raise ValueError("device IDs must be unique")

        entity_id = _entity_id(
            raw.get(CONF_DEVICE_ENTITY),
            frozenset({"switch", "light", "input_boolean"}),
        )
        if entity_id is None:
            raise ValueError("control entity must be valid")

        raw_actuators = raw.get(CONF_DEVICE_ACTUATORS, ())
        if raw_actuators in (None, ""):
            raw_actuators = ()
        if isinstance(raw_actuators, str):
            raw_actuators = (raw_actuators,)
        if not isinstance(raw_actuators, (list, tuple)):
            raise ValueError("actuators must be a list")
        actuator_ids: list[str] = []
        for raw_actuator in raw_actuators:
            actuator_id = _entity_id(raw_actuator, actuator_domains)
            if (
                actuator_id is None
                or actuator_id == entity_id
                or actuator_id in actuator_ids
            ):
                raise ValueError("logical actuator entities must be valid and unique")
            actuator_ids.append(actuator_id)
        all_entities = (entity_id, *actuator_ids)
        if any(control in seen_entities for control in all_entities):
            raise ValueError("control entities must be valid and unique")

        expected_power = raw.get(CONF_DEVICE_EXPECTED_POWER)
        if isinstance(expected_power, bool):
            raise ValueError("expected power must be finite")
        try:
            expected_power_number = float(expected_power or 0)
        except (TypeError, ValueError):
            raise ValueError("expected power must be finite") from None
        if not math.isfinite(expected_power_number) or not 1 <= expected_power_number <= 50000:
            raise ValueError("expected power is outside the allowed range")

        power_sensor = raw.get(CONF_DEVICE_POWER_SENSOR)
        if power_sensor in (None, ""):
            normalized_power_sensor = None
        else:
            normalized_power_sensor = _entity_id(power_sensor, frozenset({"sensor"}))
            if normalized_power_sensor is None:
                raise ValueError("power sensor must be a sensor entity")

        name = raw.get(CONF_DEVICE_NAME) or entity_id
        if not isinstance(name, str) or not name.strip():
            name = entity_id
        name = name.strip()

        def positive_integer(raw_value: Any, default: int, label: str) -> int:
            if raw_value is None:
                return default
            if isinstance(raw_value, bool):
                raise ValueError(f"{label} must be an integer")
            try:
                number = float(raw_value)
            except (TypeError, ValueError):
                raise ValueError(f"{label} must be an integer") from None
            if not math.isfinite(number) or number < 1 or number != int(number):
                raise ValueError(f"{label} must be a positive integer")
            return int(number)

        priority_int = positive_integer(raw.get(CONF_PRIORITY), index + 1, "priority")
        if priority_int in seen_priorities:
            raise ValueError("priorities must be unique")
        shed_priority = positive_integer(
            raw.get(CONF_SHED_PRIORITY), priority_int, "shed priority"
        )
        restore_priority_raw = raw.get(CONF_RESTORE_PRIORITY)
        restore_priority = (
            positive_integer(restore_priority_raw, priority_int, "restore priority")
            if restore_priority_raw is not None
            else None
        )

        only_from_solar = raw.get(CONF_DEVICE_ONLY_SOLAR, False)
        if not isinstance(only_from_solar, bool):
            raise ValueError("only_from_solar must be boolean")
        hvac_mode_on = raw.get(CONF_DEVICE_HVAC_MODE_ON, "heat")
        if not isinstance(hvac_mode_on, str) or not hvac_mode_on.strip():
            raise ValueError("hvac mode must be a non-empty string")

        normalized.append(
            {
                CONF_DEVICE_ID: device_id,
                CONF_DEVICE_NAME: name,
                CONF_DEVICE_ENTITY: entity_id,
                CONF_DEVICE_EXPECTED_POWER: int(math.ceil(expected_power_number)),
                CONF_DEVICE_POWER_SENSOR: normalized_power_sensor,
                CONF_DEVICE_ONLY_SOLAR: only_from_solar,
                CONF_PRIORITY: priority_int,
                CONF_DEVICE_ACTUATORS: actuator_ids,
                CONF_DEVICE_HVAC_MODE_ON: hvac_mode_on.strip(),
                CONF_SHED_PRIORITY: shed_priority,
                CONF_RESTORE_PRIORITY: restore_priority,
            }
        )
        seen_ids.add(device_id)
        seen_entities.update(all_entities)
        seen_priorities.add(priority_int)

    return normalized


# ── Config Flow ────────────────────────────────────────────────────


class PowerOrchestratorConfigFlow(  # type: ignore[call-arg]
    config_entries.ConfigFlow,
    domain=DOMAIN,
):
    """Power Orchestrator config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, Any] = {}
        self._devices: list[dict[str, Any]] = []
        self._devices_phase = "selection"
        self._pending_discovered: list[dict[str, Any]] = []
        self._pending_discovered_index = 0
        self._add_custom_device = False

    def _discovery_summary(self) -> str:
        lines = []
        if self._discovered.get("grid_power"):
            lines.append(f"✅ Grid power: {self._discovered['grid_power']}")
        else:
            lines.append("❌ Grid power: not found — select manually")
        if self._discovered.get("solar_power"):
            lines.append(f"✅ Solar power: {self._discovered['solar_power']}")
        if self._discovered.get("solar_forecast_entry"):
            # Try to get the config entry title
            ce = self.hass.config_entries.async_get_entry(self._discovered["solar_forecast_entry"])
            name = ce.title if ce else self._discovered["solar_forecast_entry"]
            forecast_entity = self._discovered.get("solar_forecast_entity")
            if forecast_entity:
                lines.append(f"✅ Solar estimated power: {forecast_entity} ({name})")
            else:
                lines.append(
                    "⚠️ Solar forecast entry found, but no exact estimated-power entity resolved "
                    "— solar-only devices stay off"
                )
        if self._discovered.get("battery_soc"):
            lines.append(f"✅ Battery SoC: {self._discovered['battery_soc']}")
        if self._discovered.get("battery_power"):
            lines.append(f"✅ Battery power: {self._discovered['battery_power']}")
        if self._discovered.get("devices"):
            lines.append(f"📦 Devices found: {len(self._discovered['devices'])}")
        return "\n".join(lines)

    # ── Step 1 ─────────────────────────────────────────────────────

    async def async_step_user(self, user_input=None):
        """Step 1: Pick sensors. Auto-discovered values are pre-filled."""
        if user_input is None:
            entries_fn = getattr(getattr(self.hass, "config_entries", None), "async_entries", None)
            existing_entries = entries_fn(DOMAIN) if callable(entries_fn) else []
            if isinstance(existing_entries, (list, tuple)) and existing_entries:
                return self.async_abort(reason="single_instance")
        if user_input is not None:
            self._discovered["grid_power"] = user_input.get("grid_power")
            self._discovered["solar_power"] = user_input.get("solar_power")
            # solar_forecast is a config entry ID (or empty string)
            forecast_entry = user_input.get("solar_forecast") or None
            self._discovered["solar_forecast_entry"] = forecast_entry
            self._discovered["solar_forecast_entity"] = (
                resolve_current_power_forecast_entity(self.hass, forecast_entry)
                if forecast_entry
                else None
            )
            self._discovered["battery_soc"] = user_input.get("battery_soc")
            self._discovered["battery_power"] = user_input.get("battery_power")
            return await self.async_step_load_monitoring()

        self._discovered = await _discover_energy(self.hass)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "grid_power",
                        default=self._discovered.get("grid_power") or "",
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor")
                    ),
                    vol.Optional(
                        "solar_power",
                        default=self._discovered.get("solar_power") or "",
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor")
                    ),
                    vol.Optional(
                        "solar_forecast",
                        default=self._discovered.get("solar_forecast_entry") or "",
                    ): selector.ConfigEntrySelector(
                        selector.ConfigEntrySelectorConfig(
                            integration="forecast_solar",
                        )
                    ),
                    vol.Optional(
                        "battery_soc",
                        default=self._discovered.get("battery_soc") or "",
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor")
                    ),
                    vol.Optional(
                        "battery_power",
                        default=self._discovered.get("battery_power") or "",
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor")
                    ),
                }
            ),
            description_placeholders={"summary": self._discovery_summary()},
        )

    # ── Step 2 ─────────────────────────────────────────────────────

    def _load_monitoring_form(self, errors=None):
        """Render load monitoring and up to ten custom threshold pairs."""
        default_sensor = self._discovered.get("grid_power", "")
        threshold_defaults = _threshold_defaults(
            self._discovered.get(CONF_THRESHOLDS)
        )
        fields: dict[Any, Any] = {
            vol.Required(
                CONF_LOAD_SENSOR,
                default=default_sensor,
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            vol.Required(CONF_MAX_LOAD, default=5000): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=100,
                    max=50000,
                    mode="box",
                    unit_of_measurement="W",
                )
            ),
            vol.Required(
                CONF_AVERAGING_PERIOD,
                default=DEFAULT_AVERAGING_PERIOD,
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=300,
                    mode="box",
                    unit_of_measurement="s",
                )
            ),
            vol.Required(
                CONF_SAFETY_RESERVE,
                default=DEFAULT_SAFETY_RESERVE,
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=5000,
                    mode="box",
                    unit_of_measurement="W",
                )
            ),
            vol.Required(
                CONF_HYSTERESIS,
                default=DEFAULT_HYSTERESIS,
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=5000,
                    mode="box",
                    unit_of_measurement="W",
                )
            ),
        }
        fields.update(_threshold_form_fields(threshold_defaults))
        return self.async_show_form(
            step_id="load_monitoring",
            data_schema=vol.Schema(fields),
            errors=errors or {},
            description_placeholders={
                "sensor_name": _friendly(self.hass, default_sensor),
            },
        )

    async def async_step_load_monitoring(self, user_input=None):
        """Step 2: Load monitoring and threshold policy settings."""
        if user_input is not None:
            thresholds, threshold_error = _parse_threshold_input(user_input)
            if threshold_error:
                return self._load_monitoring_form({"base": threshold_error})
            self._discovered["max_load"] = user_input.get(CONF_MAX_LOAD, 5000)
            self._discovered["averaging_period"] = user_input.get(
                CONF_AVERAGING_PERIOD,
                DEFAULT_AVERAGING_PERIOD,
            )
            self._discovered["safety_reserve"] = user_input.get(
                CONF_SAFETY_RESERVE,
                DEFAULT_SAFETY_RESERVE,
            )
            self._discovered["hysteresis"] = user_input.get(
                CONF_HYSTERESIS,
                DEFAULT_HYSTERESIS,
            )
            self._discovered[CONF_THRESHOLDS] = thresholds
            # Use the load sensor from this step (may differ from discovery).
            self._discovered["grid_power"] = user_input.get(CONF_LOAD_SENSOR)
            return await self.async_step_devices()

        return self._load_monitoring_form()

    def _device_name(self, device: dict[str, Any]) -> str:
        """Return a stable friendly name for a discovered or configured device."""
        entity_id = device.get(CONF_DEVICE_ENTITY) or device.get("entity_id")
        return (
            device.get(CONF_DEVICE_NAME)
            or _friendly(self.hass, entity_id)
            or entity_id
            or "Unnamed device"
        )

    def _device_selection_form(self, errors=None):
        """Show discovered Energy Dashboard devices for confirmation/removal."""
        candidates = [
            device
            for device in self._discovered.get("devices", [])
            if device.get("entity_id")
        ]
        options = [
            selector.SelectOptionDict(
                value=device["entity_id"],
                label=self._device_name(device),
            )
            for device in candidates
        ]
        fields = {}
        if options:
            fields[
                vol.Optional(
                    CONF_DISCOVERED_DEVICES,
                    default=[device["entity_id"] for device in candidates],
                )
            ] = selector.SelectSelector(
                selector.SelectSelectorConfig(options=options, multiple=True)
            )
        fields[
            vol.Optional(CONF_ADD_CUSTOM_DEVICE, default=not bool(options))
        ] = bool
        discovered = "\n".join(
            f"  • {self._device_name(device)}" for device in candidates
        ) or "  None found"
        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={
                "count": str(len(self._devices)),
                "discovered": discovered,
            },
        )

    def _device_config_form(self, candidate=None, errors=None):
        """Show control settings for one discovered or custom device."""
        candidate = candidate or {}
        candidate_name = self._device_name(candidate) if candidate else ""
        power_sensor = _sensor_entity_id(candidate.get(CONF_DEVICE_POWER_SENSOR)) or ""
        if power_sensor:
            power_sensor_status = (
                f"{candidate_name or 'Device'} — auto-discovered power sensor: "
                f"{_friendly(self.hass, power_sensor)} ({power_sensor}); "
                "select another sensor to override it or clear the field to disable telemetry"
            )
        else:
            power_sensor_status = (
                f"{candidate_name or 'Custom device'} — no power sensor was auto-discovered; "
                "select one manually if runtime telemetry is needed"
            )
        fields = {
            vol.Required(CONF_DEVICE_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=["switch", "light", "input_boolean"]
                )
            ),
            vol.Optional(CONF_DEVICE_NAME, default=candidate_name): selector.TextSelector(),
            vol.Required(
                CONF_DEVICE_EXPECTED_POWER,
                default=candidate.get(CONF_DEVICE_EXPECTED_POWER, 2000),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=50000,
                    mode="box",
                    unit_of_measurement="W",
                )
            ),
            vol.Optional(
                CONF_DEVICE_POWER_SENSOR,
                default=power_sensor,
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            vol.Optional(
                CONF_DEVICE_ACTUATORS,
                default=list(candidate.get(CONF_DEVICE_ACTUATORS, ())),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=["switch", "light", "input_boolean", "climate"],
                    multiple=True,
                )
            ),
            vol.Optional(
                CONF_DEVICE_HVAC_MODE_ON,
                default=candidate.get(CONF_DEVICE_HVAC_MODE_ON, "heat"),
            ): selector.TextSelector(),
            vol.Optional(CONF_DEVICE_ONLY_SOLAR, default=False): bool,
        }
        if not candidate:
            fields[vol.Required(CONF_ADD_ANOTHER, default=False)] = bool
        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={
                "count": str(len(self._devices)),
                "discovered": power_sensor_status,
            },
        )

    def _build_device(
        self,
        user_input: dict[str, Any],
        candidate: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create the persisted device record from a form submission."""
        candidate = candidate or {}
        entity_id = user_input.get(CONF_DEVICE_ENTITY)
        name = (
            user_input.get(CONF_DEVICE_NAME)
            or self._device_name(candidate)
            if candidate
            else user_input.get(CONF_DEVICE_NAME) or _friendly(self.hass, entity_id)
        )
        if CONF_DEVICE_POWER_SENSOR in user_input:
            power_sensor = _sensor_entity_id(user_input.get(CONF_DEVICE_POWER_SENSOR))
        else:
            power_sensor = _sensor_entity_id(candidate.get(CONF_DEVICE_POWER_SENSOR))
        return {
            CONF_DEVICE_ID: _gen_id(),
            CONF_DEVICE_NAME: name or entity_id,
            CONF_DEVICE_ENTITY: entity_id,
            CONF_DEVICE_EXPECTED_POWER: user_input.get(CONF_DEVICE_EXPECTED_POWER, 2000),
            CONF_DEVICE_POWER_SENSOR: power_sensor,
            CONF_DEVICE_ONLY_SOLAR: user_input.get(CONF_DEVICE_ONLY_SOLAR, False),
            CONF_DEVICE_ACTUATORS: user_input.get(CONF_DEVICE_ACTUATORS, []),
            CONF_DEVICE_HVAC_MODE_ON: user_input.get(CONF_DEVICE_HVAC_MODE_ON, "heat"),
        }

    # ── Step 3 ─────────────────────────────────────────────────────

    async def async_step_devices(self, user_input=None):
        """Step 3: Confirm discovered devices, then configure their controls."""
        if self._devices_phase == "selection":
            if user_input is None:
                return self._device_selection_form()

            candidates = {
                device["entity_id"]: device
                for device in self._discovered.get("devices", [])
                if device.get("entity_id")
            }
            selected_ids = user_input.get(CONF_DISCOVERED_DEVICES, []) or []
            if isinstance(selected_ids, str):
                selected_ids = [selected_ids]
            if any(entity_id not in candidates for entity_id in selected_ids):
                return self._device_selection_form(
                    errors={"base": "invalid_discovered_devices"}
                )

            self._pending_discovered = [
                candidates[entity_id] for entity_id in selected_ids
            ]
            self._pending_discovered_index = 0
            self._add_custom_device = bool(
                user_input.get(CONF_ADD_CUSTOM_DEVICE, False)
            )
            if self._pending_discovered:
                self._devices_phase = "discovered"
                return await self.async_step_devices()
            if self._add_custom_device:
                self._devices_phase = "custom"
                return await self.async_step_devices()
            return await self.async_step_priority()

        if self._devices_phase == "discovered":
            candidate = self._pending_discovered[self._pending_discovered_index]
            if user_input is None:
                return self._device_config_form(candidate)

            self._devices.append(self._build_device(user_input, candidate))
            self._pending_discovered_index += 1
            if self._pending_discovered_index < len(self._pending_discovered):
                return await self.async_step_devices()
            if self._add_custom_device:
                self._devices_phase = "custom"
                return await self.async_step_devices()
            return await self.async_step_priority()

        if user_input is None:
            return self._device_config_form()

        self._devices.append(self._build_device(user_input))
        if user_input.get(CONF_ADD_ANOTHER, False):
            return await self.async_step_devices()
        return await self.async_step_priority()

    # ── Step 4 ─────────────────────────────────────────────────────

    @staticmethod
    def _priority_field(index: int) -> str:
        """Return the form field for a one-based priority position."""
        return f"priority_{index + 1}"

    def _priority_form(self, errors=None):
        """Build one named selector for every priority position."""
        lines = "\n".join(
            f"  {i + 1}. {d.get(CONF_DEVICE_NAME) or _friendly(self.hass, d.get(CONF_DEVICE_ENTITY, ''))}"
            for i, d in enumerate(self._devices)
        ) or "  No optional devices configured"

        options = [
            selector.SelectOptionDict(
                value=d[CONF_DEVICE_ID],
                label=d.get(CONF_DEVICE_NAME)
                or _friendly(self.hass, d.get(CONF_DEVICE_ENTITY, ""))
                or d[CONF_DEVICE_ID],
            )
            for d in self._devices
        ]
        schema_fields = {}
        for index, device in enumerate(self._devices):
            schema_fields[
                vol.Required(
                    self._priority_field(index),
                    default=device[CONF_DEVICE_ID],
                )
            ] = selector.SelectSelector(
                selector.SelectSelectorConfig(options=options)
            )

        schema_fields[
            vol.Required(CONF_PAUSE_PERIOD, default=DEFAULT_PAUSE_PERIOD)
        ] = selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0,
                max=3600,
                mode="box",
                unit_of_measurement="s",
            )
        )
        return self.async_show_form(
            step_id="priority",
            data_schema=vol.Schema(schema_fields),
            errors=errors,
            description_placeholders={"device_list": lines},
        )

    async def async_step_priority(self, user_input=None):
        """Step 4: Assign named devices to priority positions."""
        if user_input is not None:
            selected_ids = [
                user_input.get(self._priority_field(index))
                for index in range(len(self._devices))
            ]
            device_by_id = {d[CONF_DEVICE_ID]: d for d in self._devices}
            valid_order = (
                len(selected_ids) == len(device_by_id)
                and all(device_id in device_by_id for device_id in selected_ids)
                and len(set(selected_ids)) == len(selected_ids)
            )
            if not valid_order:
                return self._priority_form(errors={"base": "invalid_priority_order"})

            self._devices = [device_by_id[device_id] for device_id in selected_ids]
            for index, device in enumerate(self._devices):
                device[CONF_PRIORITY] = index + 1
            self._pause_period = user_input.get(CONF_PAUSE_PERIOD, DEFAULT_PAUSE_PERIOD)
            return await self.async_step_grid_loss()

        return self._priority_form()

    # ── Step 5 ─────────────────────────────────────────────────────

    def _grid_loss_form(self, errors=None):
        """Render grid-loss form with explicit safety-source selectors."""
        bat_info = ""
        soc = self._discovered.get(CONF_BATTERY_SOC)
        if soc:
            s = self.hass.states.get(soc)
            if s:
                bat_info = f"Current battery SoC: {s.state}% ({_friendly(self.hass, soc)})"
        return self.async_show_form(
            step_id="grid_loss",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_GRID_LOSS_MODE, default=GRID_LOSS_MODE_SENSOR): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(value=GRID_LOSS_MODE_SENSOR, label="Grid loss sensor (binary sensor)"),
                                selector.SelectOptionDict(value=GRID_LOSS_MODE_THRESHOLD, label="Battery threshold (SoC %)"),
                            ],
                        )
                    ),
                    vol.Optional(CONF_GRID_LOSS_SENSOR): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="binary_sensor")
                    ),
                    vol.Optional(CONF_BATTERY_SOC, default=soc or ""): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor")
                    ),
                    vol.Optional(CONF_BATTERY_THRESHOLD, default=20): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=0, max=100, mode="box", unit_of_measurement="%")
                    ),
                }
            ),
            errors=errors or {},
            description_placeholders={"battery_info": bat_info},
        )

    async def async_step_grid_loss(self, user_input=None):
        """Step 5: Grid loss behavior."""
        if user_input is not None:
            mode = user_input.get(CONF_GRID_LOSS_MODE, GRID_LOSS_MODE_SENSOR)
            grid_sensor = user_input.get(CONF_GRID_LOSS_SENSOR)
            battery_soc = user_input.get(CONF_BATTERY_SOC) or self._discovered.get(CONF_BATTERY_SOC)
            if mode == GRID_LOSS_MODE_SENSOR and not grid_sensor:
                return self._grid_loss_form({"base": "missing_grid_loss_sensor"})
            if mode == GRID_LOSS_MODE_THRESHOLD and not battery_soc:
                return self._grid_loss_form({"base": "missing_battery_soc_sensor"})
            data = {
                CONF_LOAD_SENSOR: self._discovered.get("grid_power"),
                CONF_MAX_LOAD: self._discovered.get("max_load", 5000),
                CONF_AVERAGING_PERIOD: self._discovered.get("averaging_period", DEFAULT_AVERAGING_PERIOD),
                CONF_SAFETY_RESERVE: self._discovered.get("safety_reserve", DEFAULT_SAFETY_RESERVE),
                CONF_HYSTERESIS: self._discovered.get("hysteresis", DEFAULT_HYSTERESIS),
                CONF_THRESHOLDS: self._discovered.get(
                    CONF_THRESHOLDS,
                    [dict(item) for item in _CANONICAL_THRESHOLD_INPUTS],
                ),
                CONF_DEVICES: self._devices,
                CONF_PAUSE_PERIOD: self._pause_period,
                CONF_GRID_LOSS_MODE: mode,
            }
            if mode == GRID_LOSS_MODE_SENSOR:
                data[CONF_GRID_LOSS_SENSOR] = grid_sensor
            else:
                data[CONF_BATTERY_THRESHOLD] = user_input.get(CONF_BATTERY_THRESHOLD, 20)
                data[CONF_BATTERY_SOC] = battery_soc
            for key in ("solar_power", "solar_forecast_entity", "solar_forecast_entry", CONF_BATTERY_SOC, "battery_power"):
                val = self._discovered.get(key)
                if val and key not in data:
                    data[key] = val
            return self.async_create_entry(title="Power Orchestrator", data=data)

        return self._grid_loss_form()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return PowerOrchestratorOptionsFlow(config_entry)


class PowerOrchestratorOptionsFlow(config_entries.OptionsFlow):
    """Options flow for runtime thresholds and safety sources."""

    def __init__(self, config_entry):
        self._entry = config_entry

    def _current(self, key: str, default=None):
        options = getattr(self._entry, "options", {}) or {}
        data = getattr(self._entry, "data", {}) or {}
        return options.get(key, data.get(key, default))

    def _options_schema(self):
        """Build the options schema used for both initial and error forms."""
        threshold_defaults = _threshold_defaults(
            self._current(CONF_THRESHOLDS, None)
        )
        fields: dict[Any, Any] = {
            vol.Required(
                CONF_LOAD_SENSOR,
                default=self._current(CONF_LOAD_SENSOR, ""),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            vol.Optional(
                CONF_DEVICES,
                default=self._current(CONF_DEVICES, []),
            ): selector.ObjectSelector(),
            vol.Optional(
                "solar_power",
                default=self._current("solar_power", ""),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            vol.Optional(
                "solar_forecast_entry",
                default=self._current("solar_forecast_entry", ""),
            ): selector.ConfigEntrySelector(
                selector.ConfigEntrySelectorConfig(integration="forecast_solar")
            ),
            vol.Optional(
                "battery_power",
                default=self._current("battery_power", ""),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            vol.Required(
                CONF_MAX_LOAD,
                default=self._current(CONF_MAX_LOAD, 5000),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=100,
                    max=50000,
                    mode="box",
                    unit_of_measurement="W",
                )
            ),
            vol.Required(
                CONF_AVERAGING_PERIOD,
                default=self._current(
                    CONF_AVERAGING_PERIOD,
                    DEFAULT_AVERAGING_PERIOD,
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=300,
                    mode="box",
                    unit_of_measurement="s",
                )
            ),
            vol.Required(
                CONF_SAFETY_RESERVE,
                default=self._current(
                    CONF_SAFETY_RESERVE,
                    DEFAULT_SAFETY_RESERVE,
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=5000,
                    mode="box",
                    unit_of_measurement="W",
                )
            ),
            vol.Required(
                CONF_HYSTERESIS,
                default=self._current(CONF_HYSTERESIS, DEFAULT_HYSTERESIS),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=5000,
                    mode="box",
                    unit_of_measurement="W",
                )
            ),
            vol.Required(
                CONF_PAUSE_PERIOD,
                default=self._current(CONF_PAUSE_PERIOD, DEFAULT_PAUSE_PERIOD),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=3600,
                    mode="box",
                    unit_of_measurement="s",
                )
            ),
            vol.Required(
                CONF_GRID_LOSS_MODE,
                default=self._current(
                    CONF_GRID_LOSS_MODE,
                    GRID_LOSS_MODE_SENSOR,
                ),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(
                            value=GRID_LOSS_MODE_SENSOR,
                            label="Grid loss sensor (binary sensor)",
                        ),
                        selector.SelectOptionDict(
                            value=GRID_LOSS_MODE_THRESHOLD,
                            label="Battery threshold (SoC %)",
                        ),
                    ]
                )
            ),
            vol.Optional(
                CONF_GRID_LOSS_SENSOR,
                default=self._current(CONF_GRID_LOSS_SENSOR, ""),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="binary_sensor")
            ),
            vol.Optional(
                CONF_BATTERY_SOC,
                default=self._current(CONF_BATTERY_SOC, ""),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            vol.Optional(
                CONF_BATTERY_THRESHOLD,
                default=self._current(CONF_BATTERY_THRESHOLD, 20),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=100,
                    mode="box",
                    unit_of_measurement="%",
                )
            ),
        }
        fields.update(_threshold_form_fields(threshold_defaults))
        return vol.Schema(fields)


    async def async_step_init(self, user_input=None):
        if user_input is not None:
            mode = user_input.get(
                CONF_GRID_LOSS_MODE,
                self._current(CONF_GRID_LOSS_MODE, GRID_LOSS_MODE_SENSOR),
            )
            load_sensor = user_input.get(CONF_LOAD_SENSOR) or self._current(
                CONF_LOAD_SENSOR, ""
            )
            grid_sensor = user_input.get(CONF_GRID_LOSS_SENSOR) or self._current(
                CONF_GRID_LOSS_SENSOR, ""
            )
            battery_soc = user_input.get(CONF_BATTERY_SOC) or self._current(
                CONF_BATTERY_SOC, ""
            )
            devices_input = user_input.get(
                CONF_DEVICES,
                self._current(CONF_DEVICES, []),
            )
            errors: dict[str, str] = {}

            thresholds, threshold_error = _parse_threshold_input(
                user_input,
                _threshold_defaults(self._current(CONF_THRESHOLDS, None)),
            )
            if threshold_error:
                errors.setdefault("base", threshold_error)

            if _entity_id(load_sensor, frozenset({"sensor"})) is None:
                errors["base"] = "invalid_load_sensor"
            elif mode not in (GRID_LOSS_MODE_SENSOR, GRID_LOSS_MODE_THRESHOLD):
                errors["base"] = "invalid_grid_loss_mode"
            elif mode == GRID_LOSS_MODE_SENSOR and not grid_sensor:
                errors["base"] = "missing_grid_loss_sensor"
            elif mode == GRID_LOSS_MODE_SENSOR and _entity_id(
                grid_sensor, frozenset({"binary_sensor"})
            ) is None:
                errors["base"] = "invalid_grid_loss_sensor"
            elif mode == GRID_LOSS_MODE_THRESHOLD and not battery_soc:
                errors["base"] = "missing_battery_soc_sensor"
            elif mode == GRID_LOSS_MODE_THRESHOLD and _entity_id(
                battery_soc, frozenset({"sensor"})
            ) is None:
                errors["base"] = "invalid_battery_soc_sensor"

            try:
                normalized_devices = _normalize_options_devices(devices_input)
            except ValueError:
                normalized_devices = []
                errors.setdefault("base", "invalid_devices")

            numeric_defaults = {
                CONF_MAX_LOAD: (5000, 100, 50000),
                CONF_AVERAGING_PERIOD: (DEFAULT_AVERAGING_PERIOD, 1, 300),
                CONF_SAFETY_RESERVE: (DEFAULT_SAFETY_RESERVE, 0, 5000),
                CONF_HYSTERESIS: (DEFAULT_HYSTERESIS, 0, 5000),
                CONF_PAUSE_PERIOD: (DEFAULT_PAUSE_PERIOD, 0, 3600),
                CONF_BATTERY_THRESHOLD: (20, 0, 100),
            }
            normalized_numbers: dict[str, int | float] = {}
            for key, (default, minimum, maximum) in numeric_defaults.items():
                raw_value = user_input.get(key, self._current(key, default))
                if isinstance(raw_value, bool):
                    errors.setdefault("base", "invalid_numeric_setting")
                    continue
                try:
                    number = float(raw_value)
                except (TypeError, ValueError):
                    errors.setdefault("base", "invalid_numeric_setting")
                    continue
                if not math.isfinite(number) or not minimum <= number <= maximum:
                    errors.setdefault("base", "invalid_numeric_setting")
                    continue
                normalized_numbers[key] = (
                    int(number) if number.is_integer() else number
                )

            optional_entities = {
                "solar_power": ("sensor",),
                "battery_power": ("sensor",),
            }
            normalized_optional: dict[str, str | None] = {}
            for key, domains in optional_entities.items():
                value = user_input.get(key, self._current(key, "")) or None
                if value is not None and _entity_id(value, frozenset(domains)) is None:
                    errors.setdefault("base", f"invalid_{key}")
                else:
                    normalized_optional[key] = value

            forecast_entry = user_input.get(
                "solar_forecast_entry",
                self._current("solar_forecast_entry", ""),
            ) or None
            if forecast_entry is not None and (
                not isinstance(forecast_entry, str) or not forecast_entry.strip()
            ):
                errors.setdefault("base", "invalid_solar_forecast_entry")

            if errors:
                return self.async_show_form(
                    step_id="init",
                    data_schema=self._options_schema(),
                    errors=errors,
                )

            normalized = dict(user_input)
            normalized.update(normalized_numbers)
            normalized[CONF_LOAD_SENSOR] = load_sensor
            normalized[CONF_THRESHOLDS] = thresholds
            normalized[CONF_DEVICES] = normalized_devices
            normalized[CONF_GRID_LOSS_MODE] = mode
            normalized[CONF_GRID_LOSS_SENSOR] = (
                grid_sensor if mode == GRID_LOSS_MODE_SENSOR else None
            )
            normalized[CONF_BATTERY_SOC] = (
                battery_soc if mode == GRID_LOSS_MODE_THRESHOLD else None
            )
            normalized.update(normalized_optional)
            normalized["solar_forecast_entry"] = forecast_entry
            return self.async_create_entry(title="", data=normalized)

        return self.async_show_form(
            step_id="init",
            data_schema=self._options_schema(),
        )
