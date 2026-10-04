# Agent workflow and evidence

## Select the source baseline

Before editing, run from the intended checkout:

```bash
python3 scripts/preflight.py --baseline origin/main
```

The command reads local refs only. It reports the workspace, full HEAD and baseline
commits, branch, packaging versions and dirty paths. Divergent history, missing
refs or inconsistent packaging block the check. Preserve dirty work and use a
separate checkout from the selected baseline; do not reset or overwrite it.

`origin/main` can be stale. Record when it was fetched, or select an exact verified
release commit with `--baseline`. Version equality alone does not establish source
correspondence. For a deployment, compare every component payload file with the
reviewed release ZIP and obtain fresh process diagnostics under the approved
verification procedure. A local preflight never establishes live readiness.

## Reproduce the local gates

Create an isolated environment using the Python version in CI and install
`requirements-ci.txt`. See [Contributing](contributing.md). Then use that
environment's interpreter:

```bash
.venv/bin/python scripts/local_checks.py
.venv/bin/python scripts/local_checks.py --suite safety
```

The shared runner is called by CI for its `quality` and `real-ha` suites. It checks
all dependency pins, uses explicit pytest plugins with autoload disabled, removes
inherited pytest/PYTHONPATH overrides, and runs collection smoke before execution.
The quality suite includes compilation, Ruff, mypy, JSON/YAML validation, branch
coverage and unit tests. The real-HA suite boots only isolated, in-process Core.
It writes JUnit/coverage evidence; it never deploys or contacts the home runtime.
External hassfest/HACS validation remains in the existing CI workflow.

The safety suite includes the once-seeded storage corruption → setup → save →
unload/setup regression and telemetry changes during notification, dispatch and
recovery persistence. Keep these public-boundary regressions when changing those
paths. Reviewers should also trace how safety evidence changes across awaits and
how a fault survives ordinary reload; passing one healthy round-trip is insufficient.

## Runtime readiness from saved diagnostics

Use a freshly acquired diagnostics response from an approved read-only session.
Save this envelope locally outside Git; preserve the actual acquisition time:

```json
{
  "checked_at_utc": "2026-10-04T12:00:00Z",
  "loaded_manifest": {"domain": "power_orchestrator", "version": "0.7.4"},
  "runtime": {"loaded": true, "data": {}}
}
```

For the Core diagnostics API, `loaded_manifest` comes from `integration_manifest`
and `runtime` from `data.runtime`. Copy the complete runtime projection. The empty
example above deliberately fails readiness; do not fill unknown fields with
assumed defaults. This envelope records process evidence, not device-registry model
text or an on-disk manifest. A registry model can remain stale after an update.

```bash
python3 scripts/runtime_evidence.py readiness /tmp/po-snapshot.json --expected-version 0.7.4
```

Readiness requires the expected loaded version, fresh timezone-aware acquisition
time (default 120 s, maximum 300 s), healthy monitoring in Observe, valid load and
safety source, explicit blocked command permissions, and no unresolved journal,
storage or device faults. Missing, malformed, contradictory, future or stale fields
block readiness. This is a snapshot check, not permission to activate Auto.

## Preserve incident evidence

Use the same saved envelope, optionally with `audit_history` from an approved
bounded diagnostic capture:

```bash
python3 scripts/runtime_evidence.py incident /tmp/po-snapshot.json > /tmp/po-incident.json
```

The export allowlists scalar runtime facts and domain reason codes, keeps at most
100 actions, and drops configuration, credentials, identities and free-form error
text. It preserves capture time, unknown load as null, record counts, truncation and
the limitation that bounded history is not continuous physical-state evidence.

For a future incident, also capture a bounded Recorder timeline and Core/Victron
logs covering the incident window, with the exact UTC start/end, returned counts
and any retention gaps. Retain the original material privately outside Git; export
only reviewed, redacted facts. If logs begin after the incident, the underlying
telemetry outage remains unexplained.

### Bounded live capture

Use `python scripts/capture_runtime.py` for an approved read-only incident capture.
It runs once over non-interactive SSH, reads process diagnostics and at most 100
persisted journal records, and requests the last 200 Core log lines. It changes no
logger or Recorder settings and writes nothing to HA. The 200-line limit is not
an incident time window: the output records acquisition times, byte/line counts
and coverage gaps, without claiming earlier logs exist.

Hard limits: 45 seconds per invocation, 1 MiB per JSON source, 128 KiB for logs,
2 MiB per capture, three retained files (6 MiB; at most 8 MiB during rotation).
An oversized or timed-out reader is killed and the entire capture is rejected.
Diagnostics/journal failure also rejects the capture. An unavailable log-reader
executable retains the diagnostics with an explicit log coverage gap.
The default private local directory is `/tmp/power-orchestrator-captures`;
directories have mode 0700 and captures 0600. Rotation touches only this tool's
own capture files. Use `--directory` outside the repository for another location.
Free-space gates require 1 GiB on HA and 256 MiB locally. No continuous collector
or debug logger is enabled, and captures remain outside HA backup storage.

The raw file contains private logs; keep it out of Git. Pass its reported path to
`runtime_evidence.py readiness` or `runtime_evidence.py incident` for validation or
an allowlisted export. Continuous Recorder inclusion of changing controller
attributes or broad service events needs a separate measured storage budget;
this capture does not enable either. Missing history remains a documented gap.

The action journal captures `decision_reason` and a bounded `input_snapshot` before
dispatch. These fields remain immutable across later phases and save/load;
`outcome_reason` records confirmation, rejection or failure separately. Existing
`reason` fields remain compatible. Old records without decision evidence remain
unknown; normalization does not reconstruct a cause.

## Find existing evidence

- [Release 0.7.4](release-0.7.4.md): imported baseline and release contracts.
- [Retrospective implementation](retro-improvements.md): this change's scope,
  local verification and remaining live boundaries.
- Repository-local historical evidence: `.scratch/power-orchestrator-reliability/`
  contains the triage spec, source correspondence, replay, implementation and
  deployment reports plus issues 01 (journal cause) and 02 (telemetry policy).
  These are historical snapshots, not current runtime state.

The component is loaded inside Home Assistant; there is no standalone server to
start. Use the real-HA fixture suites for local behavior. Release delivery uses the
existing deterministic ZIP builder and [release workflow](release.md).
