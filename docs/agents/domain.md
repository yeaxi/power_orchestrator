# Domain docs

## Layout

Use a single-context layout:

- `CONTEXT.md` at the repo root holds the domain glossary.
- `docs/adr/` holds numbered architecture decision records.

Create these lazily through `domain-modeling`: write `CONTEXT.md` when the first term is resolved and an ADR when a decision needs recording.

## Before exploring the codebase

Read `CONTEXT.md`, when present, and ADRs relevant to the area being explored. If either is absent, proceed silently.

Existing architecture references are in `docs/architecture/index.md` and `docs/architecture/power-orchestrator-spec.md`. Consult them when work touches orchestrator policies or state transitions.

## Use the glossary's vocabulary

Use terms as defined in `CONTEXT.md` in issue titles, proposals, hypotheses, and test names. If a needed concept is missing, reconsider whether it belongs to the domain or note the gap for `domain-modeling`.

## Flag ADR conflicts

Explicitly identify any ADR your proposal contradicts and explain why the decision should be revisited.
