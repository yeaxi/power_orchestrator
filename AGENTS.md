# Power Orchestrator project context

## Architecture

Prefer reactive/declarative control and explicit state transitions over opaque imperative orchestration. Keep policy, ownership, safety gates, and physical actions separate and inspectable.

## Change discipline

- No live cutover before all local, correspondence, safety, and runtime gates pass.
- Use Git commits for accepted changes and Git revert/rollback for rejected changes; do not patch the live system as a shortcut.
- Local tests and a successful process exit are not evidence of live Home Assistant behavior.
- Physical or external side effects require explicit approval for the exact operation.

## Evidence

Record exact sources, timestamps, thresholds, denominators, coverage, and uncertainty. Treat unknown, stale, contradictory, or unverifiable state as blocked. Keep project-specific policies here rather than duplicating them in generic Hermes skills.
