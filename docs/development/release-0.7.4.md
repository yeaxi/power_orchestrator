# Release 0.7.4

This release brings the deployed 0.7.3 source into the public release line and
changes telemetry loss into a command-free blocked state. The imported installed
baseline is `76fc67fcb00a0c006a8b8836654ee3e4c6dc5f32`; its Python files and manifest
matched the inspected Home Assistant installation. Telemetry changes were reviewed
and tested at `cffb9a1f0f7f5cbb5198af9a4a9cd08bf498b63d` before release integration.

Missing, invalid, or stale data no longer switches appliances off. Valid current
inputs automatically clear an identified telemetry fault and resume monitoring,
including after restart. Recovery does not turn appliances on, clear actuator or
storage faults, remove causal fences, or replay legacy telemetry-loss tickets.
Confirmed supply loss and measured overload keep their independent rules.

The installed baseline includes durable restore intents/tickets, causal report
listeners, actuator command/readback mapping, startup telemetry grace, and optional
battery minimums. Existing public tests now exercise those contracts: config-entry
migration 2.4, all eight lifecycle-managed services, actual tickets instead of bare
restore IDs, fresh aggregate timestamps, and actual stop/restore order. The unit
coverage threshold remains 75%; the pinned CI and ecosystem workflows are retained.

Deployment uses the published deterministic `power_orchestrator.zip` asset, a
timestamped backup of the installed component, `ha core check`, and post-restart
source/runtime verification. The first runtime smoke uses Observe mode and performs
no device commands. Local tests do not prove physical appliance behavior.
