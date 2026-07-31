"""Constants for Power Orchestrator."""

from typing import Final

DOMAIN: Final = "power_orchestrator"

# Config flow keys
CONF_LOAD_SENSOR: Final = "load_sensor"
CONF_MAX_LOAD: Final = "max_load"  # legacy hard/load ceiling compatibility key
CONF_AVERAGING_PERIOD: Final = "averaging_period"
CONF_SAFETY_RESERVE: Final = "safety_reserve"
CONF_HYSTERESIS: Final = "hysteresis"
CONF_DEVICES: Final = "devices"
CONF_DEVICE_ID: Final = "device_id"
CONF_DEVICE_NAME: Final = "name"
CONF_DEVICE_ENTITY: Final = "entity"
CONF_DEVICE_EXPECTED_POWER: Final = "expected_power"
CONF_DEVICE_POWER_SENSOR: Final = "power_sensor"
CONF_DEVICE_ONLY_SOLAR: Final = "only_from_solar"
CONF_DEVICE_ACTUATORS: Final = "actuators"
CONF_DEVICE_HVAC_MODE_ON: Final = "hvac_mode_on"
CONF_SHED_PRIORITY: Final = "shed_priority"
CONF_RESTORE_PRIORITY: Final = "restore_priority"
CONF_DISCOVERED_DEVICES: Final = "discovered_devices"
CONF_ADD_CUSTOM_DEVICE: Final = "add_custom_device"
CONF_ADD_ANOTHER: Final = "add_another"
CONF_PRIORITY: Final = "priority"
CONF_PAUSE_PERIOD: Final = "pause_period"
CONF_GRID_LOSS_MODE: Final = "grid_loss_mode"
CONF_GRID_LOSS_SENSOR: Final = "grid_loss_sensor"
CONF_BATTERY_THRESHOLD: Final = "battery_threshold"
CONF_BATTERY_SOC: Final = "battery_soc"
CONF_POLICY_VERSION: Final = "policy_version"
CONF_EXECUTION_MODE: Final = "execution_mode"
CONF_RECOVERY_TARGET: Final = "recovery_target"
CONF_RECOVERY_START: Final = "recovery_start"
CONF_RECOVERY_LOW_DURATION: Final = "recovery_low_duration"
CONF_RECOVERY_STABILIZATION: Final = "recovery_stabilization"
CONF_HARD_INTERLOCK: Final = "hard_interlock"
CONF_SHED_SUSTAINED_LIMIT: Final = "shed_sustained_limit"
CONF_SHED_SUSTAINED_DURATION: Final = "shed_sustained_duration"
CONF_SHED_FAST_LIMIT: Final = "shed_fast_limit"
CONF_SHED_FAST_DURATION: Final = "shed_fast_duration"
CONF_SHED_CRITICAL_LIMIT: Final = "shed_critical_limit"
CONF_SHED_CRITICAL_DURATION: Final = "shed_critical_duration"
CONF_THRESHOLDS: Final = "thresholds"
CONF_THRESHOLD_COUNT: Final = "threshold_count"
MAX_CUSTOM_THRESHOLDS: Final = 10

# Grid loss modes
GRID_LOSS_MODE_SENSOR: Final = "grid_loss_sensor"
GRID_LOSS_MODE_THRESHOLD: Final = "battery_threshold"

# Operating modes
MODE_AUTO: Final = "auto"
MODE_OFF: Final = "off"

# Physical execution modes. New installs default to observe so legacy
# automations remain the only physical owner until an explicit cutover.
EXECUTION_MODE_OBSERVE: Final = "observe"
EXECUTION_MODE_LIVE: Final = "live"

# Status
STATUS_MONITORING: Final = "monitoring"
STATUS_LOAD_SHEDDING: Final = "load_shedding"
STATUS_GRID_LOSS: Final = "grid_loss"
STATUS_ADDING_LOAD: Final = "adding_load"
STATUS_SAFETY_BLOCKED: Final = "safety_blocked"
STATUS_OBSERVE: Final = "observe"
STATUS_RECOVERY_WAIT: Final = "recovery_wait"
STATUS_RECOVERY: Final = "recovery"
STATUS_FAULT: Final = "fault"

# Default values
DEFAULT_AVERAGING_PERIOD: Final = 10
DEFAULT_SAFETY_RESERVE: Final = 200
QUARANTINE_CLEAR_MAX_POWER_W: Final = 1.0
DEFAULT_HYSTERESIS: Final = 200
DEFAULT_PAUSE_PERIOD: Final = 60
DEFAULT_POLICY_VERSION: Final = "load_shedding_v1"
DEFAULT_EXECUTION_MODE: Final = EXECUTION_MODE_OBSERVE
DEFAULT_RECOVERY_TARGET: Final = 6000.0
DEFAULT_RECOVERY_START: Final = 5000.0
DEFAULT_RECOVERY_LOW_DURATION: Final = 60.0
DEFAULT_RECOVERY_STABILIZATION: Final = 60.0
DEFAULT_HARD_INTERLOCK: Final = 9000.0
DEFAULT_SHED_SUSTAINED_LIMIT: Final = 6500.0
DEFAULT_SHED_SUSTAINED_DURATION: Final = 300.0
DEFAULT_SHED_FAST_LIMIT: Final = 7000.0
DEFAULT_SHED_FAST_DURATION: Final = 30.0
DEFAULT_SHED_CRITICAL_LIMIT: Final = 8000.0
DEFAULT_SHED_CRITICAL_DURATION: Final = 5.0

# Evaluation interval
EVALUATION_INTERVAL: Final = 30

# Forecast freshness safety bound (current-hour forecast must be reported recently)
FORECAST_MAX_AGE_SECONDS: Final = 75 * 60

# Safety telemetry must be reported recently enough to authorize a start.
SAFETY_INPUT_MAX_AGE_SECONDS: Final = 3 * EVALUATION_INTERVAL
RELAY_READBACK_TIMEOUT_SECONDS: Final = 2.0
RELAY_READBACK_POLL_INTERVAL_SECONDS: Final = 0.1
MAX_RUNTIME_PAUSE_SECONDS: Final = 24 * 60 * 60
EXTERNAL_OWNERSHIP_GRACE_SECONDS: Final = 2 * 60 * 60

# Storage
STORAGE_KEY: Final = "power_orchestrator_runtime"
STORAGE_VERSION: Final = 2
DEVICE_RUNTIME_SCHEMA_VERSION: Final = 1

MAX_HISTORY_DAYS: Final = 365

# Structured event types
EVENT_SCHEMA_VERSION: Final = 1
FAULT_NOTIFICATION_SCHEMA_VERSION: Final = 1
EVENT_ACTION: Final = f"{DOMAIN}.action"
EVENT_DECISION: Final = f"{DOMAIN}.decision"
