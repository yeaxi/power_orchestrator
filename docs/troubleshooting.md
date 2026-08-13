# Troubleshooting

Look at Status, Reason code, Current load, Grid OK, Faulted, and Action journal healthy first. Status attributes list `pending_restore_ids` and `pending_restore_names`.

## `safety_blocked`

Usual causes:

- load sensor missing, unavailable, unknown, negative, or not in `W`/`kW`
- grid sensor missing or unavailable
- battery SoC invalid or unavailable
- failed or contradictory readback
- corrupt persisted state

An averaging window cannot make a bad sensor valid. Invalid load or unavailable safety telemetry also creates a persistent notification and never calls device services.

## A load is faulted or quarantined

Do not clear it with a raw Home Assistant service call. Check the real actuator state and measured power. Then use `power_orchestrator.clear_quarantine` only after that evidence is valid.

Missing proof keeps the load blocked on purpose. Restart does not clear a persisted fault.

## Mode did not persist

The config entry must stay loaded and able to write its Store. A failed write keeps the previous safe mode. It does not enable Auto.

A new entry starts in `observe`. Only a valid stored `auto`, `observe`, or `off` comes back after restart.

## Restore did not run

Automatic restore needs Auto mode, a pending queue entry, confirmed OFF, clear fences, and a continuous 60-second safe-capacity window below the lowest threshold. It restores one load per cycle in reverse shed order. A newer aggregate report is required after each restore.

A manual ON of a pending load under safe capacity removes it from the queue. Under enforced overload or grid loss, Auto re-sheds it and keeps it queued.

## Observe did nothing physical

That is the point. Observe records intended actions and performs zero physical actions, including on grid loss. It is not live evidence.

## Report a bug

Open a [bug report](https://github.com/yeaxi/power_orchestrator/issues/new?template=bug.yml). Include Home Assistant version, integration version, status, reason code, and logs. Strip tokens and passwords.
