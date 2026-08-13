"""Constants for the load-shedding-only Power Orchestrator."""

from typing import Final

DOMAIN: Final = "power_orchestrator"

# Config-flow keys
CONF_LOAD_SENSOR: Final = "load_sensor"
CONF_MAX_LOAD: Final = "max_load"  # legacy migration only
CONF_AVERAGING_PERIOD: Final = "averaging_period"
CONF_SAFETY_RESERVE: Final = "safety_reserve"  # legacy migration only
CONF_HYSTERESIS: Final = "hysteresis"  # legacy migration only
CONF_DEVICES: Final = "devices"
CONF_DEVICE_ID: Final = "device_id"
CONF_DEVICE_NAME: Final = "name"
CONF_DEVICE_ENTITY: Final = "entity"
CONF_DEVICE_EXPECTED_POWER: Final = "expected_power"
CONF_DEVICE_POWER_SENSOR: Final = "power_sensor"
CONF_DEVICE_ACTUATORS: Final = "actuators"
CONF_SHED_PRIORITY: Final = "shed_priority"
CONF_DISCOVERED_DEVICES: Final = "discovered_devices"
CONF_ADD_CUSTOM_DEVICE: Final = "add_custom_device"
CONF_ADD_ANOTHER: Final = "add_another"
CONF_PRIORITY: Final = "priority"  # legacy alias for shedding order
CONF_PAUSE_PERIOD: Final = "pause_period"
CONF_GRID_LOSS_MODE: Final = "grid_loss_mode"
CONF_GRID_LOSS_SENSOR: Final = "grid_loss_sensor"
CONF_BATTERY_THRESHOLD: Final = "battery_threshold"
CONF_BATTERY_SOC: Final = "battery_soc"
CONF_POLICY_VERSION: Final = "policy_version"
CONF_HARD_INTERLOCK: Final = "hard_interlock"  # legacy migration only
CONF_SHED_SUSTAINED_LIMIT: Final = "shed_sustained_limit"
CONF_SHED_SUSTAINED_DURATION: Final = "shed_sustained_duration"
CONF_SHED_FAST_LIMIT: Final = "shed_fast_limit"
CONF_SHED_FAST_DURATION: Final = "shed_fast_duration"
CONF_SHED_CRITICAL_LIMIT: Final = "shed_critical_limit"
CONF_SHED_CRITICAL_DURATION: Final = "shed_critical_duration"
CONF_THRESHOLDS: Final = "thresholds"
CONF_THRESHOLD_COUNT: Final = "threshold_count"
CONF_THRESHOLD_POWER: Final = "threshold_power"
CONF_THRESHOLD_DURATION: Final = "threshold_duration"
CONF_ADD_THRESHOLD: Final = "add_threshold"
CONF_PRIORITY_ORDER: Final = "priority_order"
CONF_RECONFIGURATION_REQUIRED: Final = "reconfiguration_required"
MAX_CUSTOM_THRESHOLDS: Final = 64

# Grid-loss safety modes
GRID_LOSS_MODE_SENSOR: Final = "grid_loss_sensor"
GRID_LOSS_MODE_THRESHOLD: Final = "battery_threshold"

# Single persisted controller mode. Off and Observe never call physical
# services; Observe still evaluates and records intended actions; Auto permits
# guarded physical behavior including automatic restore of pending loads.
MODE_AUTO: Final = "auto"
MODE_OFF: Final = "off"
MODE_OBSERVE: Final = "observe"
MODES: Final = frozenset({MODE_AUTO, MODE_OFF, MODE_OBSERVE})
DEFAULT_MODE: Final = MODE_OBSERVE

# Runtime status values
STATUS_MONITORING: Final = "monitoring"
STATUS_LOAD_SHEDDING: Final = "load_shedding"
STATUS_GRID_LOSS: Final = "grid_loss"
STATUS_SAFETY_BLOCKED: Final = "safety_blocked"
STATUS_OBSERVE: Final = "observe"
STATUS_FAULT: Final = "fault"
STATUS_LOAD_RESTORING: Final = "load_restoring"

# Defaults and safety bounds
DEFAULT_AVERAGING_PERIOD: Final = 10
QUARANTINE_CLEAR_MAX_POWER_W: Final = 1.0
DEFAULT_PAUSE_PERIOD: Final = 60
DEFAULT_POLICY_VERSION: Final = "load_shedding_v3"
RESTORE_SAFE_CAPACITY_DWELL_S: Final = 60.0

EVALUATION_INTERVAL: Final = 30
RELAY_READBACK_TIMEOUT_SECONDS: Final = 2.0
RELAY_READBACK_POLL_INTERVAL_SECONDS: Final = 0.1
MAX_RUNTIME_PAUSE_SECONDS: Final = 24 * 60 * 60

# Persistence
STORAGE_KEY: Final = "power_orchestrator_runtime"
STORAGE_VERSION: Final = 4
DEVICE_RUNTIME_SCHEMA_VERSION: Final = 2
MAX_HISTORY_DAYS: Final = 365

# Structured event types
EVENT_SCHEMA_VERSION: Final = 1
FAULT_NOTIFICATION_SCHEMA_VERSION: Final = 1
EVENT_ACTION: Final = f"{DOMAIN}.action"
EVENT_DECISION: Final = f"{DOMAIN}.decision"

# Persistent notification ids (deduped)
NOTIFY_TELEMETRY_ID: Final = f"{DOMAIN}_telemetry_blocked"
NOTIFY_MANUAL_ON_PREFIX: Final = f"{DOMAIN}_manual_on"
