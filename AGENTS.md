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

## Cursor Cloud specific instructions

This repo is a single Home Assistant custom integration (`custom_components/power_orchestrator`), not a running service. There is no long-running app to launch; "running it" means loading the integration inside a Home Assistant runtime and exercising the local gates. Follow the `## Change discipline` policy above: everything below is the local, non-live gate — it is not a live Home Assistant deployment and never touches real hardware.

**Toolchain / environments.** Requires Python 3.14 (see `pyproject.toml`, CI pins `3.14.2`), which is not the system Python. It is provided via `uv` (`uv` is on `PATH` in interactive shells). The startup update script maintains two venvs (do not merge them — see below):

- `.venv` — mocked unit/quality suite: `pytest pytest-asyncio voluptuous PyYAML coverage mypy ruff`.
- `.venv-real-ha` — real Home Assistant compat suite: `homeassistant==2026.7.4` + `pytest-homeassistant-custom-component==0.13.348`.

**Why two venvs (non-obvious):** the repo ships a fake `mocks/homeassistant` package used by the `tests/` suite (added to `pythonpath` via `pyproject.toml`). Installing the real `homeassistant` into `.venv` would collide/shadow with those mocks. Keep the real HA dependency isolated in `.venv-real-ha`.

**Gates** (commands themselves are the source of truth in `.github/workflows/ci.yml`):

- Quality + mocked tests — run with `.venv`: `compileall`, `ruff check`, `mypy custom_components/power_orchestrator`, then `coverage run --branch -m pytest tests/` (coverage gate `fail_under = 70`).
- Real HA smoke — run with `.venv-real-ha`: `python -m pytest -c pytest_real_ha.ini tests_real_ha -q`. This one needs `pytest_real_ha.ini` (its `pythonpath` is `custom_components` only, no mocks).

**End-to-end / hello-world:** to exercise the core load-shedding action in a real (ephemeral, in-process) HA runtime, boot HA via the `pytest-homeassistant-custom-component` `hass` fixture, install the integration into `hass.config.path("custom_components", ...)` (as `tests_real_ha/test_loader.py` does), add a `MockConfigEntry` with one managed load, then arm it with the `power_orchestrator.set_mode` service set to `auto`. Non-obvious: a fresh managed load that is already `on` is treated as *externally owned* (2h grace) and will NOT be shed by ordinary policy; `set_mode: auto` is what claims currently-on loads into planner ownership and flips execution to `live`. After arming, driving the aggregate load sensor to `>= 9000 W` (hard interlock) and calling `power_orchestrator.force_evaluate` produces one bounded `turn_off` with readback (status → `load_shedding`).
