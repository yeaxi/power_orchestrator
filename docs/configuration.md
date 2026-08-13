# Configure

Setup is a UI flow. Invalid, unknown, unavailable, non-finite, negative, or unsupported-unit input is rejected or fails closed.

Energy Dashboard discovery, if present, only suggests candidates.

## Config flow

1. **Auto-discovery.** Optional load and safety candidates from the Energy Dashboard.
2. **Load monitoring.** Aggregate load sensor, averaging period, and one or more explicit overload thresholds with dwell times.
3. **Optional devices.** Controllable entity, expected power, optional measured-power sensor, and optional actuator group.
4. **Priority and pause.** Shedding order and pause after a shed.
5. **Grid loss.** A grid binary sensor, or a battery SoC sensor and threshold.

Options and Reconfigure expose the same safety settings.

## Load monitoring

| Field | Meaning |
|---|---|
| Load sensor | Authoritative whole-house power. Wrong units or unavailable readings block planner actions. |
| Averaging period | Window for average load. Does not make an unavailable sensor valid. |
| Thresholds | Power limit plus dwell. Load must stay above the limit for the dwell before one shed. Add thresholds in strictly increasing power order. |

A zero-dwell threshold sheds as soon as the load is above its limit. A non-zero dwell waits for a continuous exceedance.

## Optional devices

Each managed load needs a `switch`, `light`, or `input_boolean`. Expected power is planning telemetry only. A measured-power sensor is optional. Extra actuators in the same logical load must all confirm readback.

## Safety source

**Grid sensor.** `on` means grid is up. A confirmed `off` triggers emergency stop of known-on optional loads in Auto. Missing or unavailable state blocks actions and creates a persistent notification.

**Battery threshold.** SoC at or below the threshold triggers emergency stop in Auto. A missing or invalid reading blocks actions and creates a persistent notification.

## Automatic restore

Auto restores pending loads without any extra restore configuration.

Restore runs when all of these are true:

- mode is `auto`
- the load is in the persistent pending-restore queue
- aggregate load plus the candidate's expected power stays strictly below the lowest threshold
- that safe capacity has held for 60 continuous seconds
- a newer aggregate report is available after the previous restore
- the load is confirmed OFF, not faulted or quarantined, and not a `climate` actuator

Restore turns on at most one pending load per evaluation cycle, in reverse actual shed order.

## Modes

One mode select: `off`, `observe`, or `auto`. The value survives restart. A new entry starts in `observe`. Corrupt stored mode falls back to `observe`.

| Mode | Effect |
|---|---|
| `off` | Normal shedding and restore are disabled. No physical services. |
| `observe` | Evaluates and records intended actions. Performs zero physical actions, including on grid loss. |
| `auto` | Permits guarded physical OFF for shedding and grid loss, and automatic ON for pending restore. |
