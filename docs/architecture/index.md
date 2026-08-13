# Architecture

Power Orchestrator is a bounded load-shedding controller. Policy, ownership, safety gates, and physical actions stay separate.

The [load-shedding specification](power-orchestrator-spec.md) is the project contract. It does not authorize live cutover, Home Assistant service calls, or physical actions.

Agent and change-discipline policy lives in the repository-root `AGENTS.md`. That file is not part of this site.

Use Git review and rollback before any approved deployment. A green local test is not live Home Assistant evidence.
