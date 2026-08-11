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
    CONF_ADD_THRESHOLD,
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
    CONF_PRIORITY_ORDER,
    CONF_SAFETY_RESERVE,
    CONF_SHED_PRIORITY,
    CONF_THRESHOLD_COUNT,
    CONF_THRESHOLD_DURATION,
    CONF_THRESHOLD_POWER,
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
    """Parse legacy numbered threshold pairs with the bounded runtime limit."""
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
        if (
            power is None
            or duration is None
            or isinstance(power, bool)
            or isinstance(duration, bool)
        ):
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


def _threshold_step_fields(
    default: Mapping[str, Any] | None = None,
    *,
    add_another_default: bool = False,
) -> dict[Any, Any]:
    """Build one repeatable threshold form instead of fixed numbered fields."""
    pair = default or {
        "power_limit": DEFAULT_SHED_SUSTAINED_LIMIT,
        "duration_s": DEFAULT_SHED_SUSTAINED_DURATION,
    }
    return {
        vol.Required(CONF_THRESHOLD_POWER, default=pair["power_limit"]): selector.NumberSelector(
            cast(Any, selector.NumberSelectorConfig)(
                min=1,
                max=DEFAULT_HARD_INTERLOCK,
                mode="box",
                unit_of_measurement="W",
            )
        ),
        vol.Required(CONF_THRESHOLD_DURATION, default=pair["duration_s"]): selector.NumberSelector(
            cast(Any, selector.NumberSelectorConfig)(
                min=0,
                max=86400,
                mode="box",
                unit_of_measurement="s",
            )
        ),
        vol.Optional(CONF_ADD_THRESHOLD, default=add_another_default): bool,
    }


def _next_threshold_default(
    collected: list[dict[str, float]], seed: list[dict[str, float]]
) -> dict[str, float]:
    """Return the next useful default for a repeatable threshold step."""
    if len(collected) < len(seed):
        return dict(seed[len(collected)])
    previous = collected[-1] if collected else {"power_limit": 0.0, "duration_s": 0.0}
    return {
        "power_limit": min(DEFAULT_HARD_INTERLOCK, previous["power_limit"] + 500.0),
        "duration_s": previous["duration_s"],
    }


def _parse_threshold_step(
    user_input: Mapping[str, Any], previous: float
) -> tuple[dict[str, float] | None, str | None]:
    """Validate one repeatable threshold pair against the collected prefix."""
    raw_power = user_input.get(CONF_THRESHOLD_POWER)
    raw_duration = user_input.get(CONF_THRESHOLD_DURATION)
    if (
        raw_power is None
        or raw_duration is None
        or isinstance(raw_power, bool)
        or isinstance(raw_duration, bool)
    ):
        return None, "invalid_thresholds"
    try:
        limit, duration = validate_threshold_pair(
            float(raw_power),
            float(raw_duration),
            previous,
        )
    except (TypeError, ValueError):
        return None, "invalid_thresholds"
    if limit > DEFAULT_HARD_INTERLOCK:
        return None, "invalid_thresholds"
    return {"power_limit": limit, "duration_s": duration}, None


def _threshold_add_allowed(
    collected: list[dict[str, float]], pair: Mapping[str, float], user_input: Mapping[str, Any]
) -> bool:
    """Prevent an add-another request that cannot produce a valid next step."""
    if not user_input.get(CONF_ADD_THRESHOLD, False):
        return True
    return (
        pair["power_limit"] < DEFAULT_HARD_INTERLOCK and len(collected) + 1 < MAX_CUSTOM_THRESHOLDS
    )


def _optional_entity_key(name: str, default: Any = None) -> Any:
    """Avoid injecting an empty string into HA's native EntitySelector."""
    if isinstance(default, str) and default.strip():
        return vol.Optional(name, default=default)
    return vol.Optional(name)


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
                candidate = source.get("power_config", {}).get("stat_rate") or source.get(
                    "stat_rate"
                )
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
        entity = _entity_id(
            raw.get(CONF_DEVICE_ENTITY), frozenset({"switch", "light", "input_boolean"})
        )
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
            actuator = _entity_id(
                raw_actuator, frozenset({"switch", "light", "input_boolean", "climate"})
            )
            if (
                actuator is None
                or actuator == entity
                or actuator in actuators
                or actuator in seen_entities
            ):
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
    raw_devices = _entry_current(entry, CONF_DEVICES, [])
    try:
        current_devices = _normalize_options_devices(raw_devices)
    except ValueError:
        current_devices = []
    control_entities = [
        device[CONF_DEVICE_ENTITY] for device in current_devices if device.get(CONF_DEVICE_ENTITY)
    ]
    fields: dict[Any, Any] = {
        vol.Required(
            CONF_LOAD_SENSOR, default=_entry_current(entry, CONF_LOAD_SENSOR, "")
        ): _entity_selector("sensor"),
        vol.Optional(CONF_DEVICES, default=raw_devices): selector.ObjectSelector(),
        vol.Required(
            CONF_MAX_LOAD, default=_entry_current(entry, CONF_MAX_LOAD, 5000)
        ): selector.NumberSelector(
            cast(Any, selector.NumberSelectorConfig)(
                min=100, max=50000, mode="box", unit_of_measurement="W"
            )
        ),
        vol.Required(
            CONF_AVERAGING_PERIOD,
            default=_entry_current(entry, CONF_AVERAGING_PERIOD, DEFAULT_AVERAGING_PERIOD),
        ): selector.NumberSelector(
            cast(Any, selector.NumberSelectorConfig)(
                min=1, max=300, mode="box", unit_of_measurement="s"
            )
        ),
        vol.Required(
            CONF_SAFETY_RESERVE,
            default=_entry_current(entry, CONF_SAFETY_RESERVE, DEFAULT_SAFETY_RESERVE),
        ): selector.NumberSelector(
            cast(Any, selector.NumberSelectorConfig)(
                min=0, max=5000, mode="box", unit_of_measurement="W"
            )
        ),
        vol.Required(
            CONF_HYSTERESIS, default=_entry_current(entry, CONF_HYSTERESIS, DEFAULT_HYSTERESIS)
        ): selector.NumberSelector(
            cast(Any, selector.NumberSelectorConfig)(
                min=0, max=5000, mode="box", unit_of_measurement="W"
            )
        ),
        vol.Required(
            CONF_PAUSE_PERIOD,
            default=_entry_current(entry, CONF_PAUSE_PERIOD, DEFAULT_PAUSE_PERIOD),
        ): selector.NumberSelector(
            cast(Any, selector.NumberSelectorConfig)(
                min=0, max=86400, mode="box", unit_of_measurement="s"
            )
        ),
        vol.Required(
            CONF_GRID_LOSS_MODE,
            default=_entry_current(entry, CONF_GRID_LOSS_MODE, GRID_LOSS_MODE_SENSOR),
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    selector.SelectOptionDict(
                        value=GRID_LOSS_MODE_SENSOR, label="Grid loss sensor"
                    ),
                    selector.SelectOptionDict(
                        value=GRID_LOSS_MODE_THRESHOLD, label="Battery threshold"
                    ),
                ]
            )
        ),
        _optional_entity_key(
            CONF_GRID_LOSS_SENSOR, _entry_current(entry, CONF_GRID_LOSS_SENSOR)
        ): _entity_selector("binary_sensor"),
        _optional_entity_key(
            CONF_BATTERY_SOC, _entry_current(entry, CONF_BATTERY_SOC)
        ): _entity_selector("sensor"),
        vol.Optional(
            CONF_BATTERY_THRESHOLD, default=_entry_current(entry, CONF_BATTERY_THRESHOLD, 20)
        ): selector.NumberSelector(
            cast(Any, selector.NumberSelectorConfig)(
                min=0, max=100, mode="box", unit_of_measurement="%"
            )
        ),
    }
    if control_entities:
        fields[vol.Optional(CONF_PRIORITY_ORDER, default=control_entities)] = (
            selector.EntitySelector(
                selector.EntitySelectorConfig(
                    include_entities=control_entities,
                    multiple=True,
                    reorder=True,
                )
            )
        )
    return vol.Schema(fields)


def _validate_threshold_collection(value: Any) -> list[dict[str, float]]:
    """Validate a structured threshold list at the options boundary."""
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= MAX_CUSTOM_THRESHOLDS:
        raise ValueError("invalid thresholds")
    parsed: list[dict[str, float]] = []
    previous = 0.0
    for raw in value:
        if not isinstance(raw, Mapping):
            raise ValueError("invalid thresholds")
        try:
            limit, duration = validate_threshold_pair(
                float(raw.get("power_limit", raw.get("limit_w"))),
                float(raw.get("duration_s", raw.get("time_s"))),
                previous,
            )
        except (TypeError, ValueError):
            raise ValueError("invalid thresholds") from None
        if limit > DEFAULT_HARD_INTERLOCK:
            raise ValueError("invalid thresholds")
        parsed.append({"power_limit": limit, "duration_s": duration})
        previous = limit
    return parsed


def _apply_priority_order(
    devices: list[dict[str, Any]], selected_entities: Any
) -> list[dict[str, Any]]:
    """Apply an exact native-selector permutation to logical devices."""
    if selected_entities is None:
        return devices
    if isinstance(selected_entities, str):
        selected_entities = [selected_entities]
    by_entity = {
        device[CONF_DEVICE_ENTITY]: device for device in devices if device.get(CONF_DEVICE_ENTITY)
    }
    if (
        not isinstance(selected_entities, list)
        or len(selected_entities) != len(by_entity)
        or len(set(selected_entities)) != len(selected_entities)
        or set(selected_entities) != set(by_entity)
    ):
        raise ValueError("invalid priority order")
    ordered = [by_entity[entity_id] for entity_id in selected_entities]
    for index, device in enumerate(ordered, start=1):
        device[CONF_PRIORITY] = index
        device[CONF_SHED_PRIORITY] = index
    return ordered


def _prepare_options_submission(
    entry: Any, user_input: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, list[dict[str, float]] | None, dict[str, str]]:
    """Validate non-repeatable options and return a clean persisted payload."""
    errors: dict[str, str] = {}
    load_sensor = _sensor_entity_id(
        user_input.get(CONF_LOAD_SENSOR, _entry_current(entry, CONF_LOAD_SENSOR, ""))
    )
    if load_sensor is None:
        errors["base"] = "invalid_load_sensor"

    mode = user_input.get(
        CONF_GRID_LOSS_MODE, _entry_current(entry, CONF_GRID_LOSS_MODE, GRID_LOSS_MODE_SENSOR)
    )
    if mode not in (GRID_LOSS_MODE_SENSOR, GRID_LOSS_MODE_THRESHOLD):
        errors["base"] = "invalid_grid_loss_mode"

    try:
        devices = _normalize_options_devices(
            user_input.get(CONF_DEVICES, _entry_current(entry, CONF_DEVICES, []))
        )
        devices = _apply_priority_order(devices, user_input.get(CONF_PRIORITY_ORDER))
    except ValueError:
        devices = []
        errors.setdefault(
            "base",
            "invalid_priority_order" if CONF_PRIORITY_ORDER in user_input else "invalid_devices",
        )

    grid_sensor = _entity_id(
        user_input.get(CONF_GRID_LOSS_SENSOR, _entry_current(entry, CONF_GRID_LOSS_SENSOR, "")),
        frozenset({"binary_sensor"}),
    )
    battery_soc = _sensor_entity_id(
        user_input.get(CONF_BATTERY_SOC, _entry_current(entry, CONF_BATTERY_SOC, ""))
    )
    if mode == GRID_LOSS_MODE_SENSOR and grid_sensor is None:
        errors["base"] = "missing_grid_loss_sensor"
    if mode == GRID_LOSS_MODE_THRESHOLD and battery_soc is None:
        errors["base"] = "missing_battery_soc_sensor"

    numeric_specs: dict[str, tuple[Any, float, float]] = {
        CONF_MAX_LOAD: (5000, 100, 50000),
        CONF_AVERAGING_PERIOD: (DEFAULT_AVERAGING_PERIOD, 1, 300),
        CONF_SAFETY_RESERVE: (DEFAULT_SAFETY_RESERVE, 0, 5000),
        CONF_HYSTERESIS: (DEFAULT_HYSTERESIS, 0, 5000),
        CONF_PAUSE_PERIOD: (DEFAULT_PAUSE_PERIOD, 0, 86400),
    }
    if mode == GRID_LOSS_MODE_THRESHOLD:
        numeric_specs[CONF_BATTERY_THRESHOLD] = (20, 0, 100)
    values: dict[str, int | float] = {}
    for key, (default, minimum, maximum) in numeric_specs.items():
        raw = user_input.get(key, _entry_current(entry, key, default))
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

    submitted_thresholds: list[dict[str, float]] | None = None
    if CONF_THRESHOLD_COUNT in user_input:
        submitted_thresholds, threshold_error = _parse_threshold_input(user_input)
        if threshold_error:
            errors.setdefault("base", threshold_error)
    elif CONF_THRESHOLDS in user_input:
        try:
            submitted_thresholds = _validate_threshold_collection(user_input[CONF_THRESHOLDS])
        except ValueError:
            errors.setdefault("base", "invalid_thresholds")

    if errors:
        return None, submitted_thresholds, errors

    normalized: dict[str, Any] = {
        CONF_LOAD_SENSOR: load_sensor,
        CONF_DEVICES: devices,
        CONF_GRID_LOSS_MODE: mode,
    }
    normalized.update(values)
    if mode == GRID_LOSS_MODE_SENSOR:
        normalized[CONF_GRID_LOSS_SENSOR] = grid_sensor
    else:
        normalized[CONF_BATTERY_SOC] = battery_soc
        normalized[CONF_BATTERY_THRESHOLD] = values[CONF_BATTERY_THRESHOLD]
    return normalized, submitted_thresholds, {}


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
        self._thresholds: list[dict[str, float]] = []
        self._threshold_seed: list[dict[str, float]] = []
        self._selected_grid_loss_mode: str | None = None
        self._reconfigure_entry: Any | None = None
        self._reconfigure_pending: dict[str, Any] | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: Any) -> "PowerOrchestratorOptionsFlow":
        """Return the native options flow for Home Assistant's flow manager."""
        return PowerOrchestratorOptionsFlow(config_entry)

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
            entries: Any = getattr(
                getattr(self.hass, "config_entries", None), "async_entries", lambda *_: []
            )(DOMAIN)
            if isinstance(entries, (list, tuple)) and entries:
                return self.async_abort(reason="single_instance")
            self._discovered = await _discover_inputs(self.hass)
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {
                        _optional_entity_key(
                            "grid_power", self._discovered.get("grid_power")
                        ): _entity_selector("sensor"),
                        _optional_entity_key(
                            CONF_BATTERY_SOC, self._discovered.get(CONF_BATTERY_SOC)
                        ): _entity_selector("sensor"),
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
                    vol.Required(
                        CONF_LOAD_SENSOR, default=self._discovered.get("grid_power") or ""
                    ): _entity_selector("sensor"),
                    vol.Required(CONF_MAX_LOAD, default=5000): selector.NumberSelector(
                        cast(Any, selector.NumberSelectorConfig)(
                            min=100, max=50000, mode="box", unit_of_measurement="W"
                        )
                    ),
                    vol.Required(
                        CONF_AVERAGING_PERIOD, default=DEFAULT_AVERAGING_PERIOD
                    ): selector.NumberSelector(
                        cast(Any, selector.NumberSelectorConfig)(
                            min=1, max=300, mode="box", unit_of_measurement="s"
                        )
                    ),
                    vol.Required(
                        CONF_SAFETY_RESERVE, default=DEFAULT_SAFETY_RESERVE
                    ): selector.NumberSelector(
                        cast(Any, selector.NumberSelectorConfig)(
                            min=0, max=5000, mode="box", unit_of_measurement="W"
                        )
                    ),
                    vol.Required(
                        CONF_HYSTERESIS, default=DEFAULT_HYSTERESIS
                    ): selector.NumberSelector(
                        cast(Any, selector.NumberSelectorConfig)(
                            min=0, max=5000, mode="box", unit_of_measurement="W"
                        )
                    ),
                }
            ),
            errors=errors or {},
            description_placeholders={
                "sensor_name": self._discovered.get("grid_power") or "selected sensor"
            },
        )

    async def async_step_load_monitoring(self, user_input: dict[str, Any] | None = None) -> Any:
        if user_input is None:
            return self._load_monitoring_form()
        self._discovered.update(
            {
                "grid_power": user_input.get(CONF_LOAD_SENSOR),
                CONF_MAX_LOAD: user_input.get(CONF_MAX_LOAD, 5000),
                CONF_AVERAGING_PERIOD: user_input.get(
                    CONF_AVERAGING_PERIOD, DEFAULT_AVERAGING_PERIOD
                ),
                CONF_SAFETY_RESERVE: user_input.get(CONF_SAFETY_RESERVE, DEFAULT_SAFETY_RESERVE),
                CONF_HYSTERESIS: user_input.get(CONF_HYSTERESIS, DEFAULT_HYSTERESIS),
            }
        )
        # Keep accepting the old numbered payload for callers that submit an
        # already-rendered legacy form, while the user-facing flow uses the
        # repeatable threshold step below.
        if CONF_THRESHOLD_COUNT in user_input:
            thresholds, error = _parse_threshold_input(user_input)
            if error or thresholds is None:
                return self._load_monitoring_form({"base": error or "invalid_thresholds"})
            self._discovered[CONF_THRESHOLDS] = thresholds
            return await self.async_step_devices()
        self._thresholds = []
        self._threshold_seed = _threshold_defaults(None)
        return await self.async_step_thresholds()

    def _threshold_form(self, errors: dict[str, str] | None = None) -> Any:
        default = _next_threshold_default(self._thresholds, self._threshold_seed)
        return self.async_show_form(
            step_id="thresholds",
            data_schema=vol.Schema(
                _threshold_step_fields(
                    default,
                    add_another_default=len(self._thresholds) + 1 < len(self._threshold_seed),
                )
            ),
            errors=errors or {},
            description_placeholders={
                "index": str(len(self._thresholds) + 1),
                "count": str(len(self._thresholds)),
            },
        )

    async def async_step_thresholds(self, user_input: dict[str, Any] | None = None) -> Any:
        """Collect one or more threshold pairs without fixed UI positions."""
        if self._reconfigure_pending is not None:
            return await self.async_step_reconfigure_thresholds(user_input)
        if user_input is None:
            return self._threshold_form()
        previous = self._thresholds[-1]["power_limit"] if self._thresholds else 0.0
        pair, error = _parse_threshold_step(user_input, previous)
        if pair is None or error:
            return self._threshold_form({"base": error or "invalid_thresholds"})
        if len(self._thresholds) >= MAX_CUSTOM_THRESHOLDS or not _threshold_add_allowed(
            self._thresholds, pair, user_input
        ):
            return self._threshold_form({"base": "invalid_thresholds"})
        self._thresholds.append(pair)
        if user_input.get(CONF_ADD_THRESHOLD, False):
            return await self.async_step_thresholds()
        self._discovered[CONF_THRESHOLDS] = [dict(item) for item in self._thresholds]
        return await self.async_step_devices()

    def _device_name(self, candidate: Mapping[str, Any]) -> str:
        return str(
            candidate.get("name") or _friendly(self.hass, str(candidate.get("entity_id") or ""))
        )

    def _device_selection_form(self, errors: dict[str, str] | None = None) -> Any:
        options = [
            selector.SelectOptionDict(value=item["entity_id"], label=self._device_name(item))
            for item in self._discovered.get("devices", [])
            if isinstance(item, dict) and item.get("entity_id")
        ]
        discovered = ", ".join(option["label"] for option in options) or "none discovered"
        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_DISCOVERED_DEVICES, default=[]): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=options, multiple=True)
                    ),
                    vol.Optional(CONF_ADD_CUSTOM_DEVICE, default=False): bool,
                }
            ),
            errors=errors or {},
            description_placeholders={"count": str(len(self._devices)), "discovered": discovered},
        )

    def _device_config_form(
        self, candidate: dict[str, Any] | None = None, errors: dict[str, str] | None = None
    ) -> Any:
        candidate = candidate or {}
        candidate_name = self._device_name(candidate) if candidate else ""
        power_sensor = _sensor_entity_id(candidate.get("power_sensor")) or ""
        status = f"{candidate_name or 'Custom device'} — measured-power sensor: {power_sensor or 'not selected'}"
        fields: dict[Any, Any] = {
            vol.Required(CONF_DEVICE_ENTITY): _entity_selector(
                ["switch", "light", "input_boolean"]
            ),
            vol.Optional(CONF_DEVICE_NAME, default=candidate_name): selector.TextSelector(),
            vol.Required(
                CONF_DEVICE_EXPECTED_POWER, default=candidate.get(CONF_DEVICE_EXPECTED_POWER, 2000)
            ): selector.NumberSelector(
                cast(Any, selector.NumberSelectorConfig)(
                    min=1, max=50000, mode="box", unit_of_measurement="W"
                )
            ),
            _optional_entity_key(CONF_DEVICE_POWER_SENSOR, power_sensor): _entity_selector(
                "sensor"
            ),
            vol.Optional(
                CONF_DEVICE_ACTUATORS, default=list(candidate.get(CONF_DEVICE_ACTUATORS, ()) or ())
            ): _entity_selector(["switch", "light", "input_boolean", "climate"], multiple=True),
        }
        if not candidate:
            fields[vol.Optional(CONF_ADD_ANOTHER, default=False)] = bool
        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema(fields),
            errors=errors or {},
            description_placeholders={"count": str(len(self._devices)), "discovered": status},
        )

    def _build_device(
        self, user_input: dict[str, Any], candidate: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        candidate = candidate or {}
        entity = _entity_id(
            user_input.get(CONF_DEVICE_ENTITY), frozenset({"switch", "light", "input_boolean"})
        )
        if entity is None:
            raise ValueError("control entity must be valid")
        name = user_input.get(CONF_DEVICE_NAME) or (
            self._device_name(candidate) if candidate else _friendly(self.hass, entity)
        )
        power_sensor = (
            _sensor_entity_id(user_input.get(CONF_DEVICE_POWER_SENSOR))
            if CONF_DEVICE_POWER_SENSOR in user_input
            else _sensor_entity_id(candidate.get("power_sensor"))
        )
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
            candidates = {
                item["entity_id"]: item
                for item in self._discovered.get("devices", [])
                if isinstance(item, dict) and item.get("entity_id")
            }
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
        """Legacy numbered priority field accepted from older callers."""
        return f"priority_{index + 1}"

    def _priority_form(self, errors: dict[str, str] | None = None) -> Any:
        control_entities = [
            str(device[CONF_DEVICE_ENTITY])
            for device in self._devices
            if device.get(CONF_DEVICE_ENTITY)
        ]
        fields: dict[Any, Any] = {}
        if control_entities:
            fields[vol.Required(CONF_PRIORITY_ORDER, default=control_entities)] = (
                selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        include_entities=control_entities,
                        multiple=True,
                        reorder=True,
                    )
                )
            )
        fields[vol.Optional(CONF_PAUSE_PERIOD, default=DEFAULT_PAUSE_PERIOD)] = (
            selector.NumberSelector(
                cast(Any, selector.NumberSelectorConfig)(
                    min=0,
                    max=86400,
                    mode="box",
                    unit_of_measurement="s",
                )
            )
        )
        return self.async_show_form(
            step_id="priority",
            data_schema=vol.Schema(fields),
            errors=errors or {},
            description_placeholders={
                "device_list": "\n".join(
                    f"{i + 1}. {d.get(CONF_DEVICE_NAME, d.get(CONF_DEVICE_ENTITY))}"
                    for i, d in enumerate(self._devices)
                )
                or "none configured"
            },
        )

    async def async_step_priority(self, user_input: dict[str, Any] | None = None) -> Any:
        if user_input is None:
            return self._priority_form()
        if not self._devices:
            self._pause_period = user_input.get(CONF_PAUSE_PERIOD, DEFAULT_PAUSE_PERIOD)
            return await self.async_step_grid_loss()

        selected_entities = user_input.get(CONF_PRIORITY_ORDER)
        if selected_entities is not None:
            if isinstance(selected_entities, str):
                selected_entities = [selected_entities]
            by_entity = {
                device.get(CONF_DEVICE_ENTITY): device
                for device in self._devices
                if device.get(CONF_DEVICE_ENTITY)
            }
            if (
                not isinstance(selected_entities, list)
                or len(selected_entities) != len(by_entity)
                or len(set(selected_entities)) != len(selected_entities)
                or set(selected_entities) != set(by_entity)
            ):
                return self._priority_form({"base": "invalid_priority_order"})
            self._devices = [by_entity[entity_id] for entity_id in selected_entities]
        else:
            legacy_ids = [
                user_input.get(self._priority_field(index)) for index in range(len(self._devices))
            ]
            by_id = {device.get(CONF_DEVICE_ID): device for device in self._devices}
            if (
                len(legacy_ids) != len(by_id)
                or len(set(legacy_ids)) != len(legacy_ids)
                or set(legacy_ids) != set(by_id)
            ):
                return self._priority_form({"base": "invalid_priority_order"})
            self._devices = [by_id[device_id] for device_id in legacy_ids]

        for index, device in enumerate(self._devices, start=1):
            device[CONF_PRIORITY] = index
            device[CONF_SHED_PRIORITY] = index
        self._pause_period = user_input.get(CONF_PAUSE_PERIOD, DEFAULT_PAUSE_PERIOD)
        return await self.async_step_grid_loss()

    def _battery_info(self) -> str:
        soc = self._discovered.get(CONF_BATTERY_SOC)
        if soc:
            return f"Battery SoC discovered: {soc}. It is used only in battery-threshold mode."
        return "Battery SoC is optional unless battery-threshold mode is selected."

    def _grid_loss_form(self, errors: dict[str, str] | None = None) -> Any:
        """Select the grid-loss mode before rendering mode-specific fields."""
        return self.async_show_form(
            step_id="grid_loss",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_GRID_LOSS_MODE,
                        default=self._selected_grid_loss_mode or GRID_LOSS_MODE_SENSOR,
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=GRID_LOSS_MODE_SENSOR, label="Grid loss sensor"
                                ),
                                selector.SelectOptionDict(
                                    value=GRID_LOSS_MODE_THRESHOLD, label="Battery threshold"
                                ),
                            ]
                        )
                    ),
                }
            ),
            errors=errors or {},
            description_placeholders={"battery_info": self._battery_info()},
        )

    def _grid_loss_source_form(self, mode: str, errors: dict[str, str] | None = None) -> Any:
        fields: dict[Any, Any] = {}
        if mode == GRID_LOSS_MODE_SENSOR:
            fields[vol.Required(CONF_GRID_LOSS_SENSOR)] = _entity_selector("binary_sensor")
        else:
            soc = self._discovered.get(CONF_BATTERY_SOC)
            if soc:
                fields[vol.Required(CONF_BATTERY_SOC, default=soc)] = _entity_selector("sensor")
            else:
                fields[vol.Required(CONF_BATTERY_SOC)] = _entity_selector("sensor")
            fields[vol.Required(CONF_BATTERY_THRESHOLD, default=20)] = selector.NumberSelector(
                cast(Any, selector.NumberSelectorConfig)(
                    min=0,
                    max=100,
                    mode="box",
                    unit_of_measurement="%",
                )
            )
        return self.async_show_form(
            step_id="grid_loss_source",
            data_schema=vol.Schema(fields),
            errors=errors or {},
            description_placeholders={"battery_info": self._battery_info()},
        )

    async def _create_grid_loss_entry(self, user_input: dict[str, Any], mode: str) -> Any:
        grid_sensor = _entity_id(
            user_input.get(CONF_GRID_LOSS_SENSOR), frozenset({"binary_sensor"})
        )
        battery_soc = _sensor_entity_id(
            user_input.get(CONF_BATTERY_SOC) or self._discovered.get(CONF_BATTERY_SOC)
        )
        if mode == GRID_LOSS_MODE_SENSOR and grid_sensor is None:
            return self._grid_loss_source_form(mode, {"base": "missing_grid_loss_sensor"})
        if mode == GRID_LOSS_MODE_THRESHOLD and battery_soc is None:
            return self._grid_loss_source_form(mode, {"base": "missing_battery_soc_sensor"})
        data = {
            CONF_LOAD_SENSOR: self._discovered.get("grid_power"),
            CONF_MAX_LOAD: self._discovered.get(CONF_MAX_LOAD, 5000),
            CONF_AVERAGING_PERIOD: self._discovered.get(
                CONF_AVERAGING_PERIOD, DEFAULT_AVERAGING_PERIOD
            ),
            CONF_SAFETY_RESERVE: self._discovered.get(CONF_SAFETY_RESERVE, DEFAULT_SAFETY_RESERVE),
            CONF_HYSTERESIS: self._discovered.get(CONF_HYSTERESIS, DEFAULT_HYSTERESIS),
            CONF_THRESHOLDS: self._discovered.get(
                CONF_THRESHOLDS, [dict(item) for item in _CANONICAL_THRESHOLD_INPUTS]
            ),
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

    async def async_step_grid_loss(self, user_input: dict[str, Any] | None = None) -> Any:
        if user_input is None:
            return self._grid_loss_form()
        mode = user_input.get(CONF_GRID_LOSS_MODE, GRID_LOSS_MODE_SENSOR)
        if mode not in (GRID_LOSS_MODE_SENSOR, GRID_LOSS_MODE_THRESHOLD):
            return self._grid_loss_form({"base": "invalid_grid_loss_mode"})
        self._selected_grid_loss_mode = mode
        # Accept the old one-screen submission shape for compatibility with
        # existing callers and migrations.
        if CONF_GRID_LOSS_SENSOR in user_input or CONF_BATTERY_SOC in user_input:
            return await self._create_grid_loss_entry(user_input, mode)
        return self._grid_loss_source_form(mode)

    async def async_step_grid_loss_source(self, user_input: dict[str, Any] | None = None) -> Any:
        mode = self._selected_grid_loss_mode or GRID_LOSS_MODE_SENSOR
        if user_input is None:
            return self._grid_loss_source_form(mode)
        return await self._create_grid_loss_entry(user_input, mode)

    def _reconfigure_threshold_form(self, errors: dict[str, str] | None = None) -> Any:
        default = _next_threshold_default(self._thresholds, self._threshold_seed)
        return self.async_show_form(
            step_id="thresholds",
            data_schema=vol.Schema(
                _threshold_step_fields(
                    default,
                    add_another_default=len(self._thresholds) + 1 < len(self._threshold_seed),
                )
            ),
            errors=errors or {},
            description_placeholders={
                "index": str(len(self._thresholds) + 1),
                "count": str(len(self._thresholds)),
            },
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> Any:
        entry = self._get_reconfigure_entry()
        if entry is None:
            return self.async_abort(reason="entry_not_loaded")
        schema = _options_schema_for_entry(entry)
        if user_input is None:
            return self.async_show_form(step_id="reconfigure", data_schema=schema)
        prepared, submitted_thresholds, errors = _prepare_options_submission(entry, user_input)
        if errors or prepared is None:
            return self.async_show_form(
                step_id="reconfigure",
                data_schema=schema,
                errors=errors,
            )
        self._reconfigure_entry = entry
        if submitted_thresholds is not None:
            prepared[CONF_THRESHOLDS] = submitted_thresholds
            return self.async_update_and_abort(
                entry,
                data=prepared,
                options={},
                reason="reconfigure_successful",
            )
        self._reconfigure_pending = prepared
        self._thresholds = []
        self._threshold_seed = _threshold_defaults(_entry_current(entry, CONF_THRESHOLDS, None))
        return self._reconfigure_threshold_form()

    async def async_step_reconfigure_thresholds(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        if self._reconfigure_entry is None or self._reconfigure_pending is None:
            return self.async_abort(reason="entry_not_loaded")
        if user_input is None:
            return self._reconfigure_threshold_form()
        previous = self._thresholds[-1]["power_limit"] if self._thresholds else 0.0
        pair, error = _parse_threshold_step(user_input, previous)
        if pair is None or error:
            return self._reconfigure_threshold_form({"base": error or "invalid_thresholds"})
        if len(self._thresholds) >= MAX_CUSTOM_THRESHOLDS or not _threshold_add_allowed(
            self._thresholds, pair, user_input
        ):
            return self._reconfigure_threshold_form({"base": "invalid_thresholds"})
        self._thresholds.append(pair)
        if user_input.get(CONF_ADD_THRESHOLD, False):
            return self._reconfigure_threshold_form()
        self._reconfigure_pending[CONF_THRESHOLDS] = [dict(item) for item in self._thresholds]
        return self.async_update_and_abort(
            self._reconfigure_entry,
            data=self._reconfigure_pending,
            options={},
            reason="reconfigure_successful",
        )


class PowerOrchestratorOptionsFlow(config_entries.OptionsFlow):
    """Validate runtime/safety options without any activation fields."""

    def __init__(self, entry: Any) -> None:
        self._entry = entry
        self._pending: dict[str, Any] | None = None
        self._thresholds: list[dict[str, float]] = []
        self._threshold_seed: list[dict[str, float]] = []

    def _current(self, key: str, default: Any = None) -> Any:
        return _entry_current(self._entry, key, default)

    def _options_schema(self) -> vol.Schema:
        return _options_schema_for_entry(self._entry)

    def _threshold_form(self, errors: dict[str, str] | None = None) -> Any:
        default = _next_threshold_default(self._thresholds, self._threshold_seed)
        return self.async_show_form(
            step_id="thresholds",
            data_schema=vol.Schema(
                _threshold_step_fields(
                    default,
                    add_another_default=len(self._thresholds) + 1 < len(self._threshold_seed),
                )
            ),
            errors=errors or {},
            description_placeholders={
                "index": str(len(self._thresholds) + 1),
                "count": str(len(self._thresholds)),
            },
        )

    def _finish(self) -> Any:
        if self._pending is None:
            return self.async_abort(reason="invalid_options_state")
        self._pending[CONF_THRESHOLDS] = [dict(item) for item in self._thresholds]
        return self.async_create_entry(title="", data=self._pending)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> Any:
        if user_input is None:
            return self.async_show_form(step_id="init", data_schema=self._options_schema())
        prepared, submitted_thresholds, errors = _prepare_options_submission(
            self._entry, user_input
        )
        if errors or prepared is None:
            return self.async_show_form(
                step_id="init",
                data_schema=self._options_schema(),
                errors=errors,
            )
        if submitted_thresholds is not None:
            prepared[CONF_THRESHOLDS] = submitted_thresholds
            return self.async_create_entry(title="", data=prepared)
        self._pending = prepared
        self._thresholds = []
        self._threshold_seed = _threshold_defaults(self._current(CONF_THRESHOLDS, None))
        return self._threshold_form()

    async def async_step_thresholds(self, user_input: dict[str, Any] | None = None) -> Any:
        if self._pending is None:
            return self.async_abort(reason="invalid_options_state")
        if user_input is None:
            return self._threshold_form()
        previous = self._thresholds[-1]["power_limit"] if self._thresholds else 0.0
        pair, error = _parse_threshold_step(user_input, previous)
        if pair is None or error:
            return self._threshold_form({"base": error or "invalid_thresholds"})
        if len(self._thresholds) >= MAX_CUSTOM_THRESHOLDS or not _threshold_add_allowed(
            self._thresholds, pair, user_input
        ):
            return self._threshold_form({"base": "invalid_thresholds"})
        self._thresholds.append(pair)
        if user_input.get(CONF_ADD_THRESHOLD, False):
            return self._threshold_form()
        return self._finish()


@callback
def async_get_options_flow(config_entry: Any) -> PowerOrchestratorOptionsFlow:
    return PowerOrchestratorOptionsFlow(config_entry)
