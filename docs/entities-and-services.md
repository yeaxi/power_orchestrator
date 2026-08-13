# Entities and services

All entities use stable config-entry-scoped unique IDs.

## Entities

| Role | Type | Meaning |
|---|---|---|
| Planner mode | Select | `auto` / `off` |
| Status | Sensor | Current safety and shedding status |
| Current load | Sensor | Fresh aggregate load in W |
| Average load | Sensor | Windowed aggregate load in W |
| Available capacity | Sensor | Remaining headroom |
| Last action | Sensor | Last bounded action summary |
| Execution mode | Sensor | `observe` or `live` |
| Reason code | Sensor | Policy or safety reason |
| Last operation | Sensor | Action-journal projection |
| Grid OK | Binary sensor | Safety source is valid and safe |
| Faulted | Binary sensor | At least one load has a durable fault |
| Action journal healthy | Binary sensor | Durable journal can accept writes |

## Services

Do not call raw `switch.turn_off`, `light.turn_on`, or similar around this integration. Use these services.

| Service | Fields | Effect |
|---|---|---|
| `power_orchestrator.force_evaluate` | none | Run one guarded evaluation. Does not bypass safety. |
| `power_orchestrator.set_mode` | `mode` | Persist `auto` or `off`. |
| `power_orchestrator.request_stop` | `device_id`, optional `source` | Request one guarded load stop. `device_id` is the configured id, not a raw entity id. |
| `power_orchestrator.clear_quarantine` | `device_id`, optional `source` | Clear a fault only after verified OFF readback and safe telemetry. Never turns a load on. |
| `power_orchestrator.set_execution_mode` | `execution_mode`, optional `confirm_live` | `observe` or explicitly confirmed `live`. |
| `power_orchestrator.authorize_shedding` | `device_ids`, `confirm_takeover` | In `observe`, claim listed already-on loads for a later live stop test. No physical call. |
| `power_orchestrator.authorize_restore` | `confirm_restore` | Arm guarded restore. No physical action. Needs `live` and `auto`. |
| `power_orchestrator.request_restore` | `device_id`, `confirm_restore`, optional `source` | Request one guarded restore of a planner-shed load. |

There is no service that starts a never-shed load.
