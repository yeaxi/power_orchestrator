# Contributing

Local tests and a green CI run are not live Home Assistant evidence. Do not deploy, reload, or call a physical service from a passing test.

Full guide: [docs/development/contributing.md](docs/development/contributing.md).

Short path:

1. Use Python 3.14 (CI pins 3.14.2) and the repo `.venv`.
2. Run compile, ruff, mypy, `coverage run -m pytest tests/`, then `pytest -c pytest_real_ha.ini tests_real_ha`.
3. For doc changes, run `mkdocs build --strict`.
4. Open a pull request with one logical change.

Project policy for agents is in `AGENTS.md`.
