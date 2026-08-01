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

The **Options** flow exposes the same safety-relevant settings after installation. The **Reconfigure** flow is available from the config-entry menu and updates the entry without removing it. Changes to load sources, safety sources, devices, actuator groups, expected power, priorities, thresholds, or pause timing trigger a guarded reload. Existing persisted quarantine, action journal, and execution-mode state remain fail-closed and are not cleared by reconfiguration.

Every field has an inline UI description. In particular:

- `expected_power` is the admission reservation; it is not silently replaced by measured power.
- measured-power sensors are optional telemetry and may be explicitly cleared;
- `only_from_solar` uses the exact current Forecast.Solar forecast, not actual PV production;
- an actuator group is confirmed only when every actuator reports the expected state;
- unknown, unavailable, stale, wrong-unit, non-finite, negative, or future telemetry blocks normal starts.

## How It Works

### Turn-On Logic

A device is enabled only when **all** conditions are met:

1. `max(current_load, average_load) + expected_power + safety_reserve + hysteresis < max_load`
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

## Supported functionality

Power Orchestrator manages logical loads that already exist in Home Assistant. It does not create proxy switches or replace the underlying device integrations.

Supported control entities:

- `switch`, `light`, and `input_boolean` as the primary on/off actuator;
- optional `climate` plus switch/light/input_boolean actuator groups;
- one optional measured-power `sensor` per logical device;
- one whole-house load `sensor`;
- a grid-loss `binary_sensor` or a battery SoC `sensor` as the authoritative safety source;
- optional Forecast.Solar current-power forecast and PV/battery telemetry.

Supported actions are `force_evaluate`, `set_mode`, `request_start`, `request_stop`, `clear_quarantine`, and `set_execution_mode`. All actions use the integration's guarded coordinator boundary; dashboards and automations must not call raw relay or climate services for managed devices.

## Entities

All entities belong to one `Power Orchestrator` device and use stable unique IDs containing the config-entry ID.

| Entity | Type | Category | Description |
|---|---|---|---|
| `select.power_orchestrator_mode` | Select | configuration | Planner mode: `auto` / `off` |
| `sensor.power_orchestrator_status` | Sensor | diagnostic | Current planner/safety status |
| `sensor.power_orchestrator_current_load` | Sensor | measurement | Fresh instantaneous aggregate load (W) |
| `sensor.power_orchestrator_average_load` | Sensor | measurement | Average aggregate load over the configured window (W) |
| `sensor.power_orchestrator_available_capacity` | Sensor | measurement | Remaining guarded headroom (W) |
| `sensor.power_orchestrator_last_action` | Sensor | diagnostic | Last bounded action summary |
| `sensor.power_orchestrator_execution_mode` | Sensor | diagnostic | `observe` or `live`; physical boundary |
| `sensor.power_orchestrator_reason_code` | Sensor | diagnostic | Typed policy/safety reason |
| `sensor.power_orchestrator_last_operation` | Sensor | diagnostic | Last operation plus bounded journal projection |
| `binary_sensor.power_orchestrator_grid_ok` | Binary Sensor | diagnostic | Grid/safety source valid and safe |
| `binary_sensor.power_orchestrator_faulted` | Binary Sensor | diagnostic | At least one logical device has a durable fault |
| `binary_sensor.power_orchestrator_recovery_blocked` | Binary Sensor | diagnostic | Recovery is blocked pending reconciliation |
| `binary_sensor.power_orchestrator_action_journal_healthy` | Binary Sensor | diagnostic | Durable action journal can accept lifecycle writes |

The coordinator also exposes `load_sensor_valid`, `load_sensor_reason`, `faulted_devices`, `recovery_blocked_devices`, and bounded journal state for diagnostics. `safety_blocked` is used when a required input, persistence state, or physical readback cannot be trusted.

## Services

All service calls return a stable optional response containing `accepted`, `action`, `mode`, `execution_mode`, `reason_code`, and the last action where supported. A response with `accepted=true` in `observe` mode records an intent only; it is not evidence of a physical state change.

| Service | Required fields | Description |
|---|---|---|
| `power_orchestrator.force_evaluate` | none | Trigger an immediate guarded evaluation |
| `power_orchestrator.set_mode` | `mode` | Set planner mode to `auto` or `off`; this never bypasses emergency guards |
| `power_orchestrator.request_start` | `device_id`, optional `source` | Request a guarded logical-device start; observe mode records only |
| `power_orchestrator.request_stop` | `device_id`, optional `source` | Request a guarded logical-device stop; raw physical services remain prohibited |
| `power_orchestrator.clear_quarantine` | `device_id`, optional `source` | Clear a durable quarantine only after fresh OFF, load, measured-power, and persistence proof |
| `power_orchestrator.set_execution_mode` | `execution_mode`, optional `confirm_live` | Select `observe` or `live`; entering `live` requires explicit confirmation and fresh safety reconciliation |

The service schemas in `services.yaml` are UI metadata. Runtime handlers validate all fields again, map invalid input to translated `ServiceValidationError`, and map operation/storage failures to translated `HomeAssistantError`.

## Use cases

### Shadow migration from existing automations

1. Install and configure the integration with `planner_mode=off` and `execution_mode=observe`.
2. Keep existing automations enabled and compare the coordinator's intended actions with current automation behavior.
3. Review `status`, `reason_code`, `last_operation`, and the action journal for a complete telemetry cycle.
4. Do not enable `live` or remove the existing automation until separate approval and physical verification are complete.

### Solar-only water-heating load

Configure a boiler with `only_from_solar=true`, an expected power reservation, and a Forecast.Solar config entry. The integration uses the exact current forecast for admission; measured PV production is runtime telemetry only.

### Whole-house overload protection

Configure the aggregate load sensor, safety reserve, hysteresis, and one-to-ten overload thresholds. When a threshold matures, the coordinator sheds at most one logical load per evaluation barrier and retains durable recovery order.

### Recovery after a failed readback

A failed or ambiguous command leaves the device faulted/recovery-blocked. The issue registry, diagnostic entities, and persistent notification identify the device. `clear_quarantine` is accepted only after independent fresh OFF/load/measured-power evidence.

## Data updates

The coordinator performs periodic evaluation using its configured update interval and also reacts to relevant Home Assistant state changes. State-change bursts are coalesced into one worker. A new load report generation can authorize at most one normal start; the coordinator waits for causal post-command telemetry before releasing the barrier.

Telemetry freshness budgets are independent from the averaging period. Increasing the averaging window never makes an old grid, battery, actuator, forecast, or load sample safe. Invalid samples clear the relevant admission path and fail closed.

## Troubleshooting

### Status is `safety_blocked`

Check `sensor.power_orchestrator_reason_code`, `load_sensor_reason`, `grid_ok`, `faulted`, `recovery_blocked`, and the action journal health sensor. Typical causes are missing/unknown/stale input, unsupported units, invalid Forecast.Solar data, a persisted storage fault, or an unconfirmed actuator readback.

### A device is quarantined

Do not force the underlying switch or climate entity through a raw service. Verify every actuator in the logical group is actually OFF, verify a fresh aggregate load sample and measured-power sample, then call `clear_quarantine` with the stable logical `device_id`. If any proof is missing or stale, the request remains blocked by design.

### A service is rejected

Confirm the config entry is loaded, exactly one Power Orchestrator entry exists, the logical device ID is correct, and the requested mode is valid. Validation and operation errors are translated through Home Assistant resources.

### Diagnostics download

Use the config-entry diagnostics action from Home Assistant. The diagnostics payload is bounded and redacts credential-like keys. It is safe to attach to an issue after reviewing entity IDs and local deployment information.

## Known limitations

- This is a HACS/custom integration and has not received an official Home Assistant Core quality-scale review; the repository targets the Platinum checklist but must not self-award the official tier.
- Software load shedding is not a substitute for hardware overcurrent protection, breakers, or an electrician's safety assessment.
- Forecast.Solar is used only when its exact current-power entity and freshness/units are verified; unavailable forecast data blocks solar-only starts.
- Existing automations remain external owners. A manual/external start is not silently adopted as planner ownership.
- `observe` never sends physical Home Assistant service calls, including emergency, grid-loss, rollback, and hard-interlock paths.
- Actual PV production is not an admission signal because curtailment makes production load-dependent.
- A persisted quarantine intentionally requires an explicit, evidence-based clear; restarting Home Assistant does not clear it.
- The integration manages configured Home Assistant entities and does not discover the physical relay's device protocol itself.

## Removal

1. Set planner mode to `off` and keep execution mode at `observe`.
2. Wait for any in-flight evaluation to finish; do not use raw relay services as a substitute for guarded stop logic.
3. In Settings → Devices & Services, open **Power Orchestrator**, choose the config-entry menu, and select **Delete**.
4. If installed through HACS, remove the repository after deleting the config entry and restart Home Assistant.
5. For a manual installation, delete `custom_components/power_orchestrator/` only after the config entry is removed, then restart Home Assistant.
6. Existing automations are not modified by removal; review them separately before changing the household control plan.

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