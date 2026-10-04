# Review standards

For policy, persistence or async lifecycle changes, review the evidence flow in
`docs/development/agent-environment.md`: trace validity across each await, distinguish
decision evidence from outcomes, and verify that blocked state survives an ordinary
save/reload. Require the relevant public-boundary regression when that behavior
changes. Deterministic syntax, dependency and packaging checks belong in the shared
local/CI runner rather than additional prose rules.
