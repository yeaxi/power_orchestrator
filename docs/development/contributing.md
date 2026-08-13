# Contributing

This repository is a Home Assistant custom integration. Local tests run in-process. They never touch live hardware.

A green local run is not permission to deploy, reload, or call a physical service.

Project policy for agents lives in the repository-root `AGENTS.md`.

## Setup

Python 3.14 is required. CI pins 3.14.2. Use `uv` and the repo `.venv`.

```bash
uv venv --python 3.14.2 .venv
source .venv/bin/activate
uv pip install pytest pytest-asyncio voluptuous PyYAML coverage mypy ruff \
  homeassistant==2026.7.4 pytest-homeassistant-custom-component==0.13.348
```

## Local gates

Commands in `.github/workflows/ci.yml` are the source of truth.

```bash
python -m compileall -q custom_components tests tests_real_ha
python -m ruff check custom_components tests tests_real_ha
python -m mypy custom_components/power_orchestrator
python -m coverage run --branch -m pytest tests/ -q
python -m coverage report
python -m pytest -c pytest_real_ha.ini tests_real_ha -q
```

Coverage fails under 75% (`pyproject.toml`).

Build docs before you merge doc changes:

```bash
python -m pip install mkdocs-material
mkdocs build --strict
```

## Pull requests

One logical change per PR. Run the gates above. Do not commit `.venv`, caches, Home Assistant `/config` state, or credentials.

Git checkpoint and rollback steps: [Git workflow](git-workflow.md).

Live Home Assistant verification is a separate, explicitly approved procedure: [Verification](verification.md).
