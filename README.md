# Power Orchestrator ⚡

`power_orchestrator` is a safety-first Home Assistant custom integration for **bounded load shedding** with an optional **guarded restore** of the loads it shed. It monitors an aggregate load and disconnects configured optional loads when the configured limits or safety conditions require it, and — only when explicitly enabled and armed — re-enables those same loads once it is safe to do so.

It does not perform PV prioritization, generation-based admission, or normal load enabling. Guarded restore is off by default, per-load opt-in, requires `live` execution with explicit arming, only ever re-enables loads the planner itself shed, and uses the same causal readback discipline as the stop path.

## Features

- Optional discovery from the Home Assistant Energy Dashboard.
- Aggregate load monitoring with `W`/`kW` normalization and semantic availability checks.
- Configurable overload thresholds with dwell times and a hard interlock.
- Deterministic shedding order with at most one normal physical stop per evaluation cycle.
- Grid-loss sensor mode or battery-SoC safety mode.
- Emergency all-stop path for unsafe grid/battery/input conditions.
- `auto`/`off` planner mode persisted through Home Assistant restarts.
- Optional guarded restore: fail-closed, opt-in, armed re-enable of planner-shed loads when headroom returns.
- `live`/`observe` execution mode; observe mode never sends physical service calls.
- Bounded physical readback and durable fault/quarantine state.
- Diagnostics, action journal, and redacted config-entry diagnostics.

## Prerequisites

- Home Assistant **2026.7.4+**.
- A required whole-house load sensor reporting `W` or `kW`.
- One safety source:
  - a grid-loss binary sensor (`on` means grid available), or
  - a battery SoC sensor with `%` and a configured threshold.
- One or more optional loads controlled by `switch`, `light`, or `input_boolean`.
- Optional measured-power sensor for each managed load.

Energy Dashboard discovery is advisory only. It supplies candidate load identities and optional telemetry; it never authorizes a physical action.

## Installation

### HACS

1. Add the repository as a custom repository in HACS.
2. Install **Power Orchestrator**.
3. Restart Home Assistant under the normal operator procedure.
4. Open **Settings → Devices & Services → Add Integration → Power Orchestrator**.

### Manual

1. Copy `custom_components/power_orchestrator/` into `config/custom_components/`.
2. Restart Home Assistant.
3. Add the integration from **Settings → Devices & Services**.

## Configuration

The config flow contains these steps:

1. **Auto-Discovery** — load and safety candidates from the Energy Dashboard, if available.
2. **Load Monitoring** — aggregate load sensor, maximum load, averaging period, reserve, hysteresis, and optional overload threshold pairs.
3. **Optional Devices** — choose existing controllable entities, expected power, optional measured-power sensors, and actuator groups.
4. **Priority & Pause** — define the deterministic shedding order and pause period.
5. **Grid Loss Behavior** — choose a grid sensor or battery-SoC threshold.

The Options and Reconfigure flows expose the same safety-relevant settings. Every field has an inline description explaining its meaning and runtime effect. Invalid, unknown, unavailable, non-finite, negative, or unsupported-unit input is rejected or fails closed.

## Runtime contract

### Load shedding

When a matured overload threshold is reached, the coordinator selects the first known-on, planner-owned load in the configured shedding order and issues a bounded stop request. A stop is not considered successful until the expected entity state is confirmed with a causal post-command report.

If readback is missing or contradictory, the load is marked unknown/faulted and remains safety-blocked. The integration never treats a failed stop as successful.

### Safety behavior

- Missing, unavailable, unknown, invalid, or wrong-unit aggregate load blocks ordinary evaluation.
- A grid-loss or invalid battery safety source triggers the emergency stop path.
- `off` blocks ordinary planner actions; emergency safety handling remains active.
- `observe` records intended actions but never calls a physical Home Assistant service.
- There is no normal automatic *enable* of never-shed loads. The only re-enable path is the guarded restore of planner-shed loads, which is off by default and requires explicit arming under `live` execution.

### Restart behavior

The planner's `auto`/`off` mode is stored using Home Assistant's persistent Store. On setup:

- a valid persisted mode is restored before the first evaluation;
- a missing, invalid, or corrupt value falls back to `off`;
- a restart does not implicitly arm a new entry;
- a persisted `auto` mode is not unconditionally overwritten with `off`.

Persisted device faults, quarantines, pauses, and action-journal state are independently validated and fail closed when malformed.

## Supported functionality

Power Orchestrator manages existing Home Assistant entities directly. It supports:

- `switch`, `light`, and `input_boolean` stop actuators;
- optional actuator groups;
- optional per-load measured-power sensors;
- one aggregate load sensor;
- one grid-loss binary sensor or battery SoC safety source;
- deterministic load-shedding thresholds and pause periods;
- optional guarded restore (opt-in, armed) of loads the planner shed.

It does **not** support PV priority, forecast-based decisions, generation-based admission, or normal automatic enabling of never-shed loads. Re-enabling is limited to the guarded restore of planner-shed loads described above.

## Entities

All entities use stable config-entry-scoped unique IDs.

| Entity role | Type | Description |
|---|---|---|
| Planner mode | Select | `auto` / `off` |
| Status | Sensor | Current safety and shedding status |
| Current load | Sensor | Fresh aggregate load in W |
| Average load | Sensor | Windowed aggregate load in W |
| Available capacity | Sensor | Guarded remaining headroom |
| Last action | Sensor | Last bounded action summary |
| Execution mode | Sensor | `observe` or `live` |
| Reason code | Sensor | Typed policy/safety reason |
| Last operation | Sensor | Bounded action-journal projection |
| Grid OK | Binary Sensor | Safety source is valid and safe |
| Faulted | Binary Sensor | At least one load has a durable fault |
| Action journal healthy | Binary Sensor | Durable journal can accept writes |

## Services

| Service | Fields | Effect |
|---|---|---|
| `power_orchestrator.force_evaluate` | none | Run one guarded evaluation |
| `power_orchestrator.set_mode` | `mode` | Persist `auto` or `off` |
| `power_orchestrator.request_stop` | `device_id`, optional `source` | Request one guarded load stop |
| `power_orchestrator.clear_quarantine` | `device_id`, optional `source` | Clear a fault only after independent evidence |
| `power_orchestrator.set_execution_mode` | `execution_mode`, optional `confirm_live` | Select `observe` or explicitly confirmed `live` |
| `power_orchestrator.authorize_restore` | `confirm_restore` | Arm guarded restore (no physical action; requires `live`+`auto`) |
| `power_orchestrator.request_restore` | `device_id`, `confirm_restore`, optional `source` | Request one guarded restore of a planner-shed load |

Runtime handlers validate every field independently of `services.yaml`. Raw relay, climate, or other physical services should not be called around the integration boundary.

## Use cases

### Whole-house overload protection

Configure the aggregate sensor, safety reserve, thresholds, and optional loads. When a threshold matures, one known-on load is shed per evaluation barrier.

### Shadow migration

Use `execution_mode=observe` while existing automations remain under separate ownership. Review reason codes, intended stop actions, and telemetry before any separately approved live activation.

### Failed readback

A missing or contradictory stop readback leaves the load unknown and faulted. The issue is surfaced through diagnostics and must be reconciled with verified valid evidence before clearing.

## Data updates

The coordinator evaluates periodically and coalesces relevant Home Assistant state changes. The source entities own their availability semantics; an averaging window cannot make an unavailable safety input valid.

The coordinator does not reserve capacity for or initiate new loads. Its physical action surface is stop-only.

## Troubleshooting

### `safety_blocked`

Inspect the status, reason code, load-sensor reason, Grid OK sensor, faulted sensor, and action-journal sensor. Typical causes include missing/unavailable input, unsupported units, invalid persistence, or failed readback.

### A load is faulted or quarantined

Do not bypass the integration with an unrelated physical service call. Verify the actual actuator state and valid measured load, then use the guarded reconciliation path. Missing proof intentionally keeps the load blocked.

### Mode did not persist

Verify that the config entry remained loaded and that the integration was allowed to write its Store data. A failed persistence operation retains the previous safe mode and does not arm the controller.

## Known limitations

- Software load shedding is not a substitute for breakers, hardware protection, or an electrician's assessment.
- Existing automations remain external owners and must be reviewed separately.
- `observe` is not live evidence; it deliberately sends no physical service calls.
- A persisted fault requires explicit evidence-based reconciliation and is not cleared by restart.
- Guarded restore only re-enables loads the planner itself shed; it never starts never-shed loads, is disarmed on restart (re-arm explicitly), and excludes `climate` loads.
- This repository has not received an official Home Assistant Core quality-scale review.

## Removal

1. Set planner mode to `off` and use `observe` while preparing removal.
2. Remove the config entry from Home Assistant.
3. Remove the HACS/manual package only after the config entry is gone.
4. Review external automations separately; integration removal does not modify them.

## Controlled verification

Before any live activation:

1. Run the local tests, compile, JSON/YAML/resource checks, and static no-admission scan.
2. Install with mode `off` and execution mode `observe`.
3. Verify entity IDs, load/safety sources, and configured shedding order.
4. Exercise unknown/unavailable/wrong-unit inputs using helpers or non-critical test entities.
5. Verify that overload and safety events can issue only bounded stop actions and that readback failures are surfaced.
6. Verify restart restoration of persisted `auto`/`off` mode in a separately approved test window.

No local test is evidence of a live Home Assistant deployment. The detailed procedure is in [`HA_VERIFICATION.md`](HA_VERIFICATION.md).

## Local Git checkpoints

This repository uses Git locally for reviewable checkpoints and reversible source changes; no GitHub repository or remote is required. Track the project-specific `AGENTS.md` after the same secret scan as source files. Keep Home Assistant runtime state, credentials, caches, generic Hermes skills/memory, and generated artifacts outside commits. The commit/deploy/rollback workflow is documented in [`docs/development/git-workflow.md`](docs/development/git-workflow.md).
