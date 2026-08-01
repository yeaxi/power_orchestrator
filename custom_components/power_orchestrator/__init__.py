"""Power Orchestrator integration."""

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

try:
    from homeassistant.core import SupportsResponse
except ImportError:  # pragma: no cover - older/local HA test doubles
    class SupportsResponse:  # type: ignore[no-redef]
        """Compatibility value for service response registration."""

        OPTIONAL = "optional"

from homeassistant.helpers.storage import Store

try:
    from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
except ImportError:  # pragma: no cover - local test doubles do not ship exceptions
    class HomeAssistantError(Exception):  # type: ignore[no-redef]
        """Fallback exception for the local Home Assistant test doubles."""

        pass

    class ServiceValidationError(HomeAssistantError):  # type: ignore[no-redef, misc]
        """Fallback validation error for the local Home Assistant test doubles."""

        pass

from .const import (
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
    CONF_EXECUTION_MODE,
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
    DEFAULT_AVERAGING_PERIOD,
    DEFAULT_EXECUTION_MODE,
    DEFAULT_HYSTERESIS,
    DEFAULT_PAUSE_PERIOD,
    DEFAULT_SAFETY_RESERVE,
    DOMAIN,
    EXECUTION_MODE_LIVE,
    EXECUTION_MODE_OBSERVE,
    GRID_LOSS_MODE_SENSOR,
    GRID_LOSS_MODE_THRESHOLD,
    MAX_RUNTIME_PAUSE_SECONDS,
    MODE_AUTO,
    MODE_OFF,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .coordinator import PowerOrchestratorCoordinator
from .forecast import (
    resolve_current_power_forecast_entity as _resolve_forecast_entity_shared,
)
from .policy import PolicyConfig
from .power_model import ManagedDevice, PowerModel
from .runtime import PowerOrchestratorRuntimeData
from .storage import RuntimeStore

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SELECT]
_LIFECYCLE_KEY = f"{DOMAIN}_lifecycle"
_REPAIR_ISSUE_IDS_KEY = f"{DOMAIN}_repair_issue_ids"

_REGISTERED_SERVICES = (
    "force_evaluate",
    "set_mode",
    "request_start",
    "request_stop",
    "clear_quarantine",
    "set_execution_mode",
)

_ALLOWED_CONTROL_DOMAINS = frozenset({"switch", "light", "input_boolean"})
_ALLOWED_ACTUATOR_DOMAINS = frozenset({"switch", "light", "input_boolean", "climate"})


def _translated_error(
    exception_type: type[Exception],
    translation_key: str,
    *,
    reason: str | None = None,
) -> Exception:
    """Build a localized HA exception while supporting local test doubles."""
    placeholders = {"reason": reason} if reason else None
    factory = cast(Any, exception_type)
    try:
        created: Exception = factory(
            translation_domain=DOMAIN,
            translation_key=translation_key,
            translation_placeholders=placeholders,
        )
        return created
    except TypeError:
        return exception_type(translation_key)


def _repair_issue_id(device_id: str) -> str:
    """Return a collision-resistant, stable issue ID for a device."""
    digest = hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:20]
    return f"quarantine_{digest}"


def _repair_device_ids(data: Mapping[str, Any], key: str) -> set[str]:
    """Return valid persisted device IDs from one quarantine collection."""
    values = data.get(key, ())
    if not isinstance(values, (list, tuple, set, frozenset)):
        return set()
    return {
        device_id
        for device_id in values
        if isinstance(device_id, str) and device_id
    }


def _sync_repair_issues(
    hass: HomeAssistant,
    coordinator: PowerOrchestratorCoordinator,
    model: PowerModel,
) -> None:
    """Mirror durable quarantine state into the Home Assistant issue registry."""
    try:
        from homeassistant.helpers.issue_registry import (
            IssueSeverity,
            async_create_issue,
            async_delete_issue,
        )
    except ImportError:  # pragma: no cover - local Home Assistant test doubles
        return

    data = getattr(coordinator, "data", None) or {}
    if not isinstance(data, Mapping):
        data = {}
    active_ids = set().union(
        _repair_device_ids(data, "faulted_devices"),
        _repair_device_ids(data, "recovery_blocked_devices"),
    )
    # The persisted coordinator state is authoritative.  Keep an issue even
    # when a device mapping was removed: deleting it would hide an unresolved
    # physical action and would weaken the fail-closed boundary.
    desired_issue_ids = {_repair_issue_id(device_id) for device_id in active_ids}
    hass_data = getattr(hass, "data", None)
    if not isinstance(hass_data, dict):
        hass_data = {}
        setattr(hass, "data", hass_data)
    previous_issue_ids = set(hass_data.get(_REPAIR_ISSUE_IDS_KEY, ()))

    for device_id in sorted(active_ids):
        async_create_issue(
            hass,
            DOMAIN,
            _repair_issue_id(device_id),
            is_fixable=False,
            is_persistent=True,
            issue_domain=DOMAIN,
            learn_more_url="https://github.com/yeaxi/power_orchestrator#troubleshooting",
            severity=IssueSeverity.ERROR,
            translation_key="quarantine_requires_reconciliation",
            translation_placeholders={"device_id": device_id},
        )

    for issue_id in sorted(previous_issue_ids - desired_issue_ids):
        async_delete_issue(hass, DOMAIN, issue_id)
    hass_data[_REPAIR_ISSUE_IDS_KEY] = desired_issue_ids


def _lifecycle_state(hass: HomeAssistant) -> dict[str, Any]:
    """Return domain-scoped setup lock and in-flight reservations."""
    data = getattr(hass, "data", None)
    if not isinstance(data, dict):
        data = {}
        setattr(hass, "data", data)
    state = data.get(_LIFECYCLE_KEY)
    if not isinstance(state, dict):
        state = {}
        data[_LIFECYCLE_KEY] = state
    lock = state.get("lock")
    if not isinstance(lock, asyncio.Lock):
        lock = asyncio.Lock()
        state["lock"] = lock
    reservations = state.get("reservations")
    if not isinstance(reservations, set):
        reservations = set()
        state["reservations"] = reservations
    return state


def _loaded_runtimes(hass: HomeAssistant) -> list[PowerOrchestratorRuntimeData]:
    """Return runtime data for currently loaded entries."""
    entries_fn = getattr(getattr(hass, "config_entries", None), "async_entries", None)
    if not callable(entries_fn):
        return []
    entries = entries_fn(DOMAIN)
    if not isinstance(entries, (list, tuple, set)):
        return []
    runtimes = []
    for entry in entries:
        state = getattr(entry, "state", None)
        state_value = getattr(state, "value", state)
        if state is not None and state_value not in ("loaded", "LOADED"):
            continue
        runtime = getattr(entry, "runtime_data", None)
        if runtime is not None:
            runtimes.append(runtime)
    return runtimes


async def async_setup(
    hass: HomeAssistant,
    config: dict[str, Any] | None = None,
) -> bool:
    """Set up global services before any config entry is loaded."""
    await _register_services(hass)
    return True


def _valid_entity_id(value: object, domains: frozenset[str]) -> bool:
    """Return True only for a syntactically valid entity in an allowed domain."""
    if not isinstance(value, str):
        return False
    domain, separator, object_id = value.partition(".")
    return bool(separator and domain in domains and object_id)


def _safe_number(
    value: object,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    """Coerce a persisted numeric setting, failing closed on bad bounds."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number) or number < minimum or number > maximum:
        return default
    return number


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Serialize singleton setup and reserve the entry before any await."""
    lifecycle = _lifecycle_state(hass)
    lock = lifecycle["lock"]
    reservations: set[str] = lifecycle["reservations"]
    async with lock:
        if (
            _loaded_runtimes(hass)
            or getattr(entry, "runtime_data", None) is not None
            or reservations
        ):
            _LOGGER.error(
                "Power Orchestrator is a whole-house singleton; refusing a second config entry"
            )
            return False
        reservations.add(entry.entry_id)
    try:
        return await _async_setup_entry_impl(hass, entry)
    finally:
        async with lock:
            reservations.discard(entry.entry_id)


async def _async_setup_entry_impl(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Power Orchestrator from a config entry after arbitration."""
    if _loaded_runtimes(hass) or getattr(entry, "runtime_data", None) is not None:
        _LOGGER.error(
            "Power Orchestrator is a whole-house singleton; refusing a second config entry"
        )
        return False

    # Config-entry reloads can occur after global services were removed by the
    # last unload; restore the guarded service boundary idempotently.
    await _register_services(hass)

    raw_data = getattr(entry, "data", {}) or {}
    data = dict(raw_data) if isinstance(raw_data, Mapping) else {}
    options = getattr(entry, "options", {}) or {}
    if isinstance(options, Mapping):
        data.update(options)

    # ── Build power model from config ──────────────────────────────
    model = PowerModel()
    devices_config = data.get(CONF_DEVICES, [])
    if not isinstance(devices_config, (list, tuple)):
        devices_config = []
    seen_device_ids: set[str] = set()
    seen_control_entities: set[str] = set()
    for i, dev_cfg in enumerate(devices_config):
        if not isinstance(dev_cfg, Mapping):
            continue
        entity_id = dev_cfg.get(CONF_DEVICE_ENTITY)
        if not isinstance(entity_id, str) or not _valid_entity_id(
            entity_id, _ALLOWED_CONTROL_DOMAINS
        ):
            _LOGGER.warning("Skipping device with invalid control entity: %r", entity_id)
            continue
        raw_actuators = dev_cfg.get(CONF_DEVICE_ACTUATORS, ())
        if isinstance(raw_actuators, str):
            raw_actuators = (raw_actuators,)
        if not isinstance(raw_actuators, (list, tuple)):
            raw_actuators = ()
        actuator_ids: list[str] = []
        invalid_actuator = False
        for actuator in raw_actuators:
            if not _valid_entity_id(actuator, _ALLOWED_ACTUATOR_DOMAINS):
                invalid_actuator = True
                break
            if actuator == entity_id or actuator in actuator_ids:
                invalid_actuator = True
                break
            actuator_ids.append(actuator)
        if invalid_actuator:
            _LOGGER.error("Skipping device with invalid/duplicate logical actuator: %r", dev_cfg)
            continue
        all_control_entities = (entity_id, *actuator_ids)
        if any(control in seen_control_entities for control in all_control_entities):
            _LOGGER.error("Duplicate physical control entity in Power Orchestrator config: %s", all_control_entities)
            return False
        seen_control_entities.update(all_control_entities)
        try:
            expected_power = float(dev_cfg.get(CONF_DEVICE_EXPECTED_POWER, 0))
        except (TypeError, ValueError):
            _LOGGER.warning("Skipping device with invalid expected power: %r", dev_cfg)
            continue
        if not math.isfinite(expected_power) or not 1 <= expected_power <= 50000:
            _LOGGER.warning("Skipping device with invalid expected power: %r", dev_cfg)
            continue
        device_id = dev_cfg.get(CONF_DEVICE_ID)
        if not isinstance(device_id, str) or not device_id:
            device_id = f"dev_{i}"
        if device_id in seen_device_ids:
            _LOGGER.warning("Skipping duplicate device ID: %s", device_id)
            continue
        seen_device_ids.add(device_id)
        name = dev_cfg.get(CONF_DEVICE_NAME)
        if not isinstance(name, str) or not name.strip():
            name = entity_id
        power_sensor = dev_cfg.get(CONF_DEVICE_POWER_SENSOR)
        if not _valid_entity_id(power_sensor, frozenset({"sensor"})):
            power_sensor = None
        priority = int(
            _safe_number(
                dev_cfg.get(CONF_PRIORITY, i + 1),
                default=float(i + 1),
                minimum=1,
                maximum=100000,
            )
        )
        shed_priority = int(
            _safe_number(
                dev_cfg.get(CONF_SHED_PRIORITY, priority),
                default=float(priority),
                minimum=1,
                maximum=100000,
            )
        )
        restore_priority_raw = dev_cfg.get(CONF_RESTORE_PRIORITY)
        restore_priority = (
            int(_safe_number(restore_priority_raw, default=float(priority), minimum=1, maximum=100000))
            if restore_priority_raw is not None
            else None
        )
        hvac_mode_on = dev_cfg.get(CONF_DEVICE_HVAC_MODE_ON, "heat")
        if not isinstance(hvac_mode_on, str) or not hvac_mode_on:
            hvac_mode_on = "heat"
        model.add_device(
            ManagedDevice(
                device_id=device_id,
                name=name,
                entity_id=entity_id,
                expected_power=max(1, int(math.ceil(expected_power))),
                only_from_solar=dev_cfg.get(CONF_DEVICE_ONLY_SOLAR) is True,
                power_sensor_id=power_sensor,
                priority=priority,
                shed_priority=shed_priority,
                restore_priority=restore_priority,
                actuator_entity_ids=tuple(actuator_ids),
                hvac_mode_on=hvac_mode_on,
            )
        )

    load_sensor = data.get(CONF_LOAD_SENSOR)
    if not isinstance(load_sensor, str) or not _valid_entity_id(
        load_sensor, frozenset({"sensor"})
    ):
        _LOGGER.error("Invalid load sensor; normal load admission is disabled")
        load_sensor = ""
    max_load = _safe_number(
        data.get(CONF_MAX_LOAD, 5000),
        default=0,
        minimum=100,
        maximum=50000,
    )
    averaging_period = _safe_number(
        data.get(CONF_AVERAGING_PERIOD, DEFAULT_AVERAGING_PERIOD),
        default=DEFAULT_AVERAGING_PERIOD,
        minimum=1,
        maximum=300,
    )
    safety_reserve = _safe_number(
        data.get(CONF_SAFETY_RESERVE, DEFAULT_SAFETY_RESERVE),
        default=max_load,
        minimum=0,
        maximum=5000,
    )
    hysteresis = _safe_number(
        data.get(CONF_HYSTERESIS, DEFAULT_HYSTERESIS),
        default=max_load,
        minimum=0,
        maximum=5000,
    )
    pause_period = _safe_number(
        data.get(CONF_PAUSE_PERIOD, DEFAULT_PAUSE_PERIOD),
        default=DEFAULT_PAUSE_PERIOD,
        minimum=0,
        maximum=MAX_RUNTIME_PAUSE_SECONDS,
    )
    grid_loss_mode = data.get(CONF_GRID_LOSS_MODE, GRID_LOSS_MODE_SENSOR)
    if grid_loss_mode not in (GRID_LOSS_MODE_SENSOR, GRID_LOSS_MODE_THRESHOLD):
        _LOGGER.error("Invalid grid-loss mode; safety source is disabled")
        grid_loss_mode = GRID_LOSS_MODE_SENSOR
    grid_loss_sensor = data.get(CONF_GRID_LOSS_SENSOR)
    if not _valid_entity_id(grid_loss_sensor, frozenset({"binary_sensor"})):
        grid_loss_sensor = None
    battery_soc_sensor = data.get(CONF_BATTERY_SOC)
    if not _valid_entity_id(battery_soc_sensor, frozenset({"sensor"})):
        battery_soc_sensor = None
    battery_threshold = _safe_number(
        data.get(CONF_BATTERY_THRESHOLD, 20),
        default=100,
        minimum=0,
        maximum=100,
    )
    solar_production_entity = data.get("solar_power")
    if not _valid_entity_id(solar_production_entity, frozenset({"sensor"})):
        solar_production_entity = None

    policy_data = dict(data)
    policy_data["safety_reserve"] = safety_reserve
    policy = PolicyConfig.from_mapping(policy_data)
    configured_execution_mode = data.get(CONF_EXECUTION_MODE, DEFAULT_EXECUTION_MODE)
    if configured_execution_mode not in (EXECUTION_MODE_OBSERVE, EXECUTION_MODE_LIVE):
        configured_execution_mode = DEFAULT_EXECUTION_MODE

    # ── Runtime store ──────────────────────────────────────────────
    store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}")
    runtime_store = RuntimeStore(store)
    await runtime_store.async_load()
    persisted_execution_mode = runtime_store.restore_execution_mode()
    if persisted_execution_mode not in (EXECUTION_MODE_OBSERVE, EXECUTION_MODE_LIVE):
        persisted_execution_mode = None
    execution_mode = persisted_execution_mode or configured_execution_mode
    runtime_store.restore_pause_timestamps(
        model,
        max_pause_seconds=pause_period,
    )

    # ── Resolve forecast config entry → entity ──────────────────────
    forecast_entry_id = data.get("solar_forecast_entry")
    if not isinstance(forecast_entry_id, str) or not forecast_entry_id:
        forecast_entry_id = None
    if forecast_entry_id:
        # The config entry ID is authoritative: entity IDs may be renamed.
        solar_forecast_entity = _resolve_forecast_entity(hass, forecast_entry_id)
    else:
        # Legacy entity-only configuration cannot prove exact Forecast.Solar
        # identity, so keep solar-only admission fail-closed.
        solar_forecast_entity = None

    # ── Coordinator ────────────────────────────────────────────────
    coordinator = PowerOrchestratorCoordinator(
        hass=hass,
        model=model,
        store=runtime_store,
        load_sensor=load_sensor,
        max_load=max_load,
        averaging_period=averaging_period,
        safety_reserve=safety_reserve,
        hysteresis=hysteresis,
        pause_period=pause_period,
        grid_loss_mode=grid_loss_mode,
        grid_loss_sensor=grid_loss_sensor,
        battery_threshold=battery_threshold,
        battery_soc_sensor=battery_soc_sensor,
        solar_forecast_entity=solar_forecast_entity,
        solar_production_entity=solar_production_entity,
        entry_id=entry.entry_id,
        policy=policy,
        execution_mode=execution_mode,
    )

    restored_device_runtime = runtime_store.restore_device_runtime(model)
    if (
        isinstance(restored_device_runtime, (tuple, list))
        and len(restored_device_runtime) == 2
        and all(isinstance(value, (set, frozenset, list, tuple)) for value in restored_device_runtime)
    ):
        faulted_devices = set(restored_device_runtime[0])
        recovery_blocked_devices = set(restored_device_runtime[1])
    else:
        _LOGGER.error("Invalid persisted device runtime; restoring all devices quarantined")
        faulted_devices = set()
        recovery_blocked_devices = set()
    coordinator.restore_device_runtime(
        faulted_devices,
        recovery_blocked_devices,
        fault_reasons=runtime_store.restore_fault_reasons(model),
        storage_invalid=runtime_store.safety_storage_invalid,
    )
    restored_notification_state = runtime_store.restore_fault_notification_state(model)
    if (
        isinstance(restored_notification_state, (tuple, list))
        and len(restored_notification_state) == 2
        and all(isinstance(value, dict) for value in restored_notification_state)
    ):
        coordinator.restore_fault_notification_state(*restored_notification_state)
    else:
        coordinator.restore_fault_notification_state({}, {})
    unresolved_reader = getattr(runtime_store, "unresolved_actions", None)
    unresolved_actions = unresolved_reader() if callable(unresolved_reader) else []
    if not isinstance(unresolved_actions, (list, tuple)):
        unresolved_actions = []
    journal_invalid = getattr(runtime_store, "action_journal_invalid", False) is True
    coordinator.restore_action_journal(
        unresolved_actions,
        journal_invalid=journal_invalid,
    )
    runtime_store.restore_policy_runtime(coordinator._policy_engine, model)
    restored_mode = runtime_store.restore_mode()
    if restored_mode == MODE_AUTO:
        _LOGGER.warning(
            "Persisted auto mode found; keeping startup-safe off until explicit re-arm"
        )
    coordinator.mode = MODE_OFF  # type: ignore[misc]

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        shutdown = getattr(coordinator, "async_shutdown", None)
        if callable(shutdown):
            try:
                await shutdown()
            except Exception:
                _LOGGER.exception("Failed to shut down coordinator after first-refresh failure")
        if not _loaded_runtimes(hass):
            _unregister_services(hass)
        raise

    # ── Store references ────────────────────────────────────────────
    repair_listener_remove = None
    add_update_listener = getattr(coordinator, "async_add_listener", None)
    if callable(add_update_listener):
        repair_listener_remove = add_update_listener(
            lambda: _sync_repair_issues(hass, coordinator, model)
        )
    _sync_repair_issues(hass, coordinator, model)
    runtime_data = PowerOrchestratorRuntimeData(
        coordinator=coordinator,
        model=model,
        store=runtime_store,
        repair_listener_remove=(
            repair_listener_remove if callable(repair_listener_remove) else None
        ),
    )
    entry.runtime_data = runtime_data

    # ── Forward setup ──────────────────────────────────────────────
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        remove_repair_listener = getattr(
            runtime_data, "repair_listener_remove", None
        )
        if callable(remove_repair_listener):
            remove_repair_listener()
        entry.runtime_data = None
        shutdown = getattr(coordinator, "async_shutdown", None)
        if callable(shutdown):
            await shutdown()
        if not _loaded_runtimes(hass):
            _unregister_services(hass)
        raise

    # ── Subscribe to relevant state changes only ────────────────────
    safety_sensor = (
        grid_loss_sensor
        if grid_loss_mode == GRID_LOSS_MODE_SENSOR
        else battery_soc_sensor
    )
    watched_entities = {
        entity_id
        for entity_id in [load_sensor, safety_sensor]
        if entity_id
    }
    for device in model.all_devices():
        watched_entities.add(device.entity_id)
        if device.power_sensor_id:
            watched_entities.add(device.power_sensor_id)

    if watched_entities:
        state_worker_task: asyncio.Future[Any] | None = None
        state_dirty = False

        async def _run_state_worker() -> None:
            """Coalesce a burst of relevant state changes into evaluations."""
            nonlocal state_worker_task, state_dirty
            try:
                while True:
                    state_dirty = False
                    await coordinator.async_force_evaluate()
                    if not state_dirty:
                        break
            finally:
                state_worker_task = None

        def _schedule_state_worker() -> None:
            """Start one worker; later events only mark it dirty."""
            nonlocal state_worker_task
            if state_worker_task is not None and not state_worker_task.done():
                return
            create_task = getattr(hass, "async_create_task", None)
            if callable(create_task):
                coroutine = _run_state_worker()
                try:
                    task = create_task(coroutine, f"{DOMAIN}_state_worker")
                except Exception:
                    task = None
                if isinstance(task, asyncio.Future):
                    state_worker_task = task
                    return
                state_worker_task = asyncio.create_task(coroutine)
                return
            state_worker_task = asyncio.create_task(_run_state_worker())

        async def _state_listener(event: Any) -> None:
            """Mark relevant state changes and schedule one coalesced worker."""
            nonlocal state_dirty
            event_data = getattr(event, "data", {}) or {}
            if event_data.get("entity_id") not in watched_entities:
                return
            state_dirty = True
            _schedule_state_worker()

        def _cancel_state_worker() -> None:
            """Cancel the coalesced worker during entry unload."""
            if state_worker_task is not None and not state_worker_task.done():
                state_worker_task.cancel()

        entry.async_on_unload(
            hass.bus.async_listen("state_changed", _state_listener)
        )
        entry.async_on_unload(_cancel_state_worker)

    add_update_listener = getattr(entry, "add_update_listener", None)
    if callable(add_update_listener):
        entry.async_on_unload(add_update_listener(_async_update_listener))

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry after an options/config update."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and persist runtime state."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    runtime = getattr(entry, "runtime_data", None)
    if runtime is not None and unload_ok:
        store = getattr(runtime, "store", None)
        coordinator = getattr(runtime, "coordinator", None)
        remove_repair_listener = getattr(runtime, "repair_listener_remove", None)
        if callable(remove_repair_listener):
            remove_repair_listener()
        try:
            if store is not None and coordinator is not None:
                save_runtime_snapshot = getattr(coordinator, "_save_runtime_snapshot", None)
                if callable(save_runtime_snapshot):
                    save_runtime_snapshot()
                else:
                    save_policy_runtime = getattr(store, "save_policy_runtime", None)
                    engine = getattr(coordinator, "_policy_engine", None)
                    if callable(save_policy_runtime) and engine is not None:
                        save_policy_runtime(engine)
            if store is not None:
                await store.async_save()
        except Exception:
            # Persistence must not strand a live coordinator after the
            # platforms have already unloaded.
            _LOGGER.exception("Failed to persist runtime state during unload")
        finally:
            shutdown = getattr(coordinator, "async_shutdown", None)
            if callable(shutdown):
                try:
                    await shutdown()
                except Exception:
                    _LOGGER.exception("Failed to shut down coordinator during unload")
            entry.runtime_data = None

    if unload_ok and not _loaded_runtimes(hass):
        _unregister_services(hass)

    return bool(unload_ok)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entry if needed."""
    # Placeholder for future migrations
    return True


async def _register_services(hass: HomeAssistant) -> None:
    """Register domain services once during integration setup."""

    def require_single_runtime() -> PowerOrchestratorRuntimeData:
        runtimes = _loaded_runtimes(hass)
        if len(runtimes) != 1:
            raise _translated_error(
                HomeAssistantError,
                "entry_not_unique",
            )
        return runtimes[0]

    def service_result(runtime: PowerOrchestratorRuntimeData, **extra: Any) -> dict[str, Any]:
        """Return a stable, dashboard-safe result envelope for service calls."""
        coordinator = runtime.coordinator
        result: dict[str, Any] = {
            "mode": getattr(coordinator, "mode", None),
            "execution_mode": getattr(coordinator, "execution_mode", None),
            "reason_code": getattr(coordinator, "reason_code", None),
            "last_action": getattr(coordinator, "_last_action", None),
        }
        result.update(extra)
        return result

    def service_actor_context(call: Any) -> tuple[str, str, str | None]:
        """Normalize source and HA context for guarded intent audit records."""
        data = getattr(call, "data", {}) or {}
        source = data.get("source", "service")
        if not isinstance(source, str) or not source.strip():
            raise _translated_error(
                ServiceValidationError,
                "invalid_service_source",
            )
        context = getattr(call, "context", None)
        user_id = getattr(context, "user_id", None)
        context_id = getattr(context, "id", None)
        actor_id = user_id if isinstance(user_id, str) and user_id else "system"
        normalized_context_id = context_id if isinstance(context_id, str) and context_id else None
        return source.strip(), actor_id, normalized_context_id

    async def handle_force_evaluate(call: Any) -> dict[str, Any]:
        """Force immediate re-evaluation for the singleton entry."""
        runtime = require_single_runtime()
        try:
            await runtime.coordinator.async_force_evaluate()
        except Exception as exc:
            raise _translated_error(
                HomeAssistantError,
                "evaluation_failed",
            ) from exc
        return service_result(runtime, accepted=True, action="force_evaluate")

    async def handle_set_mode(call: Any) -> dict[str, Any]:
        """Set the orchestrator mode for the singleton entry."""
        data = getattr(call, "data", {}) or {}
        mode = data.get("mode")
        if mode not in (MODE_AUTO, MODE_OFF):
            raise _translated_error(
                ServiceValidationError,
                "invalid_service_mode",
            )
        runtime = require_single_runtime()
        try:
            await runtime.coordinator.async_set_mode(mode)
        except ValueError as exc:
            raise _translated_error(
                ServiceValidationError,
                "request_rejected",
                reason=str(exc),
            ) from exc
        except Exception as exc:
            raise _translated_error(
                HomeAssistantError,
                "mode_change_failed",
            ) from exc
        return service_result(runtime, accepted=True, action="set_mode", requested_mode=mode)

    async def handle_request_start(call: Any) -> dict[str, Any]:
        """Request a guarded logical-device start; never call relay services directly."""
        data = getattr(call, "data", {}) or {}
        device_id = data.get("device_id")
        if not isinstance(device_id, str) or not device_id:
            raise _translated_error(
                ServiceValidationError,
                "missing_device_id",
            )
        source, actor_id, context_id = service_actor_context(call)
        runtime = require_single_runtime()
        try:
            accepted = await runtime.coordinator.async_request_start(
                device_id,
                source=source,
                actor_id=actor_id,
                context_id=context_id,
            )
        except ValueError as exc:
            raise _translated_error(
                ServiceValidationError,
                "request_rejected",
                reason=str(exc),
            ) from exc
        except Exception as exc:
            raise _translated_error(
                HomeAssistantError,
                "start_request_failed",
            ) from exc
        return service_result(
            runtime,
            accepted=accepted,
            action="request_start",
            device_id=device_id,
            source=source,
            actor_id=actor_id,
            context_id=context_id,
        )

    async def handle_request_stop(call: Any) -> dict[str, Any]:
        """Request a guarded logical-device stop."""
        data = getattr(call, "data", {}) or {}
        device_id = data.get("device_id")
        if not isinstance(device_id, str) or not device_id:
            raise _translated_error(
                ServiceValidationError,
                "missing_device_id",
            )
        source, actor_id, context_id = service_actor_context(call)
        runtime = require_single_runtime()
        try:
            accepted = await runtime.coordinator.async_request_stop(
                device_id,
                source=source,
                actor_id=actor_id,
                context_id=context_id,
            )
        except ValueError as exc:
            raise _translated_error(
                ServiceValidationError,
                "request_rejected",
                reason=str(exc),
            ) from exc
        except Exception as exc:
            raise _translated_error(
                HomeAssistantError,
                "stop_request_failed",
            ) from exc
        return service_result(
            runtime,
            accepted=accepted,
            action="request_stop",
            device_id=device_id,
            source=source,
            actor_id=actor_id,
            context_id=context_id,
        )

    async def handle_clear_quarantine(call: Any) -> dict[str, Any]:
        """Clear a device quarantine only after coordinator safety proof."""
        data = getattr(call, "data", {}) or {}
        device_id = data.get("device_id")
        if not isinstance(device_id, str) or not device_id:
            raise _translated_error(
                ServiceValidationError,
                "missing_device_id",
            )
        source, actor_id, context_id = service_actor_context(call)
        runtime = require_single_runtime()
        try:
            accepted = await runtime.coordinator.async_clear_quarantine(
                device_id,
                source=source,
                actor_id=actor_id,
                context_id=context_id,
            )
        except ValueError as exc:
            raise _translated_error(
                ServiceValidationError,
                "request_rejected",
                reason=str(exc),
            ) from exc
        except Exception as exc:
            raise _translated_error(
                HomeAssistantError,
                "quarantine_clear_failed",
            ) from exc
        return service_result(
            runtime,
            accepted=accepted,
            action="clear_quarantine",
            device_id=device_id,
            source=source,
            actor_id=actor_id,
            context_id=context_id,
        )

    async def handle_set_execution_mode(call: Any) -> dict[str, Any]:
        """Change observe/live ownership; live requires explicit confirmation."""
        data = getattr(call, "data", {}) or {}
        execution_mode = data.get("execution_mode")
        confirm_live = data.get("confirm_live", False) is True
        if execution_mode not in ("observe", "live"):
            raise _translated_error(
                ServiceValidationError,
                "invalid_execution_mode",
            )
        runtime = require_single_runtime()
        try:
            await runtime.coordinator.async_set_execution_mode(
                execution_mode,
                confirm_live=confirm_live,
            )
        except ValueError as exc:
            raise _translated_error(
                ServiceValidationError,
                "request_rejected",
                reason=str(exc),
            ) from exc
        except Exception as exc:
            raise _translated_error(
                HomeAssistantError,
                "execution_mode_change_failed",
            ) from exc
        return service_result(
            runtime,
            accepted=True,
            action="set_execution_mode",
            requested_execution_mode=execution_mode,
        )

    has_service = getattr(hass.services, "has_service", None)
    if not callable(has_service) or has_service(DOMAIN, "force_evaluate") is not True:
        hass.services.async_register(
            DOMAIN,
            "force_evaluate",
            handle_force_evaluate,
            schema=vol.Schema({}),
            supports_response=SupportsResponse.OPTIONAL,
        )
    if not callable(has_service) or has_service(DOMAIN, "set_mode") is not True:
        hass.services.async_register(
            DOMAIN,
            "set_mode",
            handle_set_mode,
            schema=vol.Schema({vol.Required("mode"): vol.In((MODE_AUTO, MODE_OFF))}),
            supports_response=SupportsResponse.OPTIONAL,
        )
    if not callable(has_service) or has_service(DOMAIN, "request_start") is not True:
        hass.services.async_register(
            DOMAIN,
            "request_start",
            handle_request_start,
            schema=vol.Schema(
                {
                    vol.Required("device_id"): str,
                    vol.Optional("source", default="service"): str,
                }
            ),
            supports_response=SupportsResponse.OPTIONAL,
        )
    if not callable(has_service) or has_service(DOMAIN, "request_stop") is not True:
        hass.services.async_register(
            DOMAIN,
            "request_stop",
            handle_request_stop,
            schema=vol.Schema(
                {
                    vol.Required("device_id"): str,
                    vol.Optional("source", default="service"): str,
                }
            ),
            supports_response=SupportsResponse.OPTIONAL,
        )
    if not callable(has_service) or has_service(DOMAIN, "clear_quarantine") is not True:
        hass.services.async_register(
            DOMAIN,
            "clear_quarantine",
            handle_clear_quarantine,
            schema=vol.Schema(
                {
                    vol.Required("device_id"): str,
                    vol.Optional("source", default="service"): str,
                }
            ),
            supports_response=SupportsResponse.OPTIONAL,
        )
    if not callable(has_service) or has_service(DOMAIN, "set_execution_mode") is not True:
        hass.services.async_register(
            DOMAIN,
            "set_execution_mode",
            handle_set_execution_mode,
            schema=vol.Schema(
                {
                    vol.Required("execution_mode"): vol.In(("observe", "live")),
                    vol.Optional("confirm_live", default=False): bool,
                }
            ),
            supports_response=SupportsResponse.OPTIONAL,
        )


def _unregister_services(hass: HomeAssistant) -> None:
    """Remove guarded global services when no runtime can serve them."""
    has_service = getattr(hass.services, "has_service", None)
    remove_service = getattr(hass.services, "async_remove", None)
    if not callable(remove_service):
        return
    for service in _REGISTERED_SERVICES:
        if not callable(has_service) or has_service(DOMAIN, service) is True:
            remove_service(DOMAIN, service)


def _resolve_forecast_entity(hass: HomeAssistant, config_entry_id: str) -> str | None:
    """Resolve a config entry ID to an exact estimated-power forecast entity."""
    return _resolve_forecast_entity_shared(hass, config_entry_id)
