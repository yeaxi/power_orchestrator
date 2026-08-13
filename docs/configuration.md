# Configure

Setup is a UI flow. Invalid, unknown, unavailable, non-finite, negative, or unsupported-unit input is rejected or fails closed.

Energy Dashboard discovery, if present, only suggests candidates.

## Config flow

1. **Auto-discovery.** Optional load and safety candidates from the Energy Dashboard.
2. **Load monitoring.** Aggregate load sensor, maximum load, averaging period, safety reserve, hysteresis, and overload thresholds with dwell times.
3. **Optional devices.** Controllable entity, expected power, optional measured-power sensor, optional actuator group, and per-load guarded-restore opt-in (off by default).
4. **Priority and pause.** Shedding order and pause after a shed.
5. **Grid loss.** A grid binary sensor, or a battery SoC sensor and threshold.

Options and Reconfigure expose the same safety settings. Options also has the global restore controls.

## Load monitoring

| Field | Meaning |
|---|---|
| Load sensor | Authoritative whole-house power. Wrong units or unavailable readings block planner actions. |
| Maximum load | Hard ceiling in watts. Above it, one lowest-priority known-on load is shed per cycle. |
| Averaging period | Window for average load. Does not make an unavailable sensor valid. |
| Safety reserve | Watts kept unused below the maximum. |
| Hysteresis | Extra margin after a shed so the same tier does not flap. Never turns a load on. |
| Thresholds | Power limit plus dwell. Load must stay above the limit for the dwell before one shed. |

## Optional devices

Each managed load needs a `switch`, `light`, or `input_boolean`. Expected power is planning telemetry only. A measured-power sensor is optional. Extra actuators in the same logical load must all confirm readback.

Guarded restore per load is off by default. Climate loads are never restored.

## Safety source

**Grid sensor.** `on` means grid is up. `off`, missing, or unavailable triggers emergency stop of known-on optional loads.

**Battery threshold.** SoC at or below the threshold, or an invalid reading, is treated as grid loss.

## Guarded restore

Off until all of these are true:

- global restore enabled in Options
- the load opted in
- execution is `live`
- planner mode is `auto`
- restore is explicitly armed

Restore only re-enables loads the planner itself shed. It disarms on restart. Arm it again if you still want it.

| Field | Meaning |
|---|---|
| Restore threshold | Aggregate load at or below which restore headroom accrues. |
| Restore hysteresis | Margin below that threshold before restore is considered. |
| Restore dwell | Seconds the load must stay at or below the restore ceiling. |
| Restore cooldown | Minimum seconds before the same load may restore again. |

## Modes

Planner mode `auto` or `off` survives restart. A new entry starts `off`. Corrupt stored mode falls back to `off`.

Execution `observe` records intended actions and never calls a physical service. `live` requires explicit confirmation.

`off` blocks ordinary sheds. Emergency safety stops stay active.
