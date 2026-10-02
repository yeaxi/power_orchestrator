"""Power Orchestrator integration entry point."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
from collections.abc import Mapping
from functools import partial
from typing import Any, cast

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store

from .const import (
    CONF_AVERAGING_PERIOD,
    CONF_BATTERY_SOC,
    CONF_BATTERY_THRESHOLD,
    CONF_DEVICE_ACTUATORS,
    CONF_DEVICE_BATTERY_MIN_SOC,
    CONF_DEVICE_COMMAND_ENTITY,
    CONF_DEVICE_EMERGENCY_OFF_ENTITIES,
    CONF_DEVICE_ENTITY,
    CONF_DEVICE_EXPECTED_POWER,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_DEVICE_POWER_SENSOR,
    CONF_DEVICE_READBACK_ENTITIES,
    CONF_DEVICES,
    CONF_GRID_LOSS_MODE,
    CONF_GRID_LOSS_SENSOR,
    CONF_LOAD_SENSOR,
    CONF_PAUSE_PERIOD,
    CONF_PRIORITY,
    CONF_RECONFIGURATION_REQUIRED,
    CONF_SHED_PRIORITY,
    CONF_THRESHOLDS,
    DEFAULT_AVERAGING_PERIOD,
    DEFAULT_PAUSE_PERIOD,
    DOMAIN,
    GRID_LOSS_MODE_SENSOR,
    MAX_RUNTIME_PAUSE_SECONDS,
    MODE_OBSERVE,
    MODE_OFF,
    MODES,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .coordinator import CoordinatorConfig, PowerOrchestratorCoordinator
from .policy import PolicyConfig, derive_thresholds_from_mapping, strip_legacy_policy_fields
from .power_model import ManagedDevice, PowerModel, parse_battery_min_soc
from .report_events import async_track_reports
from .runtime import PowerOrchestratorRuntimeData
from .storage import RuntimeStore

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SELECT]
# Configuration is a UI flow only; async_setup discards YAML, so reject it loudly
# instead of ignoring it.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
_REGISTERED_SERVICES = (
    "force_evaluate",
    "set_mode",
    "request_stop",
    "set_request",
    "set_restore_intent",
    "cancel_restore",
    "clear_fault",
    "clear_quarantine",
)
_RECONFIGURATION_ISSUE_ID = "reconfiguration_required"
_REPAIR_ISSUE_IDS_KEY = f"{DOMAIN}_repair_issue_ids"
_ALLOWED_CONTROL_DOMAINS = frozenset({"switch", "light", "input_boolean", "climate", "humidifier"})
_ALLOWED_ACTUATOR_DOMAINS = _ALLOWED_CONTROL_DOMAINS


def _translated_error(
    exception_type: type[Exception],
    translation_key: str,
    *,
    reason: str | None = None,
) -> Exception:
    """Construct a translated Home Assistant exception."""
    placeholders = {"reason": reason} if reason else None
    return cast(Any, exception_type)(
        translation_domain=DOMAIN,
        translation_key=translation_key,
        translation_placeholders=placeholders,
    )


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
    faults = getattr(coordinator, "_faults", None)
    if faults is None:
        return set()
    return set(getattr(faults, "faulted", set())) | set(getattr(faults, "quarantined", set()))


def _repair_previous_by_entry(hass: HomeAssistant) -> dict[str, Any]:
    """Keep the existing in-memory issue bookkeeping shape usable."""
    hass_data = getattr(hass, "data", None)
    if not isinstance(hass_data, dict):
        hass_data = {}
        setattr(hass, "data", hass_data)
    previous_by_entry = hass_data.setdefault(_REPAIR_ISSUE_IDS_KEY, {})
    if not isinstance(previous_by_entry, dict):
        previous_by_entry = {}
        hass_data[_REPAIR_ISSUE_IDS_KEY] = previous_by_entry
    return previous_by_entry


def _registered_quarantine_ids(issues: Any) -> set[str]:
    """Select only this integration's persistent quarantine issue keys."""
    if not isinstance(issues, Mapping):
        return set()
    result: set[str] = set()
    for key in issues:
        if (
            isinstance(key, tuple)
            and len(key) == 2
            and key[0] == DOMAIN
            and isinstance(key[1], str)
            and key[1].startswith("quarantine_")
        ):
            result.add(key[1])
    return result


def _sync_repair_issues(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Mirror durable quarantine into persistent Home Assistant issues."""
    from homeassistant.helpers.issue_registry import (
        IssueSeverity,
        async_create_issue,
        async_delete_issue,
        async_get,
    )

    active_ids = _repair_device_ids(hass, entry)
    desired_ids = {_repair_issue_id(entry.entry_id, device_id) for device_id in active_ids}
    previous_by_entry = _repair_previous_by_entry(hass)
    previous_ids = previous_by_entry.get(entry.entry_id, set())
    if not isinstance(previous_ids, set):
        previous_ids = set(previous_ids) if isinstance(previous_ids, (list, tuple)) else set()
    try:
        registry = async_get(hass)
        issues = getattr(registry, "issues", {})
        registered_ids = _registered_quarantine_ids(issues)
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
    _sync_reconfiguration_issue(hass, entry)


def _sync_reconfiguration_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Expose a repair issue when migration could not derive a user threshold list."""
    from homeassistant.helpers.issue_registry import (
        IssueSeverity,
        async_create_issue,
        async_delete_issue,
    )

    runtime = getattr(entry, "runtime_data", None)
    coordinator = getattr(runtime, "coordinator", None)
    required = bool(getattr(coordinator, "_reconfiguration_required", False))
    try:
        if required:
            async_create_issue(
                hass,
                DOMAIN,
                _RECONFIGURATION_ISSUE_ID,
                is_fixable=False,
                is_persistent=True,
                issue_domain=DOMAIN,
                severity=IssueSeverity.ERROR,
                translation_key="reconfiguration_required",
            )
        else:
            async_delete_issue(hass, DOMAIN, _RECONFIGURATION_ISSUE_ID)
    except Exception:  # pragma: no cover - issue registry is non-safety-critical
        _LOGGER.debug("Unable to synchronize reconfiguration issue", exc_info=True)


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
    except TypeError, ValueError:
        return default
    if not math.isfinite(converted) or not minimum <= converted <= maximum:
        return default
    return converted


def _normalize_entity_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        valid = _valid_entity_id(item, _ALLOWED_ACTUATOR_DOMAINS)
        if valid and valid not in result:
            result.append(valid)
    return result


def _runtime_device_identity(raw: Mapping[str, Any]) -> tuple[str, str] | None:
    device_id = raw.get(CONF_DEVICE_ID)
    entity_id = _valid_entity_id(raw.get(CONF_DEVICE_ENTITY), _ALLOWED_CONTROL_DOMAINS)
    if not isinstance(device_id, str) or not device_id.strip() or entity_id is None:
        return None
    return device_id.strip(), entity_id


def _runtime_device_actuators(
    raw: Mapping[str, Any], entity_id: str, seen_entities: set[str]
) -> list[str]:
    result: list[str] = []
    for actuator in _normalize_entity_list(raw.get(CONF_DEVICE_ACTUATORS)):
        if actuator not in {entity_id, *result, *seen_entities}:
            result.append(actuator)
    return result


def _runtime_device_io(
    raw: Mapping[str, Any], entity_id: str, actuators: list[str]
) -> tuple[str, list[str], list[str]]:
    command = _valid_entity_id(raw.get(CONF_DEVICE_COMMAND_ENTITY), _ALLOWED_ACTUATOR_DOMAINS)
    command = command or next(
        (entity for entity in actuators if entity.startswith("climate.")), entity_id
    )
    readbacks = _normalize_entity_list(raw.get(CONF_DEVICE_READBACK_ENTITIES))
    readbacks = readbacks or [entity_id if command.startswith("climate.") else command]
    emergency = _normalize_entity_list(raw.get(CONF_DEVICE_EMERGENCY_OFF_ENTITIES))
    emergency = emergency or ([entity_id] if command != entity_id else [])
    return command, readbacks, emergency


def _runtime_device_record(
    raw: Mapping[str, Any], index: int, seen_entities: set[str]
) -> dict[str, Any] | None:
    identity = _runtime_device_identity(raw)
    if identity is None:
        return None
    device_id, entity_id = identity
    actuators = _runtime_device_actuators(raw, entity_id, seen_entities)
    command, readbacks, emergency = _runtime_device_io(raw, entity_id, actuators)
    priority = int(
        _safe_number(
            raw.get(CONF_PRIORITY, index + 1), default=index + 1, minimum=1, maximum=100000
        )
    )
    name = raw.get(CONF_DEVICE_NAME)
    battery_min_soc = parse_battery_min_soc(raw.get(CONF_DEVICE_BATTERY_MIN_SOC))
    return {
        CONF_DEVICE_ID: device_id,
        CONF_DEVICE_NAME: name.strip() if isinstance(name, str) and name.strip() else entity_id,
        CONF_DEVICE_ENTITY: entity_id,
        CONF_DEVICE_EXPECTED_POWER: int(
            math.ceil(
                _safe_number(
                    raw.get(CONF_DEVICE_EXPECTED_POWER), default=1, minimum=1, maximum=50000
                )
            )
        ),
        CONF_DEVICE_POWER_SENSOR: _valid_entity_id(
            raw.get(CONF_DEVICE_POWER_SENSOR), frozenset({"sensor"})
        ),
        CONF_PRIORITY: priority,
        CONF_SHED_PRIORITY: int(
            _safe_number(
                raw.get(CONF_SHED_PRIORITY, priority), default=priority, minimum=1, maximum=100000
            )
        ),
        CONF_DEVICE_ACTUATORS: actuators,
        CONF_DEVICE_COMMAND_ENTITY: command,
        CONF_DEVICE_READBACK_ENTITIES: readbacks,
        CONF_DEVICE_EMERGENCY_OFF_ENTITIES: emergency,
        **({CONF_DEVICE_BATTERY_MIN_SOC: battery_min_soc} if battery_min_soc is not None else {}),
    }


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
        device = _runtime_device_record(raw, index, seen_entities)
        if device is None:
            continue
        device_id = device[CONF_DEVICE_ID]
        entity_id = device[CONF_DEVICE_ENTITY]
        if device_id in seen_ids or entity_id in seen_entities:
            continue
        normalized.append(device)
        seen_ids.add(device_id)
        seen_entities.update((entity_id, *device[CONF_DEVICE_ACTUATORS]))
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


def _idempotent_remover(remove: Any) -> Any:
    """Wrap a Home Assistant unsubscribe callback so repeated cleanup is safe."""
    active = True

    def _remove() -> None:
        nonlocal active
        if not active:
            return
        active = False
        remove()

    return _remove


def _register_entry_update_listener(entry: Any) -> None:
    """Own the config-entry update listener through the entry unload lifecycle."""
    add_listener = getattr(entry, "add_update_listener", None)
    if not callable(add_listener):
        return
    remove_listener = add_listener(_async_update_listener)
    on_unload = getattr(entry, "async_on_unload", None)
    if callable(remove_listener) and callable(on_unload):
        on_unload(remove_listener)


async def _async_setup_entry_impl(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = dict(entry.data or {})
    data.update(dict(entry.options or {}))
    model = _build_model(data)
    store = RuntimeStore(Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}_{entry.entry_id}"))
    await store.async_load()
    policy = PolicyConfig.from_mapping(data)
    reconfiguration_required = policy is None
    policy = policy or _unconfigured_policy()
    coordinator = PowerOrchestratorCoordinator(
        hass=hass,
        model=model,
        store=store,
        config=CoordinatorConfig(
            load_sensor=str(data.get(CONF_LOAD_SENSOR, "")),
            averaging_period=_safe_number(
                data.get(CONF_AVERAGING_PERIOD),
                default=DEFAULT_AVERAGING_PERIOD,
                minimum=1,
                maximum=300,
            ),
            pause_period=_safe_number(
                data.get(CONF_PAUSE_PERIOD),
                default=DEFAULT_PAUSE_PERIOD,
                minimum=0,
                maximum=MAX_RUNTIME_PAUSE_SECONDS,
            ),
            grid_loss_mode=data.get(CONF_GRID_LOSS_MODE, GRID_LOSS_MODE_SENSOR),
            grid_loss_sensor=data.get(CONF_GRID_LOSS_SENSOR),
            battery_threshold=data.get(CONF_BATTERY_THRESHOLD),
            battery_soc_sensor=data.get(CONF_BATTERY_SOC),
            entry_id=entry.entry_id,
            policy=policy,
        ),
    )
    coordinator._reconfiguration_required = reconfiguration_required
    _restore_runtime_state(coordinator, store, model)
    await _initialize_mode(coordinator, store, data, reconfiguration_required)

    runtime = PowerOrchestratorRuntimeData(coordinator=coordinator, model=model, store=store)
    entry.runtime_data = runtime
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime
    await _register_services(hass)

    async def _stop(event: Event) -> None:
        del event
        await coordinator.async_shutdown()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop))

    tracked_entity_ids = _tracked_entity_ids(data, model)

    async def _refresh_from_report() -> None:
        await coordinator.async_force_evaluate()
        _sync_repair_issues(hass, entry)

    if tracked_entity_ids:
        remove_listener = _idempotent_remover(
            async_track_reports(
                hass,
                tracked_entity_ids,
                coordinator._load_sensor,
                coordinator.observe_entity_report,
                _refresh_from_report,
            )
        )
        runtime.repair_listener_remove = remove_listener
        entry.async_on_unload(remove_listener)

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
    _register_entry_update_listener(entry)
    return True


def _build_model(data: dict[str, Any]) -> PowerModel:
    model = PowerModel()
    for device_data in _normalize_devices(data.get(CONF_DEVICES, [])):
        model.add_device(ManagedDevice.from_dict(device_data))
    return model


def _unconfigured_policy() -> PolicyConfig:
    from .policy import ReasonCode, ThresholdTier

    return PolicyConfig(
        thresholds=(ThresholdTier("unconfigured", 1.0, 0.0, ReasonCode.CONFIGURATION_INVALID),)
    )


def _restore_runtime_state(
    coordinator: PowerOrchestratorCoordinator,
    store: RuntimeStore,
    model: PowerModel,
) -> None:
    coordinator._safety_storage_invalid = store.safety_storage_invalid
    store.restore_pause_timestamps(model, MAX_RUNTIME_PAUSE_SECONDS)
    faulted, quarantined = store.restore_device_runtime(model)
    coordinator.restore_device_runtime(
        faulted,
        quarantined,
        fault_reasons=store.restore_fault_reasons(model),
        storage_invalid=store.safety_storage_invalid,
    )
    from .const import NOTIFY_TELEMETRY_ID

    active, pending = store.restore_fault_notification_state(
        model, telemetry_notification_id=f"{NOTIFY_TELEMETRY_ID}_{coordinator._entry_id}"
    )
    coordinator.restore_fault_notification_state(active, pending)
    telemetry_latched, telemetry_reason = store.restore_telemetry_fault()
    coordinator.restore_telemetry_fault(
        telemetry_latched,
        telemetry_reason,
        emergency_handled=store.restore_telemetry_emergency_handled(),
    )
    coordinator._safety_storage_invalid = (
        coordinator._safety_storage_invalid or store.safety_storage_invalid
    )
    coordinator.restore_action_journal(store.unresolved_actions())
    store.restore_policy_runtime(coordinator._policy_engine, model)
    coordinator.restore_requests(store.restore_requests(model))
    coordinator.restore_restore_tickets(store.restore_restore_tickets(model))


def _restored_mode(
    store: RuntimeStore,
    data: dict[str, Any],
    reconfiguration_required: bool,
) -> str:
    if store.safety_storage_invalid:
        return MODE_OFF
    if reconfiguration_required:
        return MODE_OBSERVE
    mode = store.resolve_unified_mode(data.get("execution_mode"))
    return mode if mode in MODES else MODE_OBSERVE


async def _initialize_mode(
    coordinator: PowerOrchestratorCoordinator,
    store: RuntimeStore,
    data: dict[str, Any],
    reconfiguration_required: bool,
) -> None:
    try:
        coordinator.mode = _restored_mode(store, data, reconfiguration_required)
        coordinator._save_runtime_snapshot()
        await store.async_save()
    except Exception:
        coordinator._mode = MODE_OBSERVE
        store.set_mode(MODE_OBSERVE)
        _LOGGER.exception("Unified mode could not be persisted; defaulting to observe")


def _tracked_entity_ids(data: dict[str, Any], model: PowerModel) -> list[str]:
    tracked = {
        data.get(CONF_LOAD_SENSOR),
        data.get(CONF_GRID_LOSS_SENSOR),
        data.get(CONF_BATTERY_SOC),
    }
    for device in model.all_devices():
        tracked.add(device.command_entity)
        tracked.update(device.readback_entities)
        tracked.update(device.emergency_off_entities)
        tracked.add(device.power_sensor_id)
    return sorted(entity for entity in tracked if isinstance(entity, str) and entity)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload options through Home Assistant's lifecycle."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Persist and unload one entry."""
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return False
    try:
        shutdown = getattr(runtime.coordinator, "async_shutdown", None)
        if callable(shutdown):
            await shutdown()
        else:
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
    """Migrate to thresholds-only policy and strip deleted restore/load fields."""
    data = dict(entry.data or {})
    options = dict(getattr(entry, "options", {}) or {})
    original_data = dict(data)
    original_options = dict(options)
    tiers = derive_thresholds_from_mapping({**data, **options})
    reconfiguration_required = tiers is None
    data, options = _migrated_thresholds(data, options, tiers)
    allowed_keys = _migration_allowed_keys()
    data = _clean_migration_payload(data, allowed_keys, keep_reconfigure=True)
    options = _clean_migration_payload(options, allowed_keys, keep_reconfigure=False)
    if reconfiguration_required:
        data[CONF_RECONFIGURATION_REQUIRED] = True
    else:
        data.pop(CONF_RECONFIGURATION_REQUIRED, None)
    _clean_migration_devices(data)
    _clean_migration_devices(options)
    changed = data != original_data or options != original_options
    changed = changed or getattr(entry, "version", None) != 2
    changed = changed or getattr(entry, "minor_version", None) != 4
    updater = getattr(hass.config_entries, "async_update_entry", None)
    if changed and callable(updater):
        updater(entry, data=data, options=options, version=2, minor_version=4)
    return True


def _migration_allowed_keys() -> set[str]:
    return {
        CONF_AVERAGING_PERIOD,
        CONF_BATTERY_SOC,
        CONF_BATTERY_THRESHOLD,
        CONF_DEVICES,
        CONF_GRID_LOSS_MODE,
        CONF_GRID_LOSS_SENSOR,
        CONF_LOAD_SENSOR,
        CONF_PAUSE_PERIOD,
        CONF_THRESHOLDS,
        CONF_RECONFIGURATION_REQUIRED,
        "policy_version",
    }


def _migrated_thresholds(
    data: dict[str, Any],
    options: dict[str, Any],
    tiers: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if tiers is None:
        data[CONF_RECONFIGURATION_REQUIRED] = True
        return data, options
    data[CONF_THRESHOLDS] = [
        {"power_limit": tier.limit_w, "duration_s": tier.duration_s} for tier in tiers
    ]
    options.pop(CONF_THRESHOLDS, None)
    return data, options


def _clean_migration_payload(
    payload: dict[str, Any],
    allowed_keys: set[str],
    *,
    keep_reconfigure: bool,
) -> dict[str, Any]:
    cleaned = strip_legacy_policy_fields(
        {key: value for key, value in payload.items() if key in allowed_keys}
    )
    cleaned = {key: value for key, value in cleaned.items() if key in allowed_keys}
    if not keep_reconfigure:
        cleaned.pop(CONF_THRESHOLDS, None)
        cleaned.pop(CONF_RECONFIGURATION_REQUIRED, None)
    return cleaned


def _migration_allowed_device_keys() -> set[str]:
    return {
        CONF_DEVICE_ID,
        CONF_DEVICE_NAME,
        CONF_DEVICE_ENTITY,
        CONF_DEVICE_EXPECTED_POWER,
        CONF_DEVICE_POWER_SENSOR,
        CONF_DEVICE_ACTUATORS,
        CONF_DEVICE_COMMAND_ENTITY,
        CONF_DEVICE_READBACK_ENTITIES,
        CONF_DEVICE_EMERGENCY_OFF_ENTITIES,
        CONF_DEVICE_BATTERY_MIN_SOC,
        CONF_PRIORITY,
        CONF_SHED_PRIORITY,
    }


def _clean_migration_devices(payload: dict[str, Any]) -> None:
    raw_devices = payload.get(CONF_DEVICES)
    if not isinstance(raw_devices, list):
        return
    allowed = _migration_allowed_device_keys()
    payload[CONF_DEVICES] = [
        {key: value for key, value in device.items() if key in allowed}
        for device in raw_devices
        if isinstance(device, dict)
    ]


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


async def _service_force_evaluate(hass: HomeAssistant, call: Any) -> None:
    del call
    runtime = _service_runtime(hass)
    try:
        await runtime.coordinator.async_force_evaluate()
    except Exception as exc:
        raise _translated_error(HomeAssistantError, "evaluation_failed", reason=str(exc)) from exc
    _sync_repair_issues_for_runtime(hass, runtime)


async def _service_set_mode(hass: HomeAssistant, call: Any) -> None:
    mode = getattr(call, "data", {}).get("mode")
    if mode not in MODES:
        raise _translated_error(ServiceValidationError, "invalid_service_mode")
    runtime = _service_runtime(hass)
    try:
        await runtime.coordinator.async_set_mode(mode)
    except Exception as exc:
        raise _translated_error(HomeAssistantError, "mode_change_failed", reason=str(exc)) from exc
    _sync_repair_issues_for_runtime(hass, runtime)


def _service_device_id(call: Any) -> str:
    device_id = getattr(call, "data", {}).get("device_id")
    if not isinstance(device_id, str) or not device_id.strip():
        raise _translated_error(ServiceValidationError, "missing_device_id")
    return device_id.strip()


async def _service_request_stop(hass: HomeAssistant, call: Any) -> None:
    source, actor_id, context_id = _service_source(call)
    runtime = _service_runtime(hass)
    try:
        await runtime.coordinator.async_request_stop(
            _service_device_id(call), source=source, actor_id=actor_id, context_id=context_id
        )
    except Exception as exc:
        raise _translated_error(HomeAssistantError, "stop_request_failed", reason=str(exc)) from exc
    _sync_repair_issues_for_runtime(hass, runtime)


async def _service_set_request(hass: HomeAssistant, call: Any) -> None:
    data = call.data
    source, _, _ = _service_source(call)
    deadline = cv.datetime(data["expires_at"])
    if deadline.tzinfo is None:
        raise ServiceValidationError("expires_at must include a timezone")
    runtime = _service_runtime(hass)
    await runtime.coordinator.async_set_request(
        data["device_id"],
        source=source,
        active=data["active"],
        expires_at=deadline.timestamp(),
        permit_entity=data["permit_entity"],
    )


def _absolute_intent_deadline(value: Any) -> float:
    """Accept exact UTC Unix seconds or a timezone-aware ISO timestamp."""
    if isinstance(value, (int, float)):
        if isinstance(value, bool) or not math.isfinite(value):
            raise ServiceValidationError("expires_at must be a finite UTC timestamp")
        return float(value)
    absolute = cv.datetime(value)
    if absolute.tzinfo is None:
        raise ServiceValidationError("expires_at must include a timezone")
    return absolute.timestamp()


def _restore_intent_deadline(data: Mapping[str, Any]) -> float:
    """Absolute producer expiry may shorten, never extend, the bounded TTL."""
    deadline = time.time() + float(data.get("ttl", 86400))
    if "expires_at" not in data:
        return deadline
    return min(deadline, _absolute_intent_deadline(data["expires_at"]))


async def _service_set_restore_intent(hass: HomeAssistant, call: Any) -> None:
    data = call.data
    source, _, _ = _service_source(call)
    runtime = _service_runtime(hass)
    await runtime.coordinator.async_set_restore_intent(
        data["device_id"],
        source=source,
        active=data["active"],
        expires_at=_restore_intent_deadline(data),
        permit_entity=data.get("permit_entity"),
        request_entity=data.get("request_entity"),
        request_data=data.get("request_data"),
        expected_intent=data.get("expected_intent"),
    )


async def _service_cancel_restore(hass: HomeAssistant, call: Any) -> None:
    await _service_runtime(hass).coordinator.async_cancel_restore(_service_device_id(call))


async def _service_clear_fault(hass: HomeAssistant, call: Any) -> None:
    del call
    if not await _service_runtime(hass).coordinator.async_clear_fault():
        raise _translated_error(HomeAssistantError, "fault_clear_failed")


async def _service_clear_quarantine(hass: HomeAssistant, call: Any) -> None:
    source, actor_id, context_id = _service_source(call)
    runtime = _service_runtime(hass)
    try:
        await runtime.coordinator.async_clear_quarantine(
            _service_device_id(call), source=source, actor_id=actor_id, context_id=context_id
        )
    except Exception as exc:
        raise _translated_error(
            HomeAssistantError, "quarantine_clear_failed", reason=str(exc)
        ) from exc
    _sync_repair_issues_for_runtime(hass, runtime)


def _service_schemas() -> dict[str, vol.Schema]:
    return {
        "set_request": vol.Schema(
            {
                vol.Required("device_id"): str,
                vol.Required("source"): str,
                vol.Required("active"): cv.boolean,
                vol.Required("expires_at"): str,
                vol.Required("permit_entity"): cv.entity_id,
            }
        ),
        "set_restore_intent": vol.Schema(
            {
                vol.Required("device_id"): str,
                vol.Required("source"): str,
                vol.Required("active"): cv.boolean,
                vol.Optional("ttl", default=86400): vol.All(
                    vol.Coerce(float), vol.Range(min=1, max=86400)
                ),
                vol.Optional("permit_entity"): cv.entity_id,
                vol.Optional("request_entity"): cv.entity_id,
                vol.Optional("request_data"): dict,
                vol.Optional("expires_at"): vol.Any(str, int, float),
                vol.Optional("expected_intent"): dict,
            }
        ),
        "cancel_restore": vol.Schema({vol.Required("device_id"): str}),
        "clear_fault": vol.Schema({}),
        "force_evaluate": vol.Schema({}),
        "set_mode": vol.Schema({vol.Required("mode"): vol.In(sorted(MODES))}),
        "request_stop": vol.Schema(
            {vol.Required("device_id"): str, vol.Optional("source", default="service"): str}
        ),
        "clear_quarantine": vol.Schema(
            {vol.Required("device_id"): str, vol.Optional("source", default="service"): str}
        ),
    }


def _service_handlers(hass: HomeAssistant) -> dict[str, Any]:
    return {
        "set_request": partial(_service_set_request, hass),
        "set_restore_intent": partial(_service_set_restore_intent, hass),
        "cancel_restore": partial(_service_cancel_restore, hass),
        "clear_fault": partial(_service_clear_fault, hass),
        "force_evaluate": partial(_service_force_evaluate, hass),
        "set_mode": partial(_service_set_mode, hass),
        "request_stop": partial(_service_request_stop, hass),
        "clear_quarantine": partial(_service_clear_quarantine, hass),
    }


async def _register_services(hass: HomeAssistant) -> None:
    """Register singleton services once."""
    services = hass.services
    if getattr(services, "has_service", lambda *_: False)(DOMAIN, "force_evaluate"):
        return
    schemas = _service_schemas()
    handlers = _service_handlers(hass)
    for name in _REGISTERED_SERVICES:
        services.async_register(DOMAIN, name, handlers[name], schema=schemas[name])


def _unregister_services(hass: HomeAssistant) -> None:
    services = getattr(hass, "services", None)
    remove = getattr(services, "async_remove", None)
    if callable(remove):
        for name in _REGISTERED_SERVICES:
            remove(DOMAIN, name)
