# Install

Requires Home Assistant 2026.7.4 or newer.

## What you need

- A whole-house load sensor in `W` or `kW`
- One safety source:
  - a grid-loss binary sensor (`on` means grid is up), or
  - a battery SoC sensor in `%` plus a threshold
- One or more optional loads on a `switch`, `light`, or `input_boolean`
- Optional measured-power sensor for each managed load

Energy Dashboard discovery is optional. It never authorizes a physical action.

## HACS

1. In HACS, open **Custom repositories**.
2. Add `https://github.com/yeaxi/power_orchestrator` with type **Integration**.
3. Install **Power Orchestrator**.
4. Restart Home Assistant.
5. Open **Settings → Devices & Services → Add Integration → Power Orchestrator**.

Leave planner mode `off` and execution mode `observe` until you have checked entity IDs, limits, and shedding order.

HACS installs and updates from published releases, so it lists released versions
rather than `main`. Updates arrive the same way: HACS offers the new version once
a release is published, and you restart Home Assistant to load it.

## Manual

1. Copy `custom_components/power_orchestrator/` into `config/custom_components/power_orchestrator/`.
2. Restart Home Assistant.
3. Add the integration from **Settings → Devices & Services**.

Same first-run rule: `off` and `observe` until you are ready.

## After install

The integration manages entities you already have. It does not create the load sensor or the switches.

Next: [Configure](configuration.md).
