# Power Orchestrator — Load-Shedding Specification

## Scope

Power Orchestrator is a bounded Home Assistant integration for **load shedding** with **automatic restore** of loads it stopped. Its physical actions in Auto are: turn off already-running optional loads when aggregate demand or a safety condition requires it, and turn those same pending loads back on once safe capacity returns.

The integration does **not** implement:

- PV or solar-priority logic;
- Forecast.Solar or generation-based admission;
- normal load enabling or starting of never-shed loads;
- automatic re-enabling of loads that are not in the pending-restore queue;
- battery/PV optimization or energy-production scheduling.

These are removed from the runtime, config-flow, service, and persisted configuration contracts. Legacy persisted fields are accepted only long enough to be removed by the versioned migration.

Automatic restore is always available in Auto. It restores at most one pending load per evaluation cycle, never runs during an active shed or emergency, never runs while a post-action fence is pending, requires a continuous 60-second safe-capacity window below the lowest user threshold, restores in reverse actual shed order, excludes `climate` actuators, and confirms the ON transition with the same causal readback used for stops.

## Safety boundaries

One controller mode replaces the old planner/execution pair:

| Mode | Meaning |
|---|---|
| `off` | Normal shedding and restore are disabled. No physical services. |
| `observe` | Evaluates and records intended actions. Performs zero physical actions, including on grid loss. |
| `auto` | Permits guarded physical OFF for shedding and grid loss, and automatic ON for pending restore. |

No mode can create a new load outside the pending-restore path.

## Validated telemetry

Before a normal decision, the coordinator validates:

- aggregate load sensor existence, source-reported availability, numeric value, non-negative value, and power unit;
- grid-safety source existence, source-reported availability, and semantics;
- managed-device relay and optional actuator states;
- optional measured-power sensors, including units and source-reported availability.

Unknown, unavailable, contradictory, non-finite, negative, or incorrectly-unitized aggregate/device telemetry fails closed for normal decisions. It never becomes a synthetic `0 W` value or grants permission for a normal physical action. Invalid load or unavailable safety telemetry creates a persistent notification and never calls device services.

An unavailable safety source selects `safety_blocked`, not grid loss. A confirmed OFF grid source in Auto selects the emergency stop path for known-on loads.

## Evaluation order

Each evaluation is serialized and follows this order:

1. Reconcile managed device states and handle manual ON of pending loads.
2. Read and validate aggregate load and safety telemetry.
3. On grid loss, stop known-on managed loads in reverse shed-priority order and append each confirmed stop to the pending-restore queue. In Observe or Off, record the decision without calling a physical service.
4. If required telemetry is invalid, publish `safety_blocked`, notify once, and perform no physical action.
5. If a configured overload threshold has remained above its limit for its dwell time, shed at most one logical device. A zero-dwell threshold sheds immediately. Tier timers are monotonic and are not persisted across restart.
6. Confirm the stop with causal relay/actuator readback, append the load to the pending-restore queue, and wait for a newer aggregate-load report before another normal shed.
7. If Auto may restore, no shed fired, fences are clear, and aggregate load plus the next candidate's expected power has stayed strictly below the lowest threshold for 60 seconds, restore at most one pending load, confirmed by causal ON readback, then wait for a newer aggregate report.
8. Otherwise remain in monitoring or observe state.

No evaluation branch requests a start based on capacity, PV, forecast, or battery state. The only `turn_on` path is automatic restore of a pending load.

## Logical devices and shedding

A managed device contains:

- a stable `device_id`;
- one primary control entity;
- optional coupled actuator entities;
- an optional measured-power sensor;
- a conservative expected-power value for diagnostics and restore capacity checks;
- an explicit shedding priority.

A coupled relay and climate/HVAC entity is one logical device. Partial or unconfirmed stop readback is a fault, not a successful shed.

Normal shedding is bounded to one logical device per evaluation. The pending-restore queue is durable across reload and restart. Status attributes expose `pending_restore_ids` and `pending_restore_names`.

A manual ON of a pending load under safe capacity is accepted and removed from the queue. Under enforced overload or grid loss, Auto re-sheds it and keeps it queued. A persistent notification records the outcome.

## Faults and quarantine

The following conditions latch a device fault/quarantine state:

- actuator exception;
- failed or ambiguous stop command;
- failed causal readback;
- invalid durable action-journal recovery;
- unsafe contradictory device telemetry.

Fault/quarantine state is persisted and blocks further unsafe actions for that device. Clearing quarantine requires verified valid OFF readback and safe telemetry; it never turns a device on. Home Assistant repair issues expose the fault without putting logical or entity IDs into public issue identifiers.

## Persistence and migration

- The unified mode is persisted independently of transient runtime state.
- A valid stored `auto`, `observe`, or `off` value is restored after Home Assistant restart.
- Missing, malformed, or unsafe persisted mode data falls back to `observe`.
- The runtime store persists the ordered pending-restore queue, faults, quarantine, and action-journal state.
- Monotonic tier dwell and restore-window timers are never persisted.
- Config-entry version `2.3` stores an explicit thresholds list and strips deleted limit and restore-control fields from both `data` and `options`.

## Public services

The public service surface is stop-first, with automatic restore owned by the evaluation loop:

- `force_evaluate` — run one evaluation;
- `set_mode` — persist `auto`, `observe`, or `off`;
- `request_stop` — request a guarded logical-device stop and queue it for restore when confirmed;
- `clear_quarantine` — clear a fault only after safety validation.

There is no public start service and no service that enables a never-shed load.

## Verification requirements

Before live activation:

1. Run the full local test, type, lint, compile, coverage, resource, and native loader gates.
2. Deploy only a hash-verified component archive and retain a timestamped rollback backup.
3. Run `ha core check` before restart and poll HTTP readiness after restart.
4. Confirm the config entry is version `2.3` and contains an explicit thresholds list with no legacy limit fields.
5. Confirm mode is `observe` or `off`, journal health is good, and no unresolved actions exist.
6. Use only non-physical Observe evaluation for the first runtime smoke.
7. Read every managed relay, coupled actuator, and power sensor before and after activation. Any unexpected state change is reported; it is never silently compensated.

The final acceptance report must separate local evidence, live runtime evidence, physical service calls (expected to be none for Observe validation), rollback evidence, and remaining blockers.
