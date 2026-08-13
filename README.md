# Power Orchestrator

Turns optional Home Assistant loads off when whole-house power is too high. Can turn those same loads back on later, only if you opt in and arm restore.

It does not start new loads, prioritize solar, or use forecasts.

Docs: [yeaxi.github.io/power_orchestrator](https://yeaxi.github.io/power_orchestrator/)

## Requirements

- Home Assistant 2026.7.4 or newer
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

Setup is a UI flow: load sensor and limits, optional loads, shedding order, then grid or battery safety. Guarded restore stays off until you enable it globally, opt in per load, and arm it under `live` execution.

Field-level detail: [Configure](https://yeaxi.github.io/power_orchestrator/configuration/).

## Supported functionality

Manages existing Home Assistant entities. Supported actuators are `switch`, `light`, and `input_boolean`. One aggregate load sensor. One grid-loss or battery safety source. Optional measured-power sensors and actuator groups. Optional guarded restore of loads the planner itself shed.

Not supported: PV priority, forecasts, generation-based admission, or turning on loads the planner did not shed.

Entities and services: [Entities and services](https://yeaxi.github.io/power_orchestrator/entities-and-services/).

## Use cases

**Whole-house overload protection.** Set the load sensor, limits, and optional loads. When a threshold holds, one known-on load is shed per cycle.

**Shadow migration.** Leave execution in `observe` while you review status and reason codes. `observe` never sends physical service calls.

**Failed readback.** A missing or contradictory stop leaves the load faulted. Clear it only after you verify the actuator and load.

## Data updates

The coordinator polls and also reacts to relevant state changes. An averaging window cannot make an unavailable sensor valid. Physical actions are stop, plus optional armed restore of planner-shed loads.

## Troubleshooting

If status is `safety_blocked`, check the reason code, Grid OK, Faulted, and Action journal sensors. Typical causes: missing input, wrong units, bad persisted state, or failed readback.

Do not bypass a faulted load with a raw `turn_off` / `turn_on`. Verify the actuator, then use the integration's clear-quarantine path.

More cases: [Troubleshooting](https://yeaxi.github.io/power_orchestrator/troubleshooting/).

## Known limitations

- This is not a substitute for breakers or an electrician.
- Other automations stay in control until you review them.
- `observe` is not live evidence.
- A persisted fault survives restart until you reconcile it with evidence.
- Guarded restore only re-enables planner-shed loads, disarms on restart, and skips `climate` loads.

## Removal

1. Set planner mode to `off`. Use `observe` while you prepare.
2. Remove the config entry.
3. Remove the HACS or manual files after the entry is gone.
4. Review other automations yourself. Removal does not change them.

## Report a bug

Open a [bug report](https://github.com/yeaxi/power_orchestrator/issues/new?template=bug.yml). Include Home Assistant version, integration version, status, reason code, and logs. Strip tokens and passwords.

## See also

- [Docs](https://yeaxi.github.io/power_orchestrator/)
- [Issues](https://github.com/yeaxi/power_orchestrator/issues)
- [Contributing](CONTRIBUTING.md)
