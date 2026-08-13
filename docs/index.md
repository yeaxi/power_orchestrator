# Power Orchestrator

Turns optional Home Assistant loads off when whole-house power stays too high, then turns those same loads back on automatically when capacity is safe again.

It does not start new loads, prioritize solar, or use forecasts.

- [Install](install.md)
- [Configure](configuration.md)
- [Entities and services](entities-and-services.md)
- [Troubleshooting](troubleshooting.md)

A new entry starts in **Observe**. Observe records decisions and performs zero physical actions, including on grid loss. Switch to **Auto** when you want physical shedding and automatic restore.

This software is not a substitute for breakers or an electrician.
