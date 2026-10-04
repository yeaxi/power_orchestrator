# Power Orchestrator project context

## Architecture

Prefer reactive/declarative control and explicit state transitions over opaque imperative orchestration. Keep policy, ownership, safety gates, and physical actions separate and inspectable.

Ordinary planner actions are stop-only. The sole ON-capable exception is the optional `auto_stack` restore executor: at most one ticket-owned logical-device restore dispatch per evaluation cycle, only for an explicit per-device opt-in and an active durable planner ticket. This is not generic load admission or automatic re-enabling, and it remains unverified against a live Home Assistant runtime until the controlled verification procedure is executed.

## Change discipline

- No live cutover before all local, correspondence, safety, and runtime gates pass.
- Use Git commits for accepted changes and Git revert/rollback for rejected changes; do not patch the live system as a shortcut.
- Local tests and a successful process exit are not evidence of live Home Assistant behavior.
- Physical or external side effects require explicit approval for the exact operation.

## Evidence

Record exact sources, timestamps, thresholds, denominators, coverage, and uncertainty. Treat unknown, stale, contradictory, or unverifiable state as blocked. Keep project-specific policies here rather than duplicating them in generic Hermes skills.

## Agent skills

### Issue tracker

Track issues and specs as local Markdown under `.scratch/<feature>/`. Read `docs/agents/issue-tracker.md` before ticket operations.

### Triage labels

Use the five default triage role names. Read `docs/agents/triage-labels.md` before changing triage state.

### Domain docs

Use a single-context layout: root `CONTEXT.md` and `docs/adr/`. Read `docs/agents/domain.md` before exploring or changing domain concepts.

## Local workflow

Before editing or verifying a change, read `docs/development/agent-environment.md` for source preflight and the shared local/CI runner. For runtime readiness or incident evidence, use its saved-diagnostics procedure.
