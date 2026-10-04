# Release 0.7.6: architecture deepening

The four refactors use the released 0.7.5 source at
`9e9017efce84b4b1178aaccc167f2a62de77c617`. The earlier architecture report inspected
an older preserved checkout. Implementation retains the current config-entry 2.4
schema, unified modes and intent/ticket protocol. It does not add load admission.

## Module ownership

| Module | Concentrated responsibility | Caller interface |
| --- | --- | --- |
| `readback.CausalReadback` | Pre-command member snapshots, causal reports, idle climate exception and one bounded deadline | Capture before dispatch, then `await wait(command_issued_at)` |
| `runtime_recovery.RuntimeRecovery` | Ordered hydration, unresolved action quarantine, late sticky corruption and safe mode commitment | `await coordinator.async_recover_runtime(data)` |
| `device_configuration` | Identity, electrical limits and command/readback/fallback topology shared by input adapters | Strict options, tolerant persisted records, wizard and legacy model adapters |
| `restore_transactions.RestoreTransactions` | Ordered tickets and prepared → durable dispatch → atomic terminal protocol | Policy selects; physical adapter rechecks permission; synchronous completion stages ticket/pause/fence before saving |

Removing these modules would move polling, reconstruction recipes, configuration
invariants or persistence failure handling back into multiple callers. Policy,
owner intent, safety evidence and physical actuation remain separate collaborators.
Recovery does not evaluate policy or dispatch device commands.

## Findings resolved during independent review

- Cancellation after a service call could leave an unresolved action eligible for
  another attempt. Cancellation now retires ownership and quarantines before it
  propagates; reload tests cover a physically applied but interrupted service.
- Confirmed manual and battery stops saved their pause after terminal commitment.
  The terminal snapshot now includes the pause, ticket/order and applicable fence.
- A retained ticket could target a newly configured command entity. Every restore
  dispatch now requires the captured entity and domain to match the actuator.
- Emergency fallback lacked permission checks between member calls. Each member
  now checks fresh telemetry/safety permission after the preceding await.
- Earliest-member and idle-thermostat timestamps could let a partial aggregate
  load clear the next-restore fence. Confirmation uses the latest required report;
  tests block aggregate reports between member transitions and at the fence.
- Future aggregate reports and unbounded/future durable proofs were accepted.
  Aggregate age must be between zero and its existing maximum; ticket lifetime is
  at most 24 hours with nonnegative, already-past creation. Restored intent expiry
  cannot exceed the next 24 hours. Invalid persisted proofs remain storage-blocked
  across a normal snapshot save and reload.
- The transition refactor briefly changed the domain outcome `service_error` to
  free-form exception text. Independent real-HA tests caught it; the stable
  outcome code is preserved separately from bounded error detail.

Save failures at prepared or dispatched phases perform zero physical commands.
A failed terminal save after one ON retires proof and quarantines, while the last
durable dispatched record recovers as unresolved. There is no compensating
physical command, silent retry or invented ownership proof.

## Local evidence

Verification on 2026-10-04 UTC used Python 3.14.3 and the exact repository pins:
Home Assistant 2026.8.2 and pytest-homeassistant-custom-component 0.13.356.

| Check | Released baseline | Integrated change |
| --- | --- | --- |
| `python scripts/local_checks.py` unit suite | 249 tests + 19 subtests | 354 tests + 19 subtests |
| Complete isolated real-HA suite | 67 tests + 19 subtests | 67 tests + 19 subtests |
| Coverage with branches enabled | 76% | 82% (required minimum 75%) |
| Shared safety suite | Existing gate | 29 tests + 19 subtests |
| Compilation, Ruff, mypy, JSON/YAML | Passed | Passed; mypy checks 27 source files |

The coverage denominator is 4,276 statements and 1,326 branches; 626 statements
were missed and 252 partial branches remain. The transition module has 100%
coverage in this gate. Coverage is not proof of live physical behavior.

Separate agents implemented each candidate. Independent review of candidates
1–3 passed 169 focused tests plus 19 subtests; the safety review passed 177 focused
tests. Both independently passed all 67 real-HA tests plus 19 subtests. Their
findings above were resolved before the final shared gate.

An additional isolated environment matching the inspected home Core 2026.9.2,
with pytest-homeassistant-custom-component 0.13.365, passed 114 focused tests and
all 67 real-HA tests plus 19 subtests. This is compatibility evidence, not evidence
from that home.

## Release and installation boundary

Publish only through the existing reviewed-main tag pipeline. Install the
checksum-verified release ZIP with a timestamped component backup, source-file
hash correspondence, `ha core check`, required restart and fresh loaded-process
readiness evidence. Leave the installed controller in Observe.

At this document's commit, GitHub ecosystem checks, release publication and live
installation evidence are still pending. Record their actual results in the
deployment report; local checks do not satisfy those gates.

The root agent instructions retain historical `auto_stack`/per-device-opt-in
wording. Current released source and the imported installed baseline documented in
[release 0.7.4](release-0.7.4.md) use the automatic restore contract in
[the specification](../architecture/power-orchestrator-spec.md): Auto, active owner
intent, a compatible durable ticket, safe dwell and one dispatch per cycle. This
refactor narrows those existing dispatch gates. It does not reconcile that policy
terminology or authorize Auto activation; installation verification is limited to
Observe with explicitly blocked physical permissions.

Cross-device overlap among explicit command/readback/fallback roles remains an
inherited configuration limitation. Local idle-climate fixtures do not prove a
particular live thermostat's behavior. No physical Auto verification is claimed.

Storage and config-entry versions are unchanged. Rollback restores the previous
published component package from the timestamped backup while preserving Observe;
ordinary snapshot saves cannot clear a new sticky corruption/quarantine fault.
