# Retrospective improvements, 2026-10-04

Source baseline: release 0.7.4, commit `1dec95877daabb256e8c9698650daed982a37136`.
Historical evidence and agent policy were preserved from local checkpoint `554ac05`.
The original dirty checkout was preserved; implementation uses a separate worktree.

## Changes

- Read-only Git/source preflight exposes the actual baseline, packaging versions and
  dirty paths, and blocks divergent history or inconsistent packaging.
- CI and local execution share a pinned runner with explicit pytest plugins,
  collection smoke, existing quality/coverage gates and a focused safety suite.
- Lifecycle regressions retain once-seeded corruption through save/reload and cover
  telemetry invalidation during notification and persistence awaits.
- Journal decision evidence is captured before dispatch and remains immutable across
  phases and storage round-trips. Outcome codes are independent. Old records are
  compatible and their unknown causes remain unknown.
- Saved process diagnostics drive strict, bounded-age Observe readiness; stale
  registry model text cannot create a false timeout. Incident exports allowlist
  facts and remove configuration, identities, credentials and free text.
- Compact agent pointers, an evidence index and the current GitHub workflow replace
  obsolete local-only navigation. Historical private evidence stays outside the
  public documentation build.

## Verification

Local environment: Python 3.14.3, Home Assistant 2026.8.2 and plugin 0.13.356;
all `requirements-ci.txt` dependency pins matched. CI retains Python 3.14.2.

- Full shared runner: 241 unit tests and 67 real-HA tests passed, each suite also
  reporting 19 subtests. These counts overlap with focused runs and are not summed
  as unique tests. Ruff, mypy (24 source files), compileall and JSON/YAML passed.
- Unit branch coverage: combined 75.6378009342%, above the unchanged 75% gate;
  3382/4240 statements and 828/1326 branches covered.
- Final CLI/resource regressions: 24 passed. Compact collection output and the
  focused safety runner passed: 29 tests plus 19 subtests, including the explicit
  service-error/readback-timeout pair.
- Strict documentation build passed with MkDocs Material 9.7.7. Historical agent
  references and ADR links into private `.scratch` evidence are excluded from
  the public site.
- The two original journal regressions were red before the implementation and
  green afterward: immutable cause/context after save/load, and pre-dispatch
  evidence retained after actual config-entry reload.

Commands used: `/private/tmp/po-release-ci/bin/python scripts/local_checks.py`,
the same runner with `--suite safety`, and
`/private/tmp/po-retro-docs/bin/python -m mkdocs build --strict -d /tmp/po-retro-docs-site`.
See [Agent workflow and evidence](agent-environment.md) for portable invocations.

Follow-up for 0.7.5: 249 unit tests and 67 real-HA tests passed, each with
19 subtests. The eight capture regressions cover byte limits, failed readers,
blocked stdin transfer, full stdin delivery, timeout, private rotation and remote
log bound rejection. Existing battery-policy coverage now asserts actual charge
and the per-device minimum in both confirmed stop records. Two independent
Standards/Spec reviews found and resolved the capture deadline and missing battery
context; no actionable findings remain in their follow-up. The strict docs build
passed. Private `.scratch` evidence is retained locally, excluded from the public
branch's changes and history, and ignored for future staging.

## Boundaries

Changes add evidence and local tooling; they do not change action admission,
ownership, restore ordering or dispatch permissions. The journal snapshot uses the
decision's current inputs without accepting a new policy sample. New evidence fields
are additive; no storage migration is needed. The publishable artifact is version
0.7.5 so installed source can be distinguished from the 0.7.4 baseline.

The original implementation was verified locally. The follow-up adds a bounded,
read-only live capture tool; its limits and privacy requirements are documented
in the agent environment guide. Deployment evidence is recorded separately.
Revert the source changes to roll back the implementation.
