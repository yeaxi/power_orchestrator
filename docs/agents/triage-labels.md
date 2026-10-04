# Triage labels

Map canonical triage roles to local issue `Status:` values.

| Canonical role | Local status | Meaning |
| --- | --- | --- |
| `needs-triage` | `needs-triage` | Maintainer needs to evaluate the issue |
| `needs-info` | `needs-info` | Waiting on the reporter for more information |
| `ready-for-agent` | `ready-for-agent` | Fully specified, ready for an agent |
| `ready-for-human` | `ready-for-human` | Requires human implementation |
| `wontfix` | `wontfix` | Will not be actioned |

When a skill applies a triage role, update the issue's `Status:` line using this mapping. Edit the local-status column to change the vocabulary.

Wayfinding tickets use the separate lifecycle documented in `docs/agents/issue-tracker.md`.
