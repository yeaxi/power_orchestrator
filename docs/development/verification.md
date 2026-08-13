# Power Orchestrator — controlled HA verification procedure

This document describes a future controlled verification of the integration. It is not permission to deploy and does not perform live actions.

> **Safety boundary:** without separate explicit approval, do not deploy, reload, restart, write live config or storage, or issue physical `turn_off` / `turn_on` service calls. Local tests use mocks or an in-process Home Assistant runtime.

## 1. Goal and success criteria

Verify that the integration works only as a load-shedding controller with automatic restore:

- configuration accepts aggregate load, safety source, optional loads, and explicit thresholds;
- one mode select (`off` / `observe` / `auto`) has clear boundaries;
- Observe performs zero physical actions, including on grid loss;
- a held threshold performs a bounded stop with readback;
- grid or battery loss in Auto performs the all-stop path and queues pending restore;
- invalid aggregate or unavailable safety input creates a persistent notification and never calls device services;
- after reload, valid persisted mode and the pending-restore queue return;
- there is no PV or forecast admission and no normal enable of never-shed loads;
- automatic restore re-enables pending loads one by one after a 60-second safe-capacity window, in reverse shed order, with causal ON readback.

There is **no normal automatic enabling** of never-shed loads. The physical action surface is stop plus automatic restore of the pending queue.

Success means every required check below passes, every allowed physical action has the expected readback, and no unsafe or undefined input leads to a normal action.

## 2. Prerequisites and approval gates

### Required before a live session

- [ ] Explicit approval exists for a separate live UI verification session.
- [ ] Only non-critical or test loads are selected, or a safe window is prepared.
- [ ] Entity IDs, automations, and prior configuration are recorded.
- [ ] An operator can physically turn loads off by hand.
- [ ] A rollback procedure exists.
- [ ] Test sensors or helpers are available for safety cases.

### Forbidden without separate approval

- [ ] `ha core restart`, reload integration, or reload config entry.
- [ ] Change of live dashboard, battery sensors, or physical loads.
- [ ] Manual edits to `.storage` while Home Assistant is running.
- [ ] Removal of old automations or packages before observation ends.

## 3. Installation/package check

Run only after approval for installation. This does not replace local tests.

1. Check manifest, version, domain, and package layout.
2. Install the integration without enabling physical control.
3. Confirm the package contains no credentials, tokens, or connection strings.
4. Leave mode in `observe`.

**Expectation:** Home Assistant or HACS accepts the package, the config flow is available, and no physical service calls run during installation.

## 4. Config flow walkthrough

Check that:

- Energy Dashboard discovery is optional and is not a safety permit;
- aggregate load sensor is required;
- safety source can be a grid sensor or a battery threshold;
- custom load has a controllable entity, expected power, optional power sensor, and actuator group;
- priority and pause fields have inline descriptions;
- thresholds are an explicit increasing list with dwell times;
- PV, forecast, generation, and deleted limit or restore-control fields are absent;
- duplicate or invalid devices and a missing safety source are rejected.

**Expectation:** the config flow does not change the state of any load.

## 5. Entity and device verification

After creating the entry, confirm one device **Power Orchestrator** and config-entry-scoped unique IDs for:

- status, including `pending_restore_ids` and `pending_restore_names`;
- current and average load;
- available capacity;
- last action and last operation;
- reason code;
- Grid OK;
- Faulted;
- Action journal healthy;
- mode select with options `auto`, `observe`, `off`.

Confirm a new entry starts in `observe`, and only a valid persisted mode can restore `auto` after reload or restart.

## 6. Safe runtime checks

Start with mocks, helpers, or a non-critical test load. After each step, check status, reason code, last action, pending restore attributes, and the real actuator state.

| Input/event | Expected result | Forbidden result |
|---|---|---|
| Available valid grid `on`, valid load | Safe evaluation without physical action | Normal automatic enabling of a never-shed load |
| Load `unknown`, unavailable, NaN, negative, wrong unit | `safety_blocked`; persistent notification; sample does not become `0 W` | Allowed device service call |
| Unavailable safety source | `safety_blocked`; persistent notification | Treated as confirmed grid loss |
| Grid `off` in Auto | Emergency stop path; pending queue updated | Leave a known-on load without a stop attempt |
| Battery SoC at or below threshold, or unavailable | Grid-loss or safety-blocked behavior | Allowed normal action |
| Valid load above a zero-dwell threshold in Auto | One lowest-priority known-on load shed | Batch shedding |
| Valid load above a non-zero dwell before it matures | No shed yet | Premature shed |
| Safe capacity for 60 s with pending loads in Auto | One restore per cycle in reverse shed order | Immediate restore or out-of-order restore |
| Manual ON of a pending load under safe capacity | Accepted; removed from queue | Left pending without notice |
| Manual ON of a pending load under enforced overload | Re-shed; remains queued | Accepted while overload is enforced |
| Mode `observe` during overload or grid loss | Recorded intent only; zero physical actions | Any device service call |
| Mode `off` | No ordinary physical action | Mode bypass |
| Service error or readback failure | Unknown, faulted, or safety-blocked state | Claim success without readback |

A normal stop is at most one per evaluation cycle. Emergency all-stop is a separate allowed exception in Auto.

## 7. Pause, restart, and options lifecycle

In a controlled or test environment:

1. Create an overload and confirm a bounded stop.
2. Confirm the pending-restore queue and Status attributes.
3. Set mode `auto` and confirm storage wrote it.
4. Run a separately approved reload or restart test.
5. Confirm mode `auto` and the pending queue returned.
6. Confirm missing, corrupt, or invalid persisted data yields safe `observe` or `off`.
7. Confirm Options or Reconfigure and a guarded reload.

Do not edit Home Assistant storage by hand while Home Assistant is running.

## 8. Services and manual override

After separate service-test approval:

- check `set_mode` for `auto`, `observe`, and `off`;
- reject missing or invalid mode before the handler runs;
- call `force_evaluate` and check entity update;
- call `request_stop` for a known device and confirm it joins the pending queue;
- confirm readback failure leaves the device unknown or faulted;
- check `clear_quarantine` only after independent verified OFF, load, and readback evidence;
- confirm there is no service that starts a never-shed load;
- after unload, confirm integration services are removed from the registry.

## 9. Rollback

Stop the check and roll back if:

- unknown or unavailable input leads to an ordinary physical action;
- readback does not match the command but the integration reports success;
- more than one ordinary action happens per cycle;
- `off` or `observe` is bypassed;
- emergency stop in Auto does not attempt stops for known-on loads;
- listeners or services remain after unload.

Rollback sequence only with approval:

1. Set mode `off`.
2. Stop the config entry and independently check physical loads.
3. Restore the previous known package or config path.
4. Reload or restart only under an approved operational procedure.
5. Record status, reason, action, entity ID, and timestamp without credentials.

## 10. Evidence record

For each approved session, record:

- Home Assistant and integration version;
- config-entry ID;
- load and safety source entity IDs;
- configured loads, names, priorities, expected powers, and thresholds;
- entity unique IDs;
- test case, timestamp, expected and observed result;
- whether a physical action occurred;
- rollback decision and approval.

Never include passwords, API keys, tokens, cookies, connection strings, or private credentials.

## 11. Current local verification status

The local non-live gate must cover:

- full unit regression suite;
- real in-process Home Assistant behavior and loader suite;
- Python compilation;
- JSON resource validation;
- YAML parsing for `services.yaml` and CI workflow;
- config and options flow;
- safety, availability, readback, mode persistence, pending-restore persistence, and service lifecycle;
- static scan that forbids PV, forecast, admission, and normal-enable surfaces.

This document describes future verification and is **not a substitute for the controlled live HA verification**.
