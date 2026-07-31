# Power Orchestrator ⚡

Automatically manage optional electrical loads to keep total power consumption below a configured limit while maximizing solar usage.

## Features

- **Auto-discovery** from Home Assistant Energy Dashboard
- **Power-sensor auto-discovery** — preselects each device's Energy Dashboard `stat_rate`; any other `sensor` entity can replace it, or the field can be cleared to disable measured telemetry
- **Load shedding** — gradually turn off lowest-priority devices when consumption exceeds your limit
- **Solar-first** — only enable a device when the forecast covers its expected power
- **Whole-house singleton** — one config entry owns the shared capacity budget; a second entry is refused to prevent double starts
- **Grid loss handling** — in `live` mode, instant shutdown of optional devices, with persistent notifications for manual overrides; `observe` records the stop decision without any physical service call
- **Safety-first** — unknown/unavailable/stale data never authorizes a physical action
- **Battery threshold mode** — works without a grid loss sensor, using battery SoC instead
- **Pause periods** — prevents rapid on/off cycling after a device is turned off
- **Safety-first startup** — full re-evaluation on HA restart

## Prerequisites

- Home Assistant **2026.7.4+**
- **Load sensor** (required; selected in the Load Monitoring step; must report power in `W`)
- One safety source:
  - a **grid-loss binary sensor** (`on` = grid available), or
  - a **battery SoC sensor** with canonical `%` unit plus a configured threshold
- Energy Dashboard (optional; configure device consumption entries there for auto-discovery; custom devices work without it)
- Solar production sensor (optional telemetry; it is not used as the start-admission decision)
- Solar forecast integration (optional — Forecast.Solar; the exact `power_production_now` entity is resolved by config-entry identity, normalized from W/kW to W, and stale/invalid data fails closed)
- Battery power sensor (optional telemetry)

## Installation

### HACS (recommended)

1. Add this repository as a custom repository in HACS
2. Install "Power Orchestrator"
3. Restart Home Assistant
4. Go to Settings → Devices & Services → Add Integration → **Power Orchestrator**

### Manual

1. Copy `custom_components/power_orchestrator/` to your HA `config/custom_components/` directory
2. Restart Home Assistant
3. Go to Settings → Devices & Services → Add Integration → **Power Orchestrator**

## Configuration

The config flow walks you through 5 steps:

1. **Auto-Discovery** — detects sensors from your Energy Dashboard
2. **Load Monitoring** — set your load sensor, max load limit, averaging period, safety reserve, hysteresis, and optionally **1–10 custom overload threshold pairs** (`power limit` in W + `dwell time` in seconds). Power limits must be finite, strictly increasing, and within the hard interlock; durations must be finite and positive.
3. **Optional Devices** — confirm or remove devices discovered from the Energy Dashboard, choose an on/off control entity for each confirmed device, accept the auto-discovered `stat_rate` power sensor or replace/clear it, and optionally add custom devices
4. **Priority & Pause** — choose a named device for each priority position (position 1 is highest priority, the last position is shed first), then set pause period
5. **Grid Loss Behavior** — choose between a grid loss binary sensor or battery SoC threshold

## How It Works

### Turn-On Logic

A device is enabled only when **all** conditions are met:

1. `average_load + expected_power + safety_reserve < max_load`
2. Pause timer expired
3. Solar rule satisfied (if enabled): fresh Forecast.Solar **estimated current power** (`power_production_now`, normalized to W) is at least the device's expected power
4. The configured safety source is valid and reports a safe state
5. The device has a fresh, confirmed `off` state

Unknown, unavailable, stale, future, non-finite, negative, or wrong-unit safety/forecast/load input fails closed. Actual PV production is telemetry/runtime safety only; it does not authorize a start because curtailment makes production load-dependent.

The highest-priority device is enabled first, then the system immediately re-evaluates for additional devices.

### Load Shedding

When `average_load > max_load`, the lowest-priority currently-on device is turned off. Only one physical action is attempted per evaluation cycle. Load samples are timestamped and averaged over the configured time window; invalid samples clear the window and cannot authorize a start.

### Execution mode and grid loss

The planner mode (`auto`/`off`) is separate from the physical execution mode (`live`/`observe`). `observe` is the safe shadow mode: it evaluates policy and records intended actions, but **never sends a physical Home Assistant service call**, including grid-loss, hard-interlock, evaluator-error, rollback, or `emergency=True` paths. `live` enables the same guarded commands after explicit confirmation and fresh telemetry reconciliation. Existing automations can therefore remain enabled while the integration is evaluated in `observe`.

When grid is lost in `live` mode (or battery SoC falls below the configured threshold), optional devices are stopped with bounded readback. A failed or unconfirmed stop is reported as `safety_blocked`; it is never treated as success.

## Entities

| Entity | Type | Description |
|---|---|---|
| `select.power_orchestrator_mode` | Select | `auto` / `off` |
| `sensor.power_orchestrator_status` | Sensor | monitoring / load_shedding / grid_loss / adding_load / safety_blocked |
| `sensor.power_orchestrator_current_load` | Sensor | Instantaneous load (W) |
| `sensor.power_orchestrator_average_load` | Sensor | Average load over period (W) |
| `sensor.power_orchestrator_available_capacity` | Sensor | Remaining headroom (W) |
| `sensor.power_orchestrator_last_action` | Sensor | Human-readable last action |
| `binary_sensor.power_orchestrator_grid_ok` | Binary Sensor | Grid present / absent; false for missing or stale safety input |

The coordinator also exposes `load_sensor_valid` and `load_sensor_reason` in its data payload for diagnostics. `safety_blocked` is used when a required input or physical readback cannot be trusted.

## Services

| Service | Description |
|---|---|
| `power_orchestrator.force_evaluate` | Trigger immediate re-evaluation |
| `power_orchestrator.set_mode` | Set mode: `auto` or `off` |

## Design Principles

- **Auto-discover everything possible**, allow manual override
- **Control original entities directly** — no proxy switches
- **Expected power for admission** — measured power is exposed as telemetry; admission uses the configured expected-power reservation, while invalid/stale load data blocks starts
- **Safe after restart** — a new entry starts in `off`; explicitly persisted `auto`/`off` mode and bounded pause state survive HA restarts; corrupt persisted state fails closed
- **One device at a time** — gradual load shedding and starts, no sudden batch changes
- **Fail closed** — unknown/stale telemetry never authorizes a risky start
- **No live side effects in local tests** — all service calls are mocked

## Controlled HA Verification

Before enabling this integration on a live Home Assistant instance:

1. Install the package but keep the mode `off`; do not remove existing automations yet.
2. Complete the config flow and verify the discovered/custom device list, friendly names, control entities, priorities, load source, and safety source.
3. Confirm entity unique IDs and device grouping under one Power Orchestrator device.
4. Verify that missing, `unknown`, `unavailable`, stale, and invalid load/grid/battery/forecast states produce `safety_blocked` or grid-loss behavior and never a start.
5. In a controlled window, use mocked/test devices or a non-critical load to verify one-device-per-cycle start and shedding, pause persistence, `auto/off`, emergency stop, and manual-override notification.
6. Only after those checks are recorded should deployment, reload/restart, or physical service calls be authorized separately.

Local CI validates Python compilation, JSON resources, and the complete mocked test suite; it does not replace live HA verification. The full controlled procedure is in [`HA_VERIFICATION.md`](HA_VERIFICATION.md).