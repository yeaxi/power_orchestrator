# Contributing

Local tests and a green CI run are not live Home Assistant evidence. Do not deploy, reload, or call a physical service from a passing test.

Full guide: [docs/development/contributing.md](docs/development/contributing.md).

Short path:

1. Use Python 3.14 (CI pins 3.14.2) and the repo `.venv`.
2. Run `python scripts/preflight.py`, then `python scripts/local_checks.py` in the pinned environment from `requirements-ci.txt`.
3. For doc changes, run `mkdocs build --strict`.
4. Open a pull request with one logical change.

Project policy for agents is in `AGENTS.md`.
