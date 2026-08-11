# Power Orchestrator — Load-Shedding Specification

## Scope

Power Orchestrator is a bounded Home Assistant integration for **load shedding** with an optional **guarded restore** of the loads it shed. Its physical actions are: turn off already-running optional loads when aggregate demand or a safety condition requires it, and — only when guarded restore is enabled and explicitly armed — turn those same loads back on once headroom returns.

The integration does **not** implement:

- PV or solar-priority logic;
- Forecast.Solar or generation-based admission;
- normal load enabling or starting of never-shed loads;
- automatic re-enabling of loads it did not itself shed;
- battery/PV optimization or energy-production scheduling.

These are removed from the runtime, config-flow, service, and persisted configuration contracts. Legacy persisted fields are accepted only long enough to be removed by the versioned migration.

Guarded restore is a fail-closed, opt-in exception to the historical stop-only surface: it is disabled by default, per-load opt-in, requires `live` execution plus explicit arming, only re-enables loads the planner itself shed (never emergency/manual/service stops), excludes `climate` loads, restores at most one load per evaluation cycle, never runs during an active shed/emergency or a pending post-shed fence, and confirms the ON transition with the same causal readback used for stops.

## Safety boundaries

The controller has two independent controls:

| Control | Values | Meaning |
|---|---|---|
| Planner mode | `auto`, `off` | Whether normal shedding policy evaluation is armed. `auto` never authorizes a start. |
| Execution mode | `observe`, `live` | `observe` records decisions only. `live` permits guarded stop commands after all safety checks. |

`off` blocks normal planned physical actions. Emergency safety interlocks remain active so an unsafe already-on load can still be stopped. Neither mode can create a new load.

## Validated telemetry

Before a normal decision, the coordinator validates:

- aggregate load sensor existence, source-reported availability, numeric value, non-negative value, and power unit;
- grid-safety source existence, source-reported availability, and semantics;
- managed-device relay and optional actuator states;
- optional measured-power sensors, including units and source-reported availability.

Unknown, unavailable, contradictory, non-finite, negative, or incorrectly-unitized aggregate/device telemetry fails closed for normal decisions. It never becomes a synthetic `0 W` value or grants permission for a normal physical action. An unavailable, OFF, or otherwise unsafe grid source selects only the emergency stop path for known-on loads; it never authorizes a normal action.

## Evaluation order

Each evaluation is serialized and follows this order:

1. Reconcile managed device states and external ownership.
2. Read and validate aggregate load and safety telemetry.
3. On grid loss or another emergency interlock, select known-on managed loads for stopping. In `observe`, record the decision without calling a physical service.
4. If required telemetry is invalid, publish `safety_blocked` and perform no normal action.
5. If a configured overload threshold has remained active for its dwell time, shed at most one logical device at a time.
6. Confirm the stop with causal relay/actuator readback and wait for a newer aggregate-load report before another normal shed.
7. If — and only if — guarded restore is enabled and armed, no shed fired, and the aggregate load has stayed at or below the restore ceiling for the restore dwell, restore at most one planner-shed load, confirmed by causal ON readback.
8. Otherwise remain in monitoring/observe state.

No evaluation branch requests a start or enables a load based on capacity, PV, forecast, or battery state. The only `turn_on` path is the guarded restore of a load the planner itself shed, subject to all gates in the Scope section.

## Logical devices and shedding

A managed device contains:

- a stable `device_id`;
- one primary control entity;
- optional coupled actuator entities;
- an optional measured-power sensor;
- a conservative expected-power value for diagnostics and shedding ordering;
- an explicit shedding priority.

A coupled relay and climate/HVAC entity is one logical device. Partial or unconfirmed stop readback is a fault, not a successful shed.

Normal shedding is bounded to one logical device per evaluation. The coordinator restores a device only through the guarded restore lane (opt-in, armed, planner-shed only, one per cycle). A device that was already on before the integration observed it is treated as externally owned for the configured grace period; the planner does not use that observation as permission to start it, and a manual re-enable relinquishes any restore claim.

## Faults and quarantine

The following conditions latch a device fault/quarantine state:

- actuator exception;
- failed or ambiguous stop command;
- failed causal readback;
- invalid durable action-journal recovery;
- unsafe contradictory device telemetry.

Fault/quarantine state is persisted and blocks further unsafe actions for that device. Clearing quarantine requires verified valid OFF readback and safe telemetry; it never turns a device on. Home Assistant repair issues expose the fault without putting logical or entity IDs into public issue identifiers.

## Persistence and migration

- The planner mode is persisted independently of transient runtime state.
- A valid stored `auto` or `off` value is restored after Home Assistant restart.
- Missing, malformed, or unsafe persisted mode data falls back to `off`.
- The runtime store persists device ownership, faults, quarantine, and action-journal state.
- Config-entry version `2.1` removes obsolete PV/start/recovery fields from both `data` and `options`, including nested legacy device keys. The old `recovery_blocked_devices` runtime key is migrated to `quarantined_devices`.

## Public services

The public service surface is stop-first, with a guarded restore that only re-enables planner-shed loads:

- `force_evaluate` — run a non-physical evaluation;
- `set_mode` — persistently arm/disarm normal shedding policy;
- `request_stop` — request a guarded logical-device stop;
- `clear_quarantine` — clear a fault only after safety validation;
- `set_execution_mode` — select `observe` or explicitly confirmed `live` execution;
- `authorize_restore` — arm the guarded restore lane (no physical action; requires `live`+`auto`);
- `request_restore` — request one guarded restore of a planner-shed load.

There is no public start/request-start service and no service that enables a never-shed load.

## Verification requirements

Before live activation:

1. Run the full local test, type, lint, compile, coverage, resource, and native loader gates.
2. Deploy only a hash-verified component archive and retain a timestamped rollback backup.
3. Run `ha core check` before restart and poll HTTP readiness after restart.
4. Confirm the config entry is version `2.1` and contains no legacy PV/start fields.
5. Confirm planner mode is `off`, execution mode is `observe`, journal health is good, and no unresolved actions exist.
6. Use only non-physical observe evaluation for the first runtime smoke.
7. Read every managed relay, coupled actuator, and power sensor before and after activation. Any unexpected state change is reported; it is never silently compensated.

The final acceptance report must separate local evidence, live runtime evidence, physical service calls (expected to be none for observe validation), rollback evidence, and remaining blockers.
