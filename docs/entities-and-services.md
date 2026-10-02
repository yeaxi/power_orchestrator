# Entities and services

All entities use stable config-entry-scoped unique IDs.

## Entities

| Role | Type | Meaning |
|---|---|---|
| Mode | Select | `auto` / `observe` / `off` |
| Status | Sensor | Current safety and shedding status. Attributes include `pending_restore_ids` and `pending_restore_names`. |
| Current load | Sensor | Fresh aggregate load in W |
| Average load | Sensor | Windowed aggregate load in W |
| Available capacity | Sensor | Remaining headroom below the lowest threshold |
| Last action | Sensor | Last bounded action summary |
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
| `power_orchestrator.set_mode` | `mode` | Persist `auto`, `observe`, or `off`. |
| `power_orchestrator.request_stop` | `device_id`, optional `source` | Request one guarded load stop. `device_id` is the configured id, not a raw entity id. Queues the load for automatic restore when the stop confirms. |
| `power_orchestrator.clear_quarantine` | `device_id`, optional `source` | Clear a fault only after verified OFF readback and safe telemetry. Never turns a load on. |
| `power_orchestrator.clear_fault` | none | Explicit reconciliation for retained faults; identified telemetry faults clear automatically after valid inputs return. Never turns a load on. |
| `power_orchestrator.set_restore_intent` | `device_id`, `source`, `active`, `expires_at`, optional permit/request metadata | Publish or remove an owner's bounded restore permission; does not issue an ON command. |
| `power_orchestrator.set_request` | compatibility fields for restore intent | Compatibility alias for existing owner automations. |
| `power_orchestrator.cancel_restore` | `device_id` | Cancel a durable restore ticket without changing device state. |

There is no service that starts a never-shed load. Automatic restore is the only ON path, and it only targets pending loads.
