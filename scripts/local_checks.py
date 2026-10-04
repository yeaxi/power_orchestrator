"""Run the same pinned, non-live quality and real-HA gates locally and in CI."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTEST = [
    "-p",
    "pytest_asyncio.plugin",
    "-p",
    "pytest_homeassistant_custom_component.plugins",
]


def dependency_errors(root: Path) -> list[str]:
    errors = []
    for line in (root / "requirements-ci.txt").read_text().splitlines():
        pin = line.split("#", 1)[0].strip()
        if not pin:
            continue
        name, expected = pin.split("==", 1)
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            actual = "missing"
        if actual != expected:
            errors.append(f"{name}: expected {expected}, found {actual}")
    if sys.version_info[:3] < (3, 14, 2):
        errors.append("Python >=3.14.2 is required")
    return errors


def run(*args: str) -> None:
    print("+ python -m " + " ".join(args), flush=True)
    environment = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    # A caller's alternate PYTHONPATH/plugin list must not change the gate.
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTEST_ADDOPTS", None)
    environment.pop("PYTEST_PLUGINS", None)
    subprocess.run([sys.executable, "-m", *args], cwd=ROOT, env=environment, check=True)


def validate_resources() -> None:
    import yaml

    for path in [ROOT / "hacs.json", *sorted((ROOT / "custom_components").rglob("*.json"))]:
        json.loads(path.read_text())
    paths = {
        *ROOT.glob("custom_components/**/*.yaml"),
        *ROOT.glob(".github/**/*.yml"),
        ROOT / "mkdocs.yml",
    }
    for path in sorted(paths):
        yaml.safe_load(path.read_text())
    print("JSON and YAML resources passed", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("all", "quality", "real-ha", "safety"), default="all")
    args = parser.parse_args()
    errors = dependency_errors(ROOT)
    if errors:
        parser.exit(1, "Pinned environment required:\n" + "\n".join(errors) + "\n")
    try:
        if args.suite in {"all", "quality"}:
            run("compileall", "-q", "custom_components", "scripts", "tests", "tests_real_ha")
            run("ruff", "check", "custom_components", "scripts", "tests", "tests_real_ha")
            run("mypy", "custom_components/power_orchestrator")
            validate_resources()
            run("pytest", *PYTEST, "tests/", "--collect-only", "-qq")
            run(
                "coverage",
                "run",
                "--branch",
                "-m",
                "pytest",
                *PYTEST,
                "tests/",
                "-q",
                "--junitxml=junit-unit.xml",
            )
            run("coverage", "report")
            run("coverage", "xml")
        if args.suite in {"all", "real-ha", "safety"}:
            targets = (
                ["tests_real_ha"]
                if args.suite != "safety"
                else [
                    "tests_real_ha/test_telemetry_recovery.py",
                    "tests_real_ha/test_behavior.py::test_malformed_restore_storage_blocks_auto_after_setup_and_reload",
                    "tests_real_ha/test_behavior.py::test_emergency_journal_retains_cause_before_dispatch_and_after_reload",
                    "tests_real_ha/test_behavior.py::test_failed_emergency_keeps_cause_and_records_independent_outcome",
                ]
            )
            run("pytest", *PYTEST, "-c", "pytest_real_ha.ini", *targets, "--collect-only", "-qq")
            run(
                "pytest",
                *PYTEST,
                "-c",
                "pytest_real_ha.ini",
                *targets,
                "-q",
                "--junitxml=junit-real-ha.xml",
            )
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        print(f"Local gate failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
