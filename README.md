# Power Orchestrator

Turns optional Home Assistant loads off when whole-house power stays too high, then turns those same loads back on automatically when capacity is safe again.

It does not start new loads, prioritize solar, or use forecasts.

Docs: [yeaxi.github.io/power_orchestrator](https://yeaxi.github.io/power_orchestrator/)

## Requirements

- Home Assistant 2026.8.2 or newer
- A whole-house load sensor in `W` or `kW`
- One safety source: a grid-loss binary sensor (`on` means grid is up), or a battery SoC sensor in `%`
- One or more optional loads on a `switch`, `light`, or `input_boolean`

Energy Dashboard discovery is optional. It never authorizes a physical action.

## Installation

### HACS

1. Add `https://github.com/yeaxi/power_orchestrator` as a custom repository (type: Integration).
2. Install Power Orchestrator. HACS installs and updates from published releases.
3. Restart Home Assistant.
4. Add it from **Settings → Devices & Services**.

### Manual

1. Copy `custom_components/power_orchestrator/` into `config/custom_components/`.
2. Restart Home Assistant.
3. Add it from **Settings → Devices & Services**.

Full steps: [Install](https://yeaxi.github.io/power_orchestrator/install/).

## Configuration

Setup is a UI flow: load sensor, explicit overload thresholds with dwell times, optional loads, shedding order, then grid or battery safety.

A new entry starts in **Observe**. Switch to **Auto** when you want physical shedding and automatic restore.

Field-level detail: [Configure](https://yeaxi.github.io/power_orchestrator/configuration/).

## Supported functionality

Manages existing Home Assistant entities. Supported actuators are `switch`, `light`, and `input_boolean`. One aggregate load sensor. One grid-loss or battery safety source. Optional measured-power sensors and actuator groups.

When a threshold holds for its dwell, Auto sheds one known-on load per cycle. Shed loads join a persistent pending-restore queue. When aggregate load plus the next device's expected power stays below the lowest threshold for 60 seconds, Auto restores pending loads one by one in reverse shed order.

Not supported: PV priority, forecasts, generation-based admission, or turning on loads that are not pending restore.

Entities and services: [Entities and services](https://yeaxi.github.io/power_orchestrator/entities-and-services/).

## Use cases

**Whole-house overload protection.** Set the load sensor, explicit thresholds, and optional loads. When a threshold holds, one known-on load is shed per cycle.

**Shadow migration.** Leave mode in **Observe** while you review status and reason codes. Observe performs zero physical actions, including on grid loss.

**Failed readback.** A missing or contradictory stop leaves the load faulted. Clear it only after you verify the actuator and load.

## Data updates

The coordinator polls and also reacts to relevant state changes. An averaging window cannot make an unavailable sensor valid. Physical actions in Auto are guarded stop and automatic restore of pending loads. Invalid load or unavailable safety telemetry blocks those actions and raises a persistent notification.

## Troubleshooting

If status is `safety_blocked`, check the reason code, Grid OK, Faulted, Action journal, and Status attributes for pending restore IDs. Typical causes: missing input, wrong units, bad persisted state, or failed readback.

Do not bypass a faulted load with a raw `turn_off` / `turn_on`. Verify the actuator, then use the integration's clear-quarantine path.

More cases: [Troubleshooting](https://yeaxi.github.io/power_orchestrator/troubleshooting/).

## Known limitations

- This is not a substitute for breakers or an electrician.
- Other automations stay in control until you review them.
- Observe is not live evidence. It performs zero physical actions, including on grid loss.
- A persisted fault survives restart until you reconcile it with evidence.
- Automatic restore only re-enables loads in the pending queue, one per cycle, after a continuous 60-second safe-capacity window and a newer aggregate report.

## Removal

1. Set mode to `off` or `observe`.
2. Remove the config entry.
3. Remove the HACS or manual files after the entry is gone.
4. Review other automations yourself. Removal does not change them.

## Report a bug

Open a [bug report](https://github.com/yeaxi/power_orchestrator/issues/new?template=bug.yml). Include Home Assistant version, integration version, status, reason code, and logs. Strip tokens and passwords.

## See also

- [Docs](https://yeaxi.github.io/power_orchestrator/)
- [Issues](https://github.com/yeaxi/power_orchestrator/issues)
- [Contributing](CONTRIBUTING.md)
