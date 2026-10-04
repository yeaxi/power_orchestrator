# Issue tracker: local Markdown

Issues and specs live as Markdown files in `.scratch/`.

## Conventions

- One feature per directory: `.scratch/<feature-slug>/`.
- The spec is `.scratch/<feature-slug>/spec.md`.
- Implementation tickets are separate files at `.scratch/<feature-slug>/issues/<NN>-<slug>.md`, numbered from `01`.
- Triage state is a `Status:` line near the top of each issue file. Use the role strings in `docs/agents/triage-labels.md`.
- Append comments and conversation history under a `## Comments` heading.

## Publish to the issue tracker

Create the spec or ticket at its path above, creating directories as needed.

## Fetch the relevant ticket

Read the referenced file. Resolve an issue number within its feature directory; ask for the feature or path if the number is ambiguous.

## Wayfinding operations

For `wayfinder`, the map is a file with one child file per decision ticket.

- Map: `.scratch/<effort>/map.md`, containing Notes, Decisions-so-far, and Fog.
- Child ticket: `.scratch/<effort>/issues/<NN>-<slug>.md`, numbered from `01`, with the question in its body.
- Type: a `Type:` line containing `research`, `prototype`, `grilling`, or `task`.
- Status: wayfinding tickets use `open`, `claimed`, and `resolved` as their `Status:` values.
- Blocking: a `Blocked by: NN, NN` line near the top. A ticket is unblocked when all listed tickets in the same effort are `resolved`.
- Frontier: scan for `open`, unblocked tickets; the lowest number wins.
- Claim: save `Status: claimed` before starting work.
- Resolve: append the answer under `## Answer`, save `Status: resolved`, then add a brief summary and relative link to the map's Decisions-so-far.
