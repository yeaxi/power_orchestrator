"""Power Orchestrator integration entry point."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
from collections.abc import Mapping
from typing import Any, cast

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

try:
    from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
except ImportError:  # pragma: no cover - local test doubles
    class HomeAssistantError(Exception):  # type: ignore[no-redef]
        """Fallback Home Assistant error."""

    class ServiceValidationError(HomeAssistantError):  # type: ignore[no-redef]
        """Fallback service validation error."""

from .const import (
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
    CONF_EXECUTION_MODE,
    CONF_GRID_LOSS_MODE,
    CONF_GRID_LOSS_SENSOR,
    CONF_HYSTERESIS,
    CONF_LOAD_SENSOR,
    CONF_MAX_LOAD,
    CONF_PAUSE_PERIOD,
    CONF_PRIORITY,
    CONF_SAFETY_RESERVE,
    CONF_SHED_PRIORITY,
    CONF_THRESHOLDS,
    DEFAULT_AVERAGING_PERIOD,
    DEFAULT_EXECUTION_MODE,
    DEFAULT_HYSTERESIS,
    DEFAULT_PAUSE_PERIOD,
    DEFAULT_SAFETY_RESERVE,
    DOMAIN,
    EXECUTION_MODE_LIVE,
    EXECUTION_MODE_OBSERVE,
    GRID_LOSS_MODE_SENSOR,
    MAX_RUNTIME_PAUSE_SECONDS,
    MODE_AUTO,
    MODE_OFF,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .coordinator import PowerOrchestratorCoordinator
from .policy import PolicyConfig
from .power_model import ManagedDevice, PowerModel
from .runtime import PowerOrchestratorRuntimeData
from .storage import RuntimeStore

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SELECT]
_REGISTERED_SERVICES = (
    "force_evaluate",
    "set_mode",
    "request_stop",
    "clear_quarantine",
    "set_execution_mode",
)
_REPAIR_ISSUE_IDS_KEY = f"{DOMAIN}_repair_issue_ids"
_ALLOWED_CONTROL_DOMAINS = frozenset({"switch", "light", "input_boolean"})
_ALLOWED_ACTUATOR_DOMAINS = frozenset({"switch", "light", "input_boolean", "climate"})


def _translated_error(
    exception_type: type[Exception],
    translation_key: str,
    *,
    reason: str | None = None,
) -> Exception:
    """Construct a translated HA exception with local-test compatibility."""
    placeholders = {"reason": reason} if reason else None
    try:
        return cast(Any, exception_type)(
            translation_domain=DOMAIN,
            translation_key=translation_key,
            translation_placeholders=placeholders,
        )
    except TypeError:
        return exception_type(reason or translation_key)


def _loaded_runtimes(hass: HomeAssistant) -> list[PowerOrchestratorRuntimeData]:
    """Return only runtimes with a usable coordinator."""
    container = getattr(hass, "data", {}).get(DOMAIN, {})
    if not isinstance(container, dict):
        return []
    result: list[PowerOrchestratorRuntimeData] = []
    for value in container.values():
        if getattr(value, "coordinator", None) is not None:
            result.append(value)
    return result


def _lifecycle_state(hass: HomeAssistant) -> dict[str, Any]:
    """Return the integration lifecycle registry, repairing malformed state."""
    data = getattr(hass, "data", None)
    if not isinstance(data, dict):
        data = {}
        setattr(hass, "data", data)
    key = f"{DOMAIN}_lifecycle"
    lifecycle = data.get(key)
    if not isinstance(lifecycle, dict):
        lifecycle = {}
        data[key] = lifecycle
    return lifecycle


def _repair_issue_id(entry_id: str, device_id: str) -> str:
    """Return a stable issue identifier without exposing logical IDs."""
    digest = hashlib.sha256(f"{entry_id}:{device_id}".encode()).hexdigest()[:20]
    return f"quarantine_{digest}"


def _repair_device_ids(hass: HomeAssistant, entry: ConfigEntry) -> set[str]:
    """Return currently faulted/quarantined logical IDs for diagnostics."""
    del hass
    runtime = getattr(entry, "runtime_data", None)
    coordinator = getattr(runtime, "coordinator", None)
    if coordinator is None:
        return set()
    data = getattr(coordinator, "data", None)
    if isinstance(data, Mapping):
        values: set[str] = set()
        for key in ("faulted_devices", "quarantined_devices"):
            raw = data.get(key, ())
            if isinstance(raw, (list, tuple, set, frozenset)):
                values.update(item for item in raw if isinstance(item, str) and item)
        if values:
            return values
    return set(getattr(coordinator, "_faulted", set())) | set(
        getattr(coordinator, "_quarantined", set())
    )


def _sync_repair_issues(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Mirror durable quarantine into persistent Home Assistant issues."""
    try:
        from homeassistant.helpers.issue_registry import (
            IssueSeverity,
            async_create_issue,
            async_delete_issue,
            async_get,
        )
    except ImportError:  # pragma: no cover - local Home Assistant test doubles
        return
    active_ids = _repair_device_ids(hass, entry)
    desired_ids = {_repair_issue_id(entry.entry_id, device_id) for device_id in active_ids}
    hass_data = getattr(hass, "data", None)
    if not isinstance(hass_data, dict):
        hass_data = {}
        setattr(hass, "data", hass_data)
    previous_by_entry = hass_data.setdefault(_REPAIR_ISSUE_IDS_KEY, {})
    if not isinstance(previous_by_entry, dict):
        previous_by_entry = {}
        hass_data[_REPAIR_ISSUE_IDS_KEY] = previous_by_entry
    previous_ids = previous_by_entry.get(entry.entry_id, set())
    if not isinstance(previous_ids, set):
        previous_ids = set(previous_ids) if isinstance(previous_ids, (list, tuple)) else set()
    try:
        registry = async_get(hass)
        issues = getattr(registry, "issues", {})
        registered_ids: set[str] = set()
        if isinstance(issues, Mapping):
            for key in issues:
                if (
                    isinstance(key, tuple)
                    and len(key) == 2
                    and key[0] == DOMAIN
                    and isinstance(key[1], str)
                    and key[1].startswith("quarantine_")
                ):
                    registered_ids.add(key[1])
    except Exception:  # pragma: no cover - issue registry is non-safety-critical
        _LOGGER.debug("Unable to inspect Power Orchestrator repair issues", exc_info=True)
        return
    try:
        for device_id in sorted(active_ids):
            async_create_issue(
                hass,
                DOMAIN,
                _repair_issue_id(entry.entry_id, device_id),
                is_fixable=False,
                is_persistent=True,
                issue_domain=DOMAIN,
                learn_more_url="https://github.com/yeaxi/power_orchestrator#troubleshooting",
                severity=IssueSeverity.ERROR,
                translation_key="quarantine_requires_reconciliation",
                translation_placeholders={"device_id": device_id},
            )
        for issue_id in sorted((previous_ids | registered_ids) - desired_ids):
            async_delete_issue(hass, DOMAIN, issue_id)
    except Exception:  # pragma: no cover - issue registry is non-safety-critical
        _LOGGER.debug("Unable to synchronize Power Orchestrator repair issues", exc_info=True)
        return
    previous_by_entry[entry.entry_id] = desired_ids


def _sync_repair_issues_for_runtime(
    hass: HomeAssistant, runtime: PowerOrchestratorRuntimeData
) -> None:
    """Synchronize issues when a service mutates runtime without a state event."""
    entries_api = getattr(getattr(hass, "config_entries", None), "async_entries", None)
    if not callable(entries_api):
        return
    try:
        entries = entries_api(DOMAIN)
    except Exception:  # pragma: no cover - defensive HA compatibility guard
        return
    if not isinstance(entries, (list, tuple)):
        return
    for entry in entries:
        if getattr(entry, "runtime_data", None) is runtime:
            _sync_repair_issues(hass, entry)
            return


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Initialize the integration registry."""
    del config
    hass.data.setdefault(DOMAIN, {})
    hass.data.setdefault(f"{DOMAIN}_lifecycle", {})
    return True


def _valid_entity_id(value: Any, domains: frozenset[str]) -> str | None:
    if not isinstance(value, str) or value.count(".") != 1:
        return None
    domain, object_id = value.split(".", 1)
    if domain not in domains or not object_id:
        return None
    return value


def _safe_number(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(converted) or not minimum <= converted <= maximum:
        return default
    return converted


def _normalize_devices(raw_devices: Any) -> list[dict[str, Any]]:
    """Normalize only fields needed for load shedding."""
    if not isinstance(raw_devices, list):
        return []
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_entities: set[str] = set()
    for index, raw in enumerate(raw_devices):
        if not isinstance(raw, Mapping):
            continue
        device_id = raw.get(CONF_DEVICE_ID)
        entity_id = _valid_entity_id(raw.get(CONF_DEVICE_ENTITY), _ALLOWED_CONTROL_DOMAINS)
        if not isinstance(device_id, str) or not device_id.strip() or entity_id is None:
            continue
        device_id = device_id.strip()
        if device_id in seen_ids or entity_id in seen_entities:
            continue
        actuators_raw = raw.get(CONF_DEVICE_ACTUATORS, ())
        if isinstance(actuators_raw, str):
            actuators_raw = (actuators_raw,)
        if not isinstance(actuators_raw, (list, tuple)):
            actuators_raw = ()
        actuators: list[str] = []
        for actuator in actuators_raw:
            valid = _valid_entity_id(actuator, _ALLOWED_ACTUATOR_DOMAINS)
            if valid and valid not in {entity_id, *actuators, *seen_entities}:
                actuators.append(valid)
        expected = int(math.ceil(_safe_number(
            raw.get(CONF_DEVICE_EXPECTED_POWER),
            default=1,
            minimum=1,
            maximum=50000,
        )))
        power_sensor = _valid_entity_id(raw.get(CONF_DEVICE_POWER_SENSOR), frozenset({"sensor"}))
        name = raw.get(CONF_DEVICE_NAME)
        if not isinstance(name, str) or not name.strip():
            name = entity_id
        priority = int(_safe_number(raw.get(CONF_PRIORITY, index + 1), default=index + 1, minimum=1, maximum=100000))
        shed_priority = int(_safe_number(raw.get(CONF_SHED_PRIORITY, priority), default=priority, minimum=1, maximum=100000))
        normalized.append(
            {
                CONF_DEVICE_ID: device_id,
                CONF_DEVICE_NAME: name.strip(),
                CONF_DEVICE_ENTITY: entity_id,
                CONF_DEVICE_EXPECTED_POWER: expected,
                CONF_DEVICE_POWER_SENSOR: power_sensor,
                CONF_PRIORITY: priority,
                CONF_SHED_PRIORITY: shed_priority,
                CONF_DEVICE_ACTUATORS: actuators,
            }
        )
        seen_ids.add(device_id)
        seen_entities.update((entity_id, *actuators))
    return normalized


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Load one singleton entry and restore its persisted mode before refresh."""
    lifecycle = _lifecycle_state(hass)
    lock = lifecycle.get("lock")
    if not isinstance(lock, asyncio.Lock):
        lock = asyncio.Lock()
        lifecycle["lock"] = lock
    reservations = lifecycle.get("reservations")
    if not isinstance(reservations, set):
        reservations = set()
        lifecycle["reservations"] = reservations
    async with lock:
        if (
            _loaded_runtimes(hass)
            or getattr(entry, "runtime_data", None) is not None
            or entry.entry_id in reservations
        ):
            _LOGGER.error("Refusing a second Power Orchestrator entry")
            return False
        reservations.add(entry.entry_id)
    try:
        return await _async_setup_entry_impl(hass, entry)
    except Exception:
        _LOGGER.exception("Power Orchestrator setup failed")
        entry.runtime_data = None
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        return False
    finally:
        async with lock:
            reservations.discard(entry.entry_id)


async def _async_setup_entry_impl(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = dict(entry.data or {})
    data.update(dict(entry.options or {}))
    devices = _normalize_devices(data.get(CONF_DEVICES, []))
    model = PowerModel()
    for device_data in devices:
        model.add_device(ManagedDevice.from_dict(device_data))

    store = RuntimeStore(Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}_{entry.entry_id}"))
    await store.async_load()
    policy = PolicyConfig.from_mapping(data)
    execution_mode = store.restore_execution_mode() or data.get(CONF_EXECUTION_MODE, DEFAULT_EXECUTION_MODE)
    if execution_mode not in (EXECUTION_MODE_LIVE, EXECUTION_MODE_OBSERVE):
        execution_mode = DEFAULT_EXECUTION_MODE
    coordinator = PowerOrchestratorCoordinator(
        hass=hass,
        model=model,
        store=store,
        load_sensor=str(data.get(CONF_LOAD_SENSOR, "")),
        max_load=_safe_number(data.get(CONF_MAX_LOAD), default=5000, minimum=100, maximum=50000),
        averaging_period=_safe_number(data.get(CONF_AVERAGING_PERIOD), default=DEFAULT_AVERAGING_PERIOD, minimum=1, maximum=300),
        safety_reserve=_safe_number(data.get(CONF_SAFETY_RESERVE), default=DEFAULT_SAFETY_RESERVE, minimum=0, maximum=5000),
        hysteresis=_safe_number(data.get(CONF_HYSTERESIS), default=DEFAULT_HYSTERESIS, minimum=0, maximum=5000),
        pause_period=_safe_number(data.get(CONF_PAUSE_PERIOD), default=DEFAULT_PAUSE_PERIOD, minimum=0, maximum=MAX_RUNTIME_PAUSE_SECONDS),
        grid_loss_mode=data.get(CONF_GRID_LOSS_MODE, GRID_LOSS_MODE_SENSOR),
        grid_loss_sensor=data.get(CONF_GRID_LOSS_SENSOR),
        battery_threshold=data.get(CONF_BATTERY_THRESHOLD),
        battery_soc_sensor=data.get(CONF_BATTERY_SOC),
        entry_id=entry.entry_id,
        policy=policy,
        execution_mode=execution_mode,
    )
    coordinator._safety_storage_invalid = store.safety_storage_invalid
    store.restore_pause_timestamps(model, MAX_RUNTIME_PAUSE_SECONDS)
    faulted, quarantined = store.restore_device_runtime(model)
    coordinator.restore_device_runtime(
        faulted,
        quarantined,
        fault_reasons=store.restore_fault_reasons(model),
        storage_invalid=store.safety_storage_invalid,
    )
    active_notifications, pending_notifications = store.restore_fault_notification_state(model)
    coordinator.restore_fault_notification_state(active_notifications, pending_notifications)
    coordinator.restore_action_journal(store.unresolved_actions())
    store.restore_policy_runtime(coordinator._policy_engine, model)
    restored_mode = MODE_OFF if store.safety_storage_invalid else store.restore_mode()
    coordinator.mode = restored_mode if restored_mode in (MODE_AUTO, MODE_OFF) else MODE_OFF

    runtime = PowerOrchestratorRuntimeData(coordinator=coordinator, model=model, store=store)
    entry.runtime_data = runtime
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime
    await _register_services(hass)

    tracked_entities = {data.get(CONF_LOAD_SENSOR), data.get(CONF_GRID_LOSS_SENSOR), data.get(CONF_BATTERY_SOC)}
    for device in model.all_devices():
        tracked_entities.update(device.control_entity_ids)
        if device.power_sensor_id:
            tracked_entities.add(device.power_sensor_id)
    tracked_entities.discard(None)

    async def _state_changed(event: Any) -> None:
        entity_id = getattr(event, "data", {}).get("entity_id") if event is not None else None
        if entity_id in tracked_entities:
            await coordinator.async_force_evaluate()
            _sync_repair_issues(hass, entry)

    bus = getattr(hass, "bus", None)
    listen = getattr(bus, "async_listen", None)
    if callable(listen):
        remove_listener = listen("state_changed", _state_changed)
        runtime.repair_listener_remove = remove_listener if callable(remove_listener) else None
        unload_listener = runtime.repair_listener_remove
        if callable(entry.async_on_unload) and callable(unload_listener):
            entry.async_on_unload(unload_listener)

    try:
        await coordinator.async_config_entry_first_refresh()
        _sync_repair_issues(hass, entry)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        if runtime.repair_listener_remove:
            runtime.repair_listener_remove()
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        entry.runtime_data = None
        raise
    if callable(entry.add_update_listener):
        entry.add_update_listener(_async_update_listener)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload options through Home Assistant's lifecycle."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Persist and unload one entry."""
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return False
    try:
        await runtime.coordinator.async_persist_runtime()
    except Exception:
        _LOGGER.exception("Power Orchestrator runtime persistence failed during unload")
    removed = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if removed is False:
        return False
    remove_listener = getattr(runtime, "repair_listener_remove", None)
    if callable(remove_listener):
        remove_listener()
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    entry.runtime_data = None
    if not _loaded_runtimes(hass):
        _unregister_services(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Drop obsolete configuration fields while preserving load-shedding settings."""
    changed = False
    data = dict(entry.data or {})
    allowed_data_keys = {
        CONF_AVERAGING_PERIOD,
        CONF_BATTERY_SOC,
        CONF_BATTERY_THRESHOLD,
        CONF_DEVICES,
        CONF_EXECUTION_MODE,
        CONF_GRID_LOSS_MODE,
        CONF_GRID_LOSS_SENSOR,
        CONF_HYSTERESIS,
        CONF_LOAD_SENSOR,
        CONF_MAX_LOAD,
        CONF_PAUSE_PERIOD,
        CONF_SAFETY_RESERVE,
        CONF_THRESHOLDS,
    }
    clean_data = {key: value for key, value in data.items() if key in allowed_data_keys}
    if clean_data != data:
        data = clean_data
        changed = True
    allowed_device_keys = {
        CONF_DEVICE_ID,
        CONF_DEVICE_NAME,
        CONF_DEVICE_ENTITY,
        CONF_DEVICE_EXPECTED_POWER,
        CONF_DEVICE_POWER_SENSOR,
        CONF_DEVICE_ACTUATORS,
        CONF_PRIORITY,
        CONF_SHED_PRIORITY,
    }
    raw_devices = data.get(CONF_DEVICES)
    if isinstance(raw_devices, list):
        clean_devices = [
            {key: value for key, value in device.items() if key in allowed_device_keys}
            for device in raw_devices
            if isinstance(device, dict)
        ]
        if clean_devices != raw_devices:
            data[CONF_DEVICES] = clean_devices
            changed = True
    if changed:
        updater = getattr(hass.config_entries, "async_update_entry", None)
        if callable(updater):
            updater(entry, data=data)
    return True


def _service_runtime(hass: HomeAssistant) -> PowerOrchestratorRuntimeData:
    runtimes = _loaded_runtimes(hass)
    if len(runtimes) != 1:
        raise _translated_error(HomeAssistantError, "entry_not_unique")
    return runtimes[0]


def _service_source(call: Any) -> tuple[str, str | None, str | None]:
    context = getattr(call, "context", None)
    source = getattr(call, "data", {}).get("source", "service")
    if not isinstance(source, str) or not source.strip():
        raise _translated_error(ServiceValidationError, "invalid_service_source")
    return source.strip(), getattr(context, "user_id", None), getattr(context, "id", None)


async def _register_services(hass: HomeAssistant) -> None:
    """Register singleton services once."""
    services = hass.services
    if getattr(services, "has_service", lambda *_: False)(DOMAIN, "force_evaluate"):
        return

    async def force_evaluate(call: Any) -> None:
        del call
        try:
            runtime = _service_runtime(hass)
            await runtime.coordinator.async_force_evaluate()
            _sync_repair_issues_for_runtime(hass, runtime)
        except Exception as exc:
            raise _translated_error(HomeAssistantError, "evaluation_failed", reason=str(exc)) from exc

    async def set_mode(call: Any) -> None:
        mode = getattr(call, "data", {}).get("mode")
        if mode not in (MODE_AUTO, MODE_OFF):
            raise _translated_error(ServiceValidationError, "invalid_service_mode")
        try:
            runtime = _service_runtime(hass)
            await runtime.coordinator.async_set_mode(mode)
            _sync_repair_issues_for_runtime(hass, runtime)
        except Exception as exc:
            raise _translated_error(HomeAssistantError, "mode_change_failed", reason=str(exc)) from exc

    async def request_stop(call: Any) -> None:
        data = getattr(call, "data", {})
        device_id = data.get("device_id")
        if not isinstance(device_id, str) or not device_id.strip():
            raise _translated_error(ServiceValidationError, "missing_device_id")
        source, actor_id, context_id = _service_source(call)
        try:
            runtime = _service_runtime(hass)
            await runtime.coordinator.async_request_stop(
                device_id.strip(), source=source, actor_id=actor_id, context_id=context_id
            )
            _sync_repair_issues_for_runtime(hass, runtime)
        except Exception as exc:
            raise _translated_error(HomeAssistantError, "stop_request_failed", reason=str(exc)) from exc

    async def clear_quarantine(call: Any) -> None:
        data = getattr(call, "data", {})
        device_id = data.get("device_id")
        if not isinstance(device_id, str) or not device_id.strip():
            raise _translated_error(ServiceValidationError, "missing_device_id")
        source, actor_id, context_id = _service_source(call)
        try:
            runtime = _service_runtime(hass)
            await runtime.coordinator.async_clear_quarantine(
                device_id.strip(), source=source, actor_id=actor_id, context_id=context_id
            )
            _sync_repair_issues_for_runtime(hass, runtime)
        except Exception as exc:
            raise _translated_error(HomeAssistantError, "quarantine_clear_failed", reason=str(exc)) from exc

    async def set_execution_mode(call: Any) -> None:
        data = getattr(call, "data", {})
        value = data.get(CONF_EXECUTION_MODE)
        confirm = data.get("confirm_live", False)
        if value not in (EXECUTION_MODE_LIVE, EXECUTION_MODE_OBSERVE):
            raise _translated_error(ServiceValidationError, "invalid_execution_mode")
        try:
            await _service_runtime(hass).coordinator.async_set_execution_mode(value, confirm_live=bool(confirm))
        except Exception as exc:
            raise _translated_error(HomeAssistantError, "execution_mode_change_failed", reason=str(exc)) from exc

    service_schema = {
        "force_evaluate": vol.Schema({}),
        "set_mode": vol.Schema({vol.Required("mode"): vol.In([MODE_AUTO, MODE_OFF])}),
        "request_stop": vol.Schema({vol.Required("device_id"): str, vol.Optional("source", default="service"): str}),
        "clear_quarantine": vol.Schema({vol.Required("device_id"): str, vol.Optional("source", default="service"): str}),
        "set_execution_mode": vol.Schema({
            vol.Required(CONF_EXECUTION_MODE): vol.In([EXECUTION_MODE_LIVE, EXECUTION_MODE_OBSERVE]),
            vol.Optional("confirm_live", default=False): bool,
        }),
    }
    handlers = {
        "force_evaluate": force_evaluate,
        "set_mode": set_mode,
        "request_stop": request_stop,
        "clear_quarantine": clear_quarantine,
        "set_execution_mode": set_execution_mode,
    }
    for name in _REGISTERED_SERVICES:
        services.async_register(DOMAIN, name, handlers[name], schema=service_schema[name])


def _unregister_services(hass: HomeAssistant) -> None:
    services = getattr(hass, "services", None)
    remove = getattr(services, "async_remove", None)
    if callable(remove):
        for name in _REGISTERED_SERVICES:
            remove(DOMAIN, name)
