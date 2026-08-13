# Troubleshooting

Look at Status, Reason code, Current load, Grid OK, Faulted, and Action journal healthy first.

## `safety_blocked`

Usual causes:

- load sensor missing, unavailable, unknown, negative, or not in `W`/`kW`
- grid sensor off, missing, or unavailable
- battery SoC at or below threshold, or invalid
- failed or contradictory readback
- corrupt persisted state

An averaging window cannot make a bad sensor valid.

## A load is faulted or quarantined

Do not clear it with a raw Home Assistant service call. Check the real actuator state and measured power. Then use `power_orchestrator.clear_quarantine` only after that evidence is valid.

Missing proof keeps the load blocked on purpose. Restart does not clear a persisted fault.

## Mode did not persist

The config entry must stay loaded and able to write its Store. A failed write keeps the previous safe mode. It does not arm the controller.

A new entry starts `off`. Only a valid stored `auto` comes back after restart.

## Restore did not run

Restore needs all of: global enable, per-load opt-in, `live`, `auto`, and an explicit arm. It only re-enables loads the planner itself shed. It disarms on restart and never restores `climate` loads.

## `observe` did nothing physical

That is the point. `observe` records intended actions and never sends service calls. It is not live evidence.

## Report a bug

Open a [bug report](https://github.com/yeaxi/power_orchestrator/issues/new?template=bug.yml). Include Home Assistant version, integration version, status, reason code, and logs. Strip tokens and passwords.
