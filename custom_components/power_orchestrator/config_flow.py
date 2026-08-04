"""Configuration and options flows for the load-shedding controller."""

from __future__ import annotations

import logging
import math
import uuid
from collections.abc import Mapping
from typing import Any, cast

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
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
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
    CONF_SAFETY_RESERVE,
    CONF_SHED_PRIORITY,
    CONF_THRESHOLD_COUNT,
    CONF_THRESHOLDS,
    DEFAULT_AVERAGING_PERIOD,
    DEFAULT_HARD_INTERLOCK,
    DEFAULT_HYSTERESIS,
    DEFAULT_PAUSE_PERIOD,
    DEFAULT_SAFETY_RESERVE,
    DEFAULT_SHED_CRITICAL_DURATION,
    DEFAULT_SHED_CRITICAL_LIMIT,
    DEFAULT_SHED_FAST_DURATION,
    DEFAULT_SHED_FAST_LIMIT,
    DEFAULT_SHED_SUSTAINED_DURATION,
    DEFAULT_SHED_SUSTAINED_LIMIT,
    DOMAIN,
    GRID_LOSS_MODE_SENSOR,
    GRID_LOSS_MODE_THRESHOLD,
    MAX_CUSTOM_THRESHOLDS,
)
from .policy import validate_threshold_pair

_LOGGER = logging.getLogger(__name__)

_CANONICAL_THRESHOLD_INPUTS = (
    {"power_limit": DEFAULT_SHED_SUSTAINED_LIMIT, "duration_s": DEFAULT_SHED_SUSTAINED_DURATION},
    {"power_limit": DEFAULT_SHED_FAST_LIMIT, "duration_s": DEFAULT_SHED_FAST_DURATION},
    {"power_limit": DEFAULT_SHED_CRITICAL_LIMIT, "duration_s": DEFAULT_SHED_CRITICAL_DURATION},
)


def _gen_id() -> str:
    return uuid.uuid4().hex[:8]


def _friendly(hass: Any, entity_id: str) -> str:
    if not entity_id:
        return ""
    state = hass.states.get(entity_id)
    name = getattr(state, "attributes", {}).get("friendly_name") if state is not None else None
    return name.strip() if isinstance(name, str) and name.strip() else entity_id


def _entity_id(value: Any, domains: frozenset[str]) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if "." not in value:
        return None
    domain, object_id = value.split(".", 1)
    return value if domain in domains and object_id else None


def _sensor_entity_id(value: Any) -> str | None:
    return _entity_id(value, frozenset({"sensor"}))


def _threshold_field(index: int, kind: str) -> str:
    return f"threshold_{index}_{kind}"


def _threshold_defaults(value: Any) -> list[dict[str, float]]:
    """Normalize persisted threshold pairs and fill canonical defaults."""
    result: list[dict[str, float]] = []
    if isinstance(value, (list, tuple)):
        for raw in value[:MAX_CUSTOM_THRESHOLDS]:
            if not isinstance(raw, Mapping):
                continue
            try:
                limit = float(raw.get("power_limit", raw.get("limit_w")))
                duration = float(raw.get("duration_s", raw.get("time_s")))
            except (TypeError, ValueError):
                continue
            if math.isfinite(limit) and math.isfinite(duration) and limit > 0 and duration >= 0:
                result.append({"power_limit": limit, "duration_s": duration})
    if not result:
        result = [dict(item) for item in _CANONICAL_THRESHOLD_INPUTS]
    return result


def _parse_threshold_input(
    user_input: Mapping[str, Any],
    defaults: list[dict[str, float]] | None = None,
) -> tuple[list[dict[str, float]] | None, str | None]:
    """Parse one to ten strictly increasing threshold pairs."""
    raw_count = user_input.get(CONF_THRESHOLD_COUNT)
    if raw_count is None:
        pairs = defaults or _threshold_defaults(None)
        return pairs, None
    if isinstance(raw_count, bool):
        return None, "invalid_thresholds"
    try:
        count = int(raw_count)
    except (TypeError, ValueError):
        return None, "invalid_thresholds"
    if count != raw_count or not 1 <= count <= MAX_CUSTOM_THRESHOLDS:
        return None, "invalid_thresholds"
    parsed: list[dict[str, float]] = []
    previous = 0.0
    for index in range(1, count + 1):
        power = user_input.get(_threshold_field(index, "power"))
        duration = user_input.get(_threshold_field(index, "time"))
        if power is None or duration is None or isinstance(power, bool) or isinstance(duration, bool):
            return None, "invalid_thresholds"
        try:
            limit, dwell = validate_threshold_pair(float(power), float(duration), previous)
        except (TypeError, ValueError):
            return None, "invalid_thresholds"
        if limit > DEFAULT_HARD_INTERLOCK:
            return None, "invalid_thresholds"
        parsed.append({"power_limit": limit, "duration_s": dwell})
        previous = limit
    return parsed, None


def _threshold_form_fields(defaults: list[dict[str, float]] | None = None) -> dict[Any, Any]:
    """Expose all ten threshold positions so the UI has explicit descriptions."""
    values = defaults or _threshold_defaults(None)
    fields: dict[Any, Any] = {
        vol.Required(CONF_THRESHOLD_COUNT, default=len(values)): selector.NumberSelector(
            cast(Any, selector.NumberSelectorConfig)(min=1, max=MAX_CUSTOM_THRESHOLDS, mode="box")
        )
    }
    for index in range(1, MAX_CUSTOM_THRESHOLDS + 1):
        pair = values[index - 1] if index <= len(values) else values[-1]
        fields[vol.Optional(_threshold_field(index, "power"), default=pair["power_limit"])] = selector.NumberSelector(
            cast(Any, selector.NumberSelectorConfig)(min=1, max=100000, mode="box", unit_of_measurement="W")
        )
        fields[vol.Optional(_threshold_field(index, "time"), default=pair["duration_s"])] = selector.NumberSelector(
            cast(Any, selector.NumberSelectorConfig)(min=0, max=86400, mode="box", unit_of_measurement="s")
        )
    return fields


def _entity_selector(domains: str | list[str], *, multiple: bool = False) -> Any:
    return selector.EntitySelector(selector.EntitySelectorConfig(domain=domains, multiple=multiple))


async def _discover_inputs(hass: Any) -> dict[str, Any]:
    """Discover optional load and grid-safety inputs; no activation policy is inferred."""
    result: dict[str, Any] = {
        "grid_power": None,
        "battery_soc": None,
        "devices": [],
    }
    try:
        from homeassistant.components.energy import async_get_manager

        manager = await async_get_manager(hass)
        data = getattr(manager, "data", None)
        if not isinstance(data, dict):
            return result
        for source in data.get("energy_sources", []):
            if not isinstance(source, dict):
                continue
            source_type = source.get("type")
            if source_type == "grid":
                candidate = source.get("power_config", {}).get("stat_rate") or source.get("stat_rate")
                result["grid_power"] = _sensor_entity_id(candidate)
            elif source_type == "battery":
                result["battery_soc"] = _sensor_entity_id(source.get("stat_soc"))
        for device in data.get("device_consumption", []):
            if not isinstance(device, dict):
                continue
            entity_id = _sensor_entity_id(device.get("stat_consumption"))
            if entity_id is None:
                continue
            result["devices"].append(
                {
                    "entity_id": entity_id,
                    "name": device.get("name") or _friendly(hass, entity_id),
                    "power_sensor": _sensor_entity_id(device.get("stat_rate")),
                }
            )
    except Exception as exc:  # discovery is advisory; forms remain usable
        _LOGGER.debug("Optional load/safety discovery unavailable: %s", exc)
    return result


def _normalize_options_devices(value: Any) -> list[dict[str, Any]]:
    """Validate and normalize structured device mappings."""
    if not isinstance(value, list):
        raise ValueError("devices must be a list")
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_entities: set[str] = set()
    seen_priorities: set[int] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise ValueError("device mapping must be an object")
        device_id = raw.get(CONF_DEVICE_ID)
        if not isinstance(device_id, str) or not device_id.strip() or device_id in seen_ids:
            raise ValueError("device IDs must be unique")
        device_id = device_id.strip()
        entity = _entity_id(raw.get(CONF_DEVICE_ENTITY), frozenset({"switch", "light", "input_boolean"}))
        if entity is None or entity in seen_entities:
            raise ValueError("control entities must be valid and unique")
        raw_actuators = raw.get(CONF_DEVICE_ACTUATORS, ())
        if raw_actuators in (None, ""):
            raw_actuators = ()
        if isinstance(raw_actuators, str):
            raw_actuators = (raw_actuators,)
        if not isinstance(raw_actuators, (list, tuple)):
            raise ValueError("actuators must be a list")
        actuators: list[str] = []
        for raw_actuator in raw_actuators:
            actuator = _entity_id(raw_actuator, frozenset({"switch", "light", "input_boolean", "climate"}))
            if actuator is None or actuator == entity or actuator in actuators or actuator in seen_entities:
                raise ValueError("logical actuator entities must be valid and unique")
            actuators.append(actuator)
        expected_raw = raw.get(CONF_DEVICE_EXPECTED_POWER)
        if expected_raw is None or isinstance(expected_raw, bool):
            raise ValueError("expected power must be finite")
        try:
            expected = float(expected_raw)
        except (TypeError, ValueError):
            raise ValueError("expected power must be finite") from None
        if not math.isfinite(expected) or not 1 <= expected <= 50000:
            raise ValueError("expected power is outside the allowed range")
        sensor = raw.get(CONF_DEVICE_POWER_SENSOR)
        sensor = None if sensor in (None, "") else _sensor_entity_id(sensor)
        if raw.get(CONF_DEVICE_POWER_SENSOR) not in (None, "") and sensor is None:
            raise ValueError("power sensor must be a sensor entity")
        name = raw.get(CONF_DEVICE_NAME) or entity
        if not isinstance(name, str) or not name.strip():
            name = entity

        def positive(raw_value: Any, default: int, label: str) -> int:
            if raw_value is None:
                return default
            if isinstance(raw_value, bool):
                raise ValueError(f"{label} must be an integer")
            try:
                converted = float(raw_value)
            except (TypeError, ValueError):
                raise ValueError(f"{label} must be an integer") from None
            if not math.isfinite(converted) or converted < 1 or converted != int(converted):
                raise ValueError(f"{label} must be a positive integer")
            return int(converted)

        priority = positive(raw.get(CONF_PRIORITY), index + 1, "priority")
        if priority in seen_priorities:
            raise ValueError("priorities must be unique")
        shed_priority = positive(raw.get(CONF_SHED_PRIORITY), priority, "shed priority")
        normalized.append(
            {
                CONF_DEVICE_ID: device_id,
                CONF_DEVICE_NAME: name.strip(),
                CONF_DEVICE_ENTITY: entity,
                CONF_DEVICE_EXPECTED_POWER: int(math.ceil(expected)),
                CONF_DEVICE_POWER_SENSOR: sensor,
                CONF_PRIORITY: priority,
                CONF_SHED_PRIORITY: shed_priority,
                CONF_DEVICE_ACTUATORS: actuators,
            }
        )
        seen_ids.add(device_id)
        seen_entities.update((entity, *actuators))
        seen_priorities.add(priority)
    return normalized


def _entry_current(entry: Any, key: str, default: Any = None) -> Any:
    options = getattr(entry, "options", {}) or {}
    data = getattr(entry, "data", {}) or {}
    if key in options:
        return options[key]
    return data.get(key, default)


def _options_schema_for_entry(entry: Any) -> vol.Schema:
    fields: dict[Any, Any] = {
        vol.Required(CONF_LOAD_SENSOR, default=_entry_current(entry, CONF_LOAD_SENSOR, "")): _entity_selector("sensor"),
        vol.Optional(CONF_DEVICES, default=_entry_current(entry, CONF_DEVICES, [])): selector.ObjectSelector(),
        vol.Required(CONF_MAX_LOAD, default=_entry_current(entry, CONF_MAX_LOAD, 5000)): selector.NumberSelector(cast(Any, selector.NumberSelectorConfig)(min=100, max=50000, mode="box", unit_of_measurement="W")),
        vol.Required(CONF_AVERAGING_PERIOD, default=_entry_current(entry, CONF_AVERAGING_PERIOD, DEFAULT_AVERAGING_PERIOD)): selector.NumberSelector(cast(Any, selector.NumberSelectorConfig)(min=1, max=300, mode="box", unit_of_measurement="s")),
        vol.Required(CONF_SAFETY_RESERVE, default=_entry_current(entry, CONF_SAFETY_RESERVE, DEFAULT_SAFETY_RESERVE)): selector.NumberSelector(cast(Any, selector.NumberSelectorConfig)(min=0, max=5000, mode="box", unit_of_measurement="W")),
        vol.Required(CONF_HYSTERESIS, default=_entry_current(entry, CONF_HYSTERESIS, DEFAULT_HYSTERESIS)): selector.NumberSelector(cast(Any, selector.NumberSelectorConfig)(min=0, max=5000, mode="box", unit_of_measurement="W")),
        vol.Required(CONF_PAUSE_PERIOD, default=_entry_current(entry, CONF_PAUSE_PERIOD, DEFAULT_PAUSE_PERIOD)): selector.NumberSelector(cast(Any, selector.NumberSelectorConfig)(min=0, max=86400, mode="box", unit_of_measurement="s")),
        vol.Required(CONF_GRID_LOSS_MODE, default=_entry_current(entry, CONF_GRID_LOSS_MODE, GRID_LOSS_MODE_SENSOR)): selector.SelectSelector(selector.SelectSelectorConfig(options=[selector.SelectOptionDict(value=GRID_LOSS_MODE_SENSOR, label="Grid loss sensor"), selector.SelectOptionDict(value=GRID_LOSS_MODE_THRESHOLD, label="Battery threshold")])),
        vol.Optional(CONF_GRID_LOSS_SENSOR, default=_entry_current(entry, CONF_GRID_LOSS_SENSOR, "")): _entity_selector("binary_sensor"),
        vol.Optional(CONF_BATTERY_SOC, default=_entry_current(entry, CONF_BATTERY_SOC, "")): _entity_selector("sensor"),
        vol.Optional(CONF_BATTERY_THRESHOLD, default=_entry_current(entry, CONF_BATTERY_THRESHOLD, 20)): selector.NumberSelector(cast(Any, selector.NumberSelectorConfig)(min=0, max=100, mode="box", unit_of_measurement="%")),
    }
    fields.update(_threshold_form_fields(_threshold_defaults(_entry_current(entry, CONF_THRESHOLDS, None))))
    return vol.Schema(fields)


class PowerOrchestratorConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Five-step onboarding flow for load shedding and safety sources."""

    VERSION = 2
    MINOR_VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, Any] = {}
        self._devices: list[dict[str, Any]] = []
        self._devices_phase = "selection"
        self._pending_discovered: list[dict[str, Any]] = []
        self._pending_discovered_index = 0
        self._add_custom_device = False
        self._pause_period = DEFAULT_PAUSE_PERIOD

    def _discovery_summary(self) -> str:
        lines = [
            f"{'✅' if self._discovered.get('grid_power') else '❌'} Grid power: {self._discovered.get('grid_power') or 'not found — select manually'}",
        ]
        if self._discovered.get("battery_soc"):
            lines.append(f"✅ Battery SoC: {self._discovered['battery_soc']}")
        if self._discovered.get("devices"):
            lines.append(f"📦 Devices found: {len(self._discovered['devices'])}")
        return "\n".join(lines)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> Any:
        """Step 1: discover and select telemetry sources."""
        if user_input is None:
            entries: Any = getattr(getattr(self.hass, "config_entries", None), "async_entries", lambda *_: [])(DOMAIN)
            if isinstance(entries, (list, tuple)) and entries:
                return self.async_abort(reason="single_instance")
            self._discovered = await _discover_inputs(self.hass)
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {
                        vol.Optional("grid_power", default=self._discovered.get("grid_power") or ""): _entity_selector("sensor"),
                        vol.Optional(CONF_BATTERY_SOC, default=self._discovered.get(CONF_BATTERY_SOC) or ""): _entity_selector("sensor"),
                    }
                ),
                description_placeholders={"summary": self._discovery_summary()},
            )
        self._discovered["grid_power"] = _sensor_entity_id(user_input.get("grid_power"))
        self._discovered[CONF_BATTERY_SOC] = _sensor_entity_id(user_input.get(CONF_BATTERY_SOC))
        return await self.async_step_load_monitoring()

    def _load_monitoring_form(self, errors: dict[str, str] | None = None) -> Any:
        return self.async_show_form(
            step_id="load_monitoring",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_LOAD_SENSOR, default=self._discovered.get("grid_power") or ""): _entity_selector("sensor"),
                    vol.Required(CONF_MAX_LOAD, default=5000): selector.NumberSelector(cast(Any, selector.NumberSelectorConfig)(min=100, max=50000, mode="box", unit_of_measurement="W")),
                    vol.Required(CONF_AVERAGING_PERIOD, default=DEFAULT_AVERAGING_PERIOD): selector.NumberSelector(cast(Any, selector.NumberSelectorConfig)(min=1, max=300, mode="box", unit_of_measurement="s")),
                    vol.Required(CONF_SAFETY_RESERVE, default=DEFAULT_SAFETY_RESERVE): selector.NumberSelector(cast(Any, selector.NumberSelectorConfig)(min=0, max=5000, mode="box", unit_of_measurement="W")),
                    vol.Required(CONF_HYSTERESIS, default=DEFAULT_HYSTERESIS): selector.NumberSelector(cast(Any, selector.NumberSelectorConfig)(min=0, max=5000, mode="box", unit_of_measurement="W")),
                    **_threshold_form_fields(),
                }
            ),
            errors=errors or {},
        )

    async def async_step_load_monitoring(self, user_input: dict[str, Any] | None = None) -> Any:
        if user_input is None:
            return self._load_monitoring_form()
        thresholds, error = _parse_threshold_input(user_input)
        if error:
            return self._load_monitoring_form({"base": error})
        self._discovered.update(
            {
                "grid_power": user_input.get(CONF_LOAD_SENSOR),
                CONF_MAX_LOAD: user_input.get(CONF_MAX_LOAD, 5000),
                CONF_AVERAGING_PERIOD: user_input.get(CONF_AVERAGING_PERIOD, DEFAULT_AVERAGING_PERIOD),
                CONF_SAFETY_RESERVE: user_input.get(CONF_SAFETY_RESERVE, DEFAULT_SAFETY_RESERVE),
                CONF_HYSTERESIS: user_input.get(CONF_HYSTERESIS, DEFAULT_HYSTERESIS),
                CONF_THRESHOLDS: thresholds,
            }
        )
        return await self.async_step_devices()

    def _device_name(self, candidate: Mapping[str, Any]) -> str:
        return str(candidate.get("name") or _friendly(self.hass, str(candidate.get("entity_id") or "")))

    def _device_selection_form(self, errors: dict[str, str] | None = None) -> Any:
        options = [
            selector.SelectOptionDict(value=item["entity_id"], label=self._device_name(item))
            for item in self._discovered.get("devices", [])
            if isinstance(item, dict) and item.get("entity_id")
        ]
        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_DISCOVERED_DEVICES, default=[]): selector.SelectSelector(selector.SelectSelectorConfig(options=options, multiple=True)),
                    vol.Optional(CONF_ADD_CUSTOM_DEVICE, default=False): bool,
                }
            ),
            errors=errors or {},
        )

    def _device_config_form(self, candidate: dict[str, Any] | None = None, errors: dict[str, str] | None = None) -> Any:
        candidate = candidate or {}
        candidate_name = self._device_name(candidate) if candidate else ""
        power_sensor = _sensor_entity_id(candidate.get("power_sensor")) or ""
        status = f"{candidate_name or 'Custom device'} — measured-power sensor: {power_sensor or 'not selected'}"
        fields: dict[Any, Any] = {
            vol.Required(CONF_DEVICE_ENTITY): _entity_selector(["switch", "light", "input_boolean"]),
            vol.Optional(CONF_DEVICE_NAME, default=candidate_name): selector.TextSelector(),
            vol.Required(CONF_DEVICE_EXPECTED_POWER, default=candidate.get(CONF_DEVICE_EXPECTED_POWER, 2000)): selector.NumberSelector(cast(Any, selector.NumberSelectorConfig)(min=1, max=50000, mode="box", unit_of_measurement="W")),
            vol.Optional(CONF_DEVICE_POWER_SENSOR, default=power_sensor): _entity_selector("sensor"),
            vol.Optional(CONF_DEVICE_ACTUATORS, default=list(candidate.get(CONF_DEVICE_ACTUATORS, ()) or ())): _entity_selector(["switch", "light", "input_boolean", "climate"], multiple=True),
        }
        if not candidate:
            fields[vol.Optional(CONF_ADD_ANOTHER, default=False)] = bool
        return self.async_show_form(step_id="devices", data_schema=vol.Schema(fields), errors=errors or {}, description_placeholders={"count": str(len(self._devices)), "discovered": status})

    def _build_device(self, user_input: dict[str, Any], candidate: dict[str, Any] | None = None) -> dict[str, Any]:
        candidate = candidate or {}
        entity = _entity_id(user_input.get(CONF_DEVICE_ENTITY), frozenset({"switch", "light", "input_boolean"}))
        if entity is None:
            raise ValueError("control entity must be valid")
        name = user_input.get(CONF_DEVICE_NAME) or (self._device_name(candidate) if candidate else _friendly(self.hass, entity))
        power_sensor = _sensor_entity_id(user_input.get(CONF_DEVICE_POWER_SENSOR)) if CONF_DEVICE_POWER_SENSOR in user_input else _sensor_entity_id(candidate.get("power_sensor"))
        raw_actuators = user_input.get(CONF_DEVICE_ACTUATORS, []) or []
        actuator_values: list[str] = []
        if isinstance(raw_actuators, (list, tuple)):
            for raw_actuator in raw_actuators:
                actuator = _entity_id(
                    raw_actuator,
                    frozenset({"switch", "light", "input_boolean", "climate"}),
                )
                if actuator and actuator != entity and actuator not in actuator_values:
                    actuator_values.append(actuator)
        return {
            CONF_DEVICE_ID: _gen_id(),
            CONF_DEVICE_NAME: name or entity,
            CONF_DEVICE_ENTITY: entity,
            CONF_DEVICE_EXPECTED_POWER: user_input.get(CONF_DEVICE_EXPECTED_POWER, 2000),
            CONF_DEVICE_POWER_SENSOR: power_sensor,
            CONF_DEVICE_ACTUATORS: actuator_values,
        }

    async def async_step_devices(self, user_input: dict[str, Any] | None = None) -> Any:
        if self._devices_phase == "selection":
            if user_input is None:
                return self._device_selection_form()
            candidates = {item["entity_id"]: item for item in self._discovered.get("devices", []) if isinstance(item, dict) and item.get("entity_id")}
            selected = user_input.get(CONF_DISCOVERED_DEVICES, []) or []
            if isinstance(selected, str):
                selected = [selected]
            if any(item not in candidates for item in selected):
                return self._device_selection_form({"base": "invalid_discovered_devices"})
            self._pending_discovered = [candidates[item] for item in selected]
            self._pending_discovered_index = 0
            self._add_custom_device = bool(user_input.get(CONF_ADD_CUSTOM_DEVICE, False))
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
            try:
                self._devices.append(self._build_device(user_input, candidate))
            except ValueError:
                return self._device_config_form(candidate, {"base": "invalid_devices"})
            self._pending_discovered_index += 1
            if self._pending_discovered_index < len(self._pending_discovered):
                return await self.async_step_devices()
            if self._add_custom_device:
                self._devices_phase = "custom"
                return await self.async_step_devices()
            return await self.async_step_priority()
        if user_input is None:
            return self._device_config_form()
        try:
            self._devices.append(self._build_device(user_input))
        except ValueError:
            return self._device_config_form(errors={"base": "invalid_devices"})
        if user_input.get(CONF_ADD_ANOTHER, False):
            return await self.async_step_devices()
        return await self.async_step_priority()

    @staticmethod
    def _priority_field(index: int) -> str:
        return f"priority_{index + 1}"

    def _priority_form(self, errors: dict[str, str] | None = None) -> Any:
        options = [selector.SelectOptionDict(value=d[CONF_DEVICE_ID], label=d.get(CONF_DEVICE_NAME, d[CONF_DEVICE_ENTITY])) for d in self._devices]
        fields: dict[Any, Any] = {}
        for index, device in enumerate(self._devices):
            fields[vol.Required(self._priority_field(index), default=device[CONF_DEVICE_ID])] = selector.SelectSelector(selector.SelectSelectorConfig(options=options))
        fields[vol.Optional(CONF_PAUSE_PERIOD, default=DEFAULT_PAUSE_PERIOD)] = selector.NumberSelector(cast(Any, selector.NumberSelectorConfig)(min=0, max=86400, mode="box", unit_of_measurement="s"))
        return self.async_show_form(step_id="priority", data_schema=vol.Schema(fields), errors=errors or {}, description_placeholders={"device_list": "\n".join(f"{i + 1}. {d.get(CONF_DEVICE_NAME, d.get(CONF_DEVICE_ENTITY))}" for i, d in enumerate(self._devices))})

    async def async_step_priority(self, user_input: dict[str, Any] | None = None) -> Any:
        if user_input is None:
            return self._priority_form()
        selected = [user_input.get(self._priority_field(index)) for index in range(len(self._devices))]
        by_id = {d[CONF_DEVICE_ID]: d for d in self._devices}
        if len(selected) != len(by_id) or set(selected) != set(by_id):
            return self._priority_form({"base": "invalid_priority_order"})
        self._devices = [by_id[device_id] for device_id in selected]
        for index, device in enumerate(self._devices, start=1):
            device[CONF_PRIORITY] = index
            device[CONF_SHED_PRIORITY] = index
        self._pause_period = user_input.get(CONF_PAUSE_PERIOD, DEFAULT_PAUSE_PERIOD)
        return await self.async_step_grid_loss()

    def _grid_loss_form(self, errors: dict[str, str] | None = None) -> Any:
        soc = self._discovered.get(CONF_BATTERY_SOC) or ""
        return self.async_show_form(
            step_id="grid_loss",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_GRID_LOSS_MODE, default=GRID_LOSS_MODE_SENSOR): selector.SelectSelector(selector.SelectSelectorConfig(options=[selector.SelectOptionDict(value=GRID_LOSS_MODE_SENSOR, label="Grid loss sensor"), selector.SelectOptionDict(value=GRID_LOSS_MODE_THRESHOLD, label="Battery threshold")])),
                    vol.Optional(CONF_GRID_LOSS_SENSOR): _entity_selector("binary_sensor"),
                    vol.Optional(CONF_BATTERY_SOC, default=soc): _entity_selector("sensor"),
                    vol.Optional(CONF_BATTERY_THRESHOLD, default=20): selector.NumberSelector(cast(Any, selector.NumberSelectorConfig)(min=0, max=100, mode="box", unit_of_measurement="%")),
                }
            ),
            errors=errors or {},
        )

    async def async_step_grid_loss(self, user_input: dict[str, Any] | None = None) -> Any:
        if user_input is None:
            return self._grid_loss_form()
        mode = user_input.get(CONF_GRID_LOSS_MODE, GRID_LOSS_MODE_SENSOR)
        grid_sensor = _entity_id(user_input.get(CONF_GRID_LOSS_SENSOR), frozenset({"binary_sensor"}))
        battery_soc = _sensor_entity_id(user_input.get(CONF_BATTERY_SOC) or self._discovered.get(CONF_BATTERY_SOC))
        if mode == GRID_LOSS_MODE_SENSOR and grid_sensor is None:
            return self._grid_loss_form({"base": "missing_grid_loss_sensor"})
        if mode == GRID_LOSS_MODE_THRESHOLD and battery_soc is None:
            return self._grid_loss_form({"base": "missing_battery_soc_sensor"})
        data = {
            CONF_LOAD_SENSOR: self._discovered.get("grid_power"),
            CONF_MAX_LOAD: self._discovered.get(CONF_MAX_LOAD, 5000),
            CONF_AVERAGING_PERIOD: self._discovered.get(CONF_AVERAGING_PERIOD, DEFAULT_AVERAGING_PERIOD),
            CONF_SAFETY_RESERVE: self._discovered.get(CONF_SAFETY_RESERVE, DEFAULT_SAFETY_RESERVE),
            CONF_HYSTERESIS: self._discovered.get(CONF_HYSTERESIS, DEFAULT_HYSTERESIS),
            CONF_THRESHOLDS: self._discovered.get(CONF_THRESHOLDS, [dict(item) for item in _CANONICAL_THRESHOLD_INPUTS]),
            CONF_DEVICES: self._devices,
            CONF_PAUSE_PERIOD: self._pause_period,
            CONF_GRID_LOSS_MODE: mode,
        }
        if mode == GRID_LOSS_MODE_SENSOR:
            data[CONF_GRID_LOSS_SENSOR] = grid_sensor
        else:
            data[CONF_BATTERY_SOC] = battery_soc
            data[CONF_BATTERY_THRESHOLD] = user_input.get(CONF_BATTERY_THRESHOLD, 20)
        return self.async_create_entry(title="Power Orchestrator", data=data)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> Any:
        entry = self._get_reconfigure_entry()
        if entry is None:
            return self.async_abort(reason="entry_not_loaded")
        schema = _options_schema_for_entry(entry)
        if user_input is None:
            return self.async_show_form(step_id="reconfigure", data_schema=schema)
        options = PowerOrchestratorOptionsFlow(entry)
        result = await options.async_step_init(user_input)
        if result.get("type") != "create_entry":
            return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=result.get("errors", {}))
        return self.async_update_and_abort(entry, data=result.get("data", {}), options={}, reason="reconfigure_successful")


class PowerOrchestratorOptionsFlow(config_entries.OptionsFlow):
    """Validate runtime/safety options without any activation fields."""

    def __init__(self, entry: Any) -> None:
        super().__init__(entry)  # type: ignore[call-arg]
        self._entry = entry

    def _current(self, key: str, default: Any = None) -> Any:
        return _entry_current(self._entry, key, default)

    def _options_schema(self) -> vol.Schema:
        return _options_schema_for_entry(self._entry)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> Any:
        if user_input is None:
            return self.async_show_form(step_id="init", data_schema=self._options_schema())
        errors: dict[str, str] = {}
        load_sensor = _sensor_entity_id(user_input.get(CONF_LOAD_SENSOR, self._current(CONF_LOAD_SENSOR, "")))
        if load_sensor is None:
            errors["base"] = "invalid_load_sensor"
        mode = user_input.get(CONF_GRID_LOSS_MODE, self._current(CONF_GRID_LOSS_MODE, GRID_LOSS_MODE_SENSOR))
        grid_sensor = _entity_id(user_input.get(CONF_GRID_LOSS_SENSOR, self._current(CONF_GRID_LOSS_SENSOR, "")), frozenset({"binary_sensor"}))
        battery_soc = _sensor_entity_id(user_input.get(CONF_BATTERY_SOC, self._current(CONF_BATTERY_SOC, "")))
        if mode == GRID_LOSS_MODE_SENSOR and grid_sensor is None:
            errors["base"] = "missing_grid_loss_sensor"
        if mode == GRID_LOSS_MODE_THRESHOLD and battery_soc is None:
            errors["base"] = "missing_battery_soc_sensor"
        try:
            devices = _normalize_options_devices(user_input.get(CONF_DEVICES, self._current(CONF_DEVICES, [])))
        except ValueError:
            devices = []
            errors.setdefault("base", "invalid_devices")
        thresholds, threshold_error = _parse_threshold_input(user_input, _threshold_defaults(self._current(CONF_THRESHOLDS, None)))
        if threshold_error:
            errors.setdefault("base", threshold_error)
        numeric = {
            CONF_MAX_LOAD: (5000, 100, 50000),
            CONF_AVERAGING_PERIOD: (DEFAULT_AVERAGING_PERIOD, 1, 300),
            CONF_SAFETY_RESERVE: (DEFAULT_SAFETY_RESERVE, 0, 5000),
            CONF_HYSTERESIS: (DEFAULT_HYSTERESIS, 0, 5000),
            CONF_PAUSE_PERIOD: (DEFAULT_PAUSE_PERIOD, 0, 86400),
            CONF_BATTERY_THRESHOLD: (20, 0, 100),
        }
        values: dict[str, int | float] = {}
        for key, (default, minimum, maximum) in numeric.items():
            raw = user_input.get(key, self._current(key, default))
            if isinstance(raw, bool):
                errors.setdefault("base", "invalid_numeric_setting")
                continue
            try:
                converted = float(raw)
            except (TypeError, ValueError):
                errors.setdefault("base", "invalid_numeric_setting")
                continue
            if not math.isfinite(converted) or not minimum <= converted <= maximum:
                errors.setdefault("base", "invalid_numeric_setting")
                continue
            values[key] = int(converted) if converted.is_integer() else converted
        if errors:
            return self.async_show_form(step_id="init", data_schema=self._options_schema(), errors=errors)
        normalized = dict(user_input)
        normalized.update(values)
        normalized[CONF_LOAD_SENSOR] = load_sensor
        normalized[CONF_DEVICES] = devices
        normalized[CONF_THRESHOLDS] = thresholds
        normalized[CONF_GRID_LOSS_MODE] = mode
        normalized[CONF_GRID_LOSS_SENSOR] = grid_sensor if mode == GRID_LOSS_MODE_SENSOR else None
        normalized[CONF_BATTERY_SOC] = battery_soc if mode == GRID_LOSS_MODE_THRESHOLD else None
        return self.async_create_entry(title="", data=normalized)


@callback
def async_get_options_flow(config_entry: Any) -> PowerOrchestratorOptionsFlow:
    return PowerOrchestratorOptionsFlow(config_entry)
