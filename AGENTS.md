# Power Orchestrator project context

## Architecture

Prefer reactive/declarative control and explicit state transitions over opaque imperative orchestration. Keep policy, safety gates, and physical actions separate and inspectable.

## Change discipline

- No live cutover before all local, correspondence, safety, and runtime gates pass.
- Use Git commits for accepted changes and Git revert/rollback for rejected changes; do not patch the live system as a shortcut.
- Local tests and a successful process exit are not evidence of live Home Assistant behavior.
- Physical or external side effects require explicit approval for the exact operation.

## Evidence

Record exact sources, timestamps, thresholds, denominators, coverage, and uncertainty. Treat unknown, stale, contradictory, or unverifiable state as blocked. Keep project-specific policies here rather than duplicating them in generic Hermes skills.

## Cursor Cloud specific instructions

This repo is a single Home Assistant custom integration (`custom_components/power_orchestrator`), not a running service. There is no long-running app to launch; "running it" means loading the integration inside a Home Assistant runtime and exercising the local gates. Follow the `## Change discipline` policy above: everything below is the local, non-live gate — it is not a live Home Assistant deployment and never touches real hardware.

**Toolchain / environment.** Requires Python 3.14 (see `pyproject.toml`, CI pins `3.14.2`), which is not the system Python. It is provided via `uv` (`uv` is on `PATH` in interactive shells). A single venv `.venv` holds everything: the quality tools plus real Home Assistant. There is no bespoke mock package anymore — the whole suite runs against real Home Assistant via `pytest-homeassistant-custom-component`.

- `.venv` deps: `uv pip install -r requirements-ci.txt`. That file pins every version CI uses, including `homeassistant==2026.7.4` and `pytest-homeassistant-custom-component==0.13.348`, which must be bumped as a pair together with `hacs.json` and the hassfest image tag in `.github/workflows/validate.yml`.
- The plugin auto-registers, so `hass` and `enable_custom_integrations` fixtures are available; `tests/conftest.py` enables custom integrations automatically. Unit tests that only need a lightweight `hass` still build their own `MagicMock`.

**Gates** (commands themselves are the source of truth in `.github/workflows/ci.yml`):

- Quality + tests — run with `.venv`: `compileall`, `ruff check`, `mypy custom_components/power_orchestrator`, then `coverage run --branch -m pytest tests/` (coverage gate `fail_under = 75`).
- Real HA behavior/loader suite — run with `.venv`: `python -m pytest -c pytest_real_ha.ini tests_real_ha -q` (its `pythonpath` is `custom_components`; covers loader + end-to-end shed/emergency/observe/persistence behavior).
- Ecosystem validation — `.github/workflows/validate.yml` runs hassfest (pinned Docker image) and HACS validation. Both need Docker or GitHub API access, so they run on GitHub rather than locally, on every push and pull request and weekly on a schedule.
- Delivery — `hacs.json` sets `zip_release`, so HACS installs only from a release asset named `power_orchestrator.zip`. A `v<x>.<y>.<z>` tag runs `.github/workflows/release.yml`, which re-runs both gate workflows, requires the tag to be an ancestor of `main` and to match the manifest version, then publishes the asset built by `scripts/build_release_zip.py`. See `docs/development/release.md`.

**End-to-end / hello-world:** to exercise the core load-shedding action in a real (ephemeral, in-process) HA runtime, boot HA via the `pytest-homeassistant-custom-component` `hass` fixture, install the integration into `hass.config.path("custom_components", ...)` (as `tests_real_ha/test_loader.py` does), and add a `MockConfigEntry` with one managed load and an explicit zero-dwell threshold. A new entry starts in `observe` and performs zero physical actions. Call `power_orchestrator.set_mode` with `auto`, drive the aggregate load sensor above the configured threshold, and call `power_orchestrator.force_evaluate`. That produces one bounded `turn_off` with readback (status → `load_shedding`) and queues the load for automatic restore. After the load stays below the lowest threshold for the 60-second safe-capacity window (monkeypatch `RESTORE_SAFE_CAPACITY_DWELL_S` in tests), a later evaluation restores that pending load with one bounded `turn_on`.
