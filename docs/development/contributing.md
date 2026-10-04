# Contributing

This repository is a Home Assistant custom integration. Local tests run in-process. They never touch live hardware.

A green local run is not permission to deploy, reload, or call a physical service.

Project policy for agents lives in the repository-root `AGENTS.md`.

## Setup

Python 3.14 is required. CI pins 3.14.2. Use `uv` and the repo `.venv`.

```bash
uv venv --python 3.14.2 .venv
source .venv/bin/activate
uv pip install -r requirements-ci.txt
```

`requirements-ci.txt` pins every tool version, and CI installs the same file, so
a gate cannot pass locally and fail in CI because of a tool release. `homeassistant`
and `pytest-homeassistant-custom-component` move together; the same Home Assistant
version appears in `hacs.json` and in the hassfest image tag in
`.github/workflows/validate.yml`.

## Local gates

CI and local work use the same runner, `scripts/local_checks.py`.

```bash
python scripts/preflight.py --baseline origin/main
python scripts/local_checks.py
```

Coverage fails under 75% (`pyproject.toml`).

For focused lifecycle and asynchronous safety regressions, use
`python scripts/local_checks.py --suite safety`. Environment isolation and saved
runtime evidence are documented in [Agent workflow and evidence](agent-environment.md).

Two more gates run on GitHub and need Docker or GitHub API access, so they are not
part of the local loop: hassfest and HACS validation
(`.github/workflows/validate.yml`). Both also run on every pull request.

Build docs before you merge doc changes:

```bash
python -m pip install -r requirements-docs.txt
mkdocs build --strict
```

## Pull requests

One logical change per PR. Run the gates above. Do not commit `.venv`, caches, Home Assistant `/config` state, or credentials.

Git checkpoint and rollback steps: [Git workflow](git-workflow.md).

Live Home Assistant verification is a separate, explicitly approved procedure: [Verification](verification.md).

Publishing a version: [Release](release.md).
