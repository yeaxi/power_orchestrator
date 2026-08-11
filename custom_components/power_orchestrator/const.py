"""Constants for the load-shedding-only Power Orchestrator."""

from typing import Final

DOMAIN: Final = "power_orchestrator"

# Config-flow keys
CONF_LOAD_SENSOR: Final = "load_sensor"
CONF_MAX_LOAD: Final = "max_load"
CONF_AVERAGING_PERIOD: Final = "averaging_period"
CONF_SAFETY_RESERVE: Final = "safety_reserve"
CONF_HYSTERESIS: Final = "hysteresis"
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
CONF_EXECUTION_MODE: Final = "execution_mode"
CONF_HARD_INTERLOCK: Final = "hard_interlock"
CONF_SHED_SUSTAINED_LIMIT: Final = "shed_sustained_limit"
CONF_SHED_SUSTAINED_DURATION: Final = "shed_sustained_duration"
CONF_SHED_FAST_LIMIT: Final = "shed_fast_limit"
CONF_SHED_FAST_DURATION: Final = "shed_fast_duration"
CONF_SHED_CRITICAL_LIMIT: Final = "shed_critical_limit"
CONF_SHED_CRITICAL_DURATION: Final = "shed_critical_duration"
CONF_THRESHOLDS: Final = "thresholds"
# Legacy numbered-threshold inputs remain accepted for migration/automation
# compatibility; the config flow itself uses repeatable threshold steps.
CONF_THRESHOLD_COUNT: Final = "threshold_count"
CONF_THRESHOLD_POWER: Final = "threshold_power"
CONF_THRESHOLD_DURATION: Final = "threshold_duration"
CONF_ADD_THRESHOLD: Final = "add_threshold"
CONF_PRIORITY_ORDER: Final = "priority_order"
MAX_CUSTOM_THRESHOLDS: Final = 64

# Guarded restore (opt-in orchestration). Off by default; a load is only ever
# re-enabled if it was shed by the planner itself, its per-load opt-in is set,
# and restore is explicitly armed under live execution. No PV/forecast/
# generation admission is implied by any of these.
CONF_RESTORE_ENABLED: Final = "restore_enabled"
CONF_RESTORE_THRESHOLD: Final = "restore_threshold"
CONF_RESTORE_HYSTERESIS: Final = "restore_hysteresis"
CONF_RESTORE_DWELL: Final = "restore_dwell"
CONF_RESTORE_COOLDOWN: Final = "restore_cooldown"
# Per-device opt-in key stored inside each device mapping.
CONF_DEVICE_RESTORE_ENABLED: Final = "restore_enabled"
# Persisted runtime arm flag (never a config-flow field).
CONF_RESTORE_ARMED: Final = "restore_armed"

# Grid-loss safety modes
GRID_LOSS_MODE_SENSOR: Final = "grid_loss_sensor"
GRID_LOSS_MODE_THRESHOLD: Final = "battery_threshold"

# Controller modes
MODE_AUTO: Final = "auto"
MODE_OFF: Final = "off"

# Physical execution modes. New installs default to observe until an explicit
# operator-controlled live cutover.
EXECUTION_MODE_OBSERVE: Final = "observe"
EXECUTION_MODE_LIVE: Final = "live"

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
DEFAULT_SAFETY_RESERVE: Final = 200
QUARANTINE_CLEAR_MAX_POWER_W: Final = 1.0
DEFAULT_HYSTERESIS: Final = 200
DEFAULT_PAUSE_PERIOD: Final = 60
DEFAULT_POLICY_VERSION: Final = "load_shedding_v2"
DEFAULT_EXECUTION_MODE: Final = EXECUTION_MODE_OBSERVE
DEFAULT_HARD_INTERLOCK: Final = 9000.0
DEFAULT_SHED_SUSTAINED_LIMIT: Final = 6500.0
DEFAULT_SHED_SUSTAINED_DURATION: Final = 300.0
DEFAULT_SHED_FAST_LIMIT: Final = 7000.0
DEFAULT_SHED_FAST_DURATION: Final = 30.0
DEFAULT_SHED_CRITICAL_LIMIT: Final = 8000.0
DEFAULT_SHED_CRITICAL_DURATION: Final = 5.0

# Guarded-restore defaults (conservative; restore stays disabled unless the
# operator opts in globally and per-load and arms it under live execution).
DEFAULT_RESTORE_ENABLED: Final = False
DEFAULT_RESTORE_HYSTERESIS: Final = 200.0
DEFAULT_RESTORE_DWELL: Final = 300.0
DEFAULT_RESTORE_COOLDOWN: Final = 600.0

EVALUATION_INTERVAL: Final = 30
RELAY_READBACK_TIMEOUT_SECONDS: Final = 2.0
RELAY_READBACK_POLL_INTERVAL_SECONDS: Final = 0.1
MAX_RUNTIME_PAUSE_SECONDS: Final = 24 * 60 * 60
EXTERNAL_OWNERSHIP_GRACE_SECONDS: Final = 2 * 60 * 60

# Persistence
STORAGE_KEY: Final = "power_orchestrator_runtime"
STORAGE_VERSION: Final = 3
DEVICE_RUNTIME_SCHEMA_VERSION: Final = 2
MAX_HISTORY_DAYS: Final = 365

# Structured event types
EVENT_SCHEMA_VERSION: Final = 1
FAULT_NOTIFICATION_SCHEMA_VERSION: Final = 1
EVENT_ACTION: Final = f"{DOMAIN}.action"
EVENT_DECISION: Final = f"{DOMAIN}.decision"
