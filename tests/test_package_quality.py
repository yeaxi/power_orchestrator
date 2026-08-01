"""Package metadata and resource quality gates."""
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).parents[1]
INTEGRATION = ROOT / "custom_components" / "power_orchestrator"


def test_hacs_and_manifest_metadata_are_release_ready():
    manifest = json.loads((INTEGRATION / "manifest.json").read_text())
    hacs = json.loads((ROOT / "hacs.json").read_text())

    assert manifest["version"] == "0.5.0"
    assert manifest["single_config_entry"] is True
    assert manifest.get("dependencies", []) == []
    assert manifest["codeowners"]
    assert manifest["documentation"].startswith("https://github.com/")
    assert manifest["issue_tracker"].endswith("/issues")
    assert hacs["homeassistant"] == "2026.7.4"
    assert hacs["render_readme"] is True
    assert hacs["zip_release"] is True
    assert set(hacs["domains"]) == {"sensor", "binary_sensor", "select"}
    assert (ROOT / "brand" / "icon.png").is_file()
    assert "MIT License" in (ROOT / "LICENSE").read_text()


def test_pyproject_and_ci_define_the_local_quality_gate():
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert 'name = "power-orchestrator"' in pyproject
    assert 'version = "0.5.0"' in pyproject
    assert 'testpaths = ["tests"]' in pyproject

    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "pytest" in workflow
    assert "compileall" in workflow
    assert "json.tool" in workflow
    assert "PyYAML" in workflow
    assert "yaml.safe_load" in workflow
    assert "ruff check" in workflow
    assert "mypy" in workflow
    assert "coverage run" in workflow
    assert "coverage report" in workflow
    assert 'python-version: "3.14.2"' in workflow
    assert "homeassistant==2026.7.4" in workflow
    assert "pytest-homeassistant-custom-component==0.13.348" in workflow
    assert "pytest_real_ha.ini" in workflow
    assert "tests_real_ha" in workflow


def test_controlled_ha_verification_procedure_is_present():
    procedure = (ROOT / "HA_VERIFICATION.md").read_text()
    for heading in (
        "## 3. Installation/package check",
        "## 4. Config flow walkthrough",
        "## 6. Safe runtime checks",
        "## 9. Rollback",
    ):
        assert heading in procedure
    assert "service calls" in procedure
    assert "turn_on" in procedure
    assert "not a substitute for the controlled live HA verification" in procedure



def test_translation_resources_have_matching_config_and_options_shapes():
    strings = json.loads((INTEGRATION / "strings.json").read_text())
    english = json.loads((INTEGRATION / "translations" / "en.json").read_text())
    ukrainian = json.loads((INTEGRATION / "translations" / "uk.json").read_text())

    assert english["config"] == strings["config"]
    assert english["options"] == strings["options"]
    assert set(strings["config"]["error"]) >= {
        "invalid_priority_order",
        "invalid_discovered_devices",
        "missing_grid_loss_sensor",
        "missing_battery_soc_sensor",
    }
    assert "init" in strings["options"]["step"]
    assert "battery_soc" in strings["options"]["step"]["init"]["data"]
    assert "uk" not in ukrainian
    assert "config" in ukrainian and "options" in ukrainian


def test_quality_scale_is_declared_and_complete():
    """Track the full official IQS rule set for the Platinum target."""
    import yaml

    manifest = json.loads((INTEGRATION / "manifest.json").read_text())
    quality = yaml.safe_load((INTEGRATION / "quality_scale.yaml").read_text())
    rules = quality["rules"]
    expected = {
        "action-setup",
        "action-exceptions",
        "appropriate-polling",
        "async-dependency",
        "brands",
        "common-modules",
        "config-entry-unloading",
        "config-flow-test-coverage",
        "config-flow",
        "dependency-transparency",
        "devices",
        "diagnostics",
        "discovery-update-info",
        "discovery",
        "docs-actions",
        "docs-configuration-parameters",
        "docs-data-update",
        "docs-examples",
        "docs-high-level-description",
        "docs-installation-instructions",
        "docs-installation-parameters",
        "docs-known-limitations",
        "docs-removal-instructions",
        "docs-supported-devices",
        "docs-supported-functions",
        "docs-troubleshooting",
        "docs-use-cases",
        "docs-conditions",
        "docs-triggers",
        "dynamic-devices",
        "entity-category",
        "entity-device-class",
        "entity-disabled-by-default",
        "entity-event-setup",
        "entity-translations",
        "entity-unique-id",
        "entity-unavailable",
        "exception-translations",
        "has-entity-name",
        "icon-translations",
        "inject-websession",
        "integration-owner",
        "log-when-unavailable",
        "parallel-updates",
        "reauthentication-flow",
        "reconfiguration-flow",
        "repair-issues",
        "runtime-data",
        "stale-devices",
        "strict-typing",
        "test-before-configure",
        "test-before-setup",
        "test-coverage",
        "unique-config-entry",
    }
    assert manifest["quality_scale"] == "platinum"
    assert set(rules) == expected
    assert all(
        value == "done"
        or (isinstance(value, dict) and value.get("status") in {"done", "exempt"})
        for value in rules.values()
    )


def test_platinum_runtime_resources_are_present():
    """Require the HA-facing Platinum contract files and resource sections."""
    assert (INTEGRATION / "diagnostics.py").is_file()
    icons = json.loads((INTEGRATION / "icons.json").read_text())
    assert icons["entity"]["sensor"]["last_operation"]["default"]
    assert icons["entity"]["binary_sensor"]["action_journal_healthy"]["default"]

    strings = json.loads((INTEGRATION / "strings.json").read_text())
    assert strings["exceptions"]
    assert "reconfigure" in strings["config"]["step"]
    assert "issues" in strings
    assert "services" in strings

    readme = (ROOT / "README.md").read_text()
    for heading in (
        "## Use cases",
        "## Supported functionality",
        "## Data updates",
        "## Troubleshooting",
        "## Known limitations",
        "## Removal",
    ):
        assert heading in readme
