"""Package metadata and resource quality gates."""
from __future__ import annotations

import json
import tomllib
from pathlib import Path


ROOT = Path(__file__).parents[1]
INTEGRATION = ROOT / "custom_components" / "power_orchestrator"

# Keys accepted by the HACS manifest schema. HACS rejects anything else
# (voluptuous PREVENT_EXTRA), which fails the HACS validation action.
HACS_JSON_ALLOWED_KEYS = {
    "content_in_root",
    "country",
    "filename",
    "hacs",
    "hide_default_branch",
    "homeassistant",
    "name",
    "persistent_directory",
    "render_readme",
    "zip_release",
}


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


def test_hacs_and_manifest_metadata_are_release_ready():
    manifest = json.loads((INTEGRATION / "manifest.json").read_text())
    hacs = json.loads((ROOT / "hacs.json").read_text())

    assert manifest["single_config_entry"] is True
    assert manifest.get("dependencies", []) == []
    assert manifest["codeowners"]
    assert manifest["documentation"] == "https://yeaxi.github.io/power_orchestrator/"
    assert manifest["issue_tracker"].endswith("/issues")
    assert hacs["homeassistant"] == "2026.7.4"
    assert hacs["render_readme"] is True
    assert hacs["zip_release"] is True
    assert hacs["filename"] == "power_orchestrator.zip"
    assert set(hacs) <= HACS_JSON_ALLOWED_KEYS
    assert "MIT License" in (ROOT / "LICENSE").read_text()


def test_manifest_keys_are_sorted_the_way_hassfest_requires():
    """hassfest requires domain, then name, then alphabetical order."""
    keys = list(json.loads((INTEGRATION / "manifest.json").read_text()))
    assert keys == sorted(keys, key=lambda key: {"domain": ".domain", "name": ".name"}.get(key, key))


def test_brand_assets_live_where_hacs_and_home_assistant_look():
    """HACS checks the integration-local path for a non-root layout, as does HA 2026.3+."""
    assert (INTEGRATION / "brand" / "icon.png").is_file()
    assert not (ROOT / "brand").exists()


def test_declared_version_is_consistent_across_packaging_files():
    manifest_version = json.loads((INTEGRATION / "manifest.json").read_text())["version"]
    assert manifest_version == _pyproject()["project"]["version"]
    assert manifest_version.count(".") == 2
    assert all(part.isdigit() for part in manifest_version.split("."))


def test_device_info_model_matches_the_declared_version():
    """A release bump must not leave a stale version in the device registry."""
    manifest_version = json.loads((INTEGRATION / "manifest.json").read_text())["version"]
    for platform in ("binary_sensor.py", "select.py", "sensor.py"):
        source = (INTEGRATION / platform).read_text()
        for line in source.splitlines():
            if '"model":' in line:
                assert f'"v{manifest_version}"' in line, f"{platform}: {line.strip()}"


def test_pyproject_and_ci_define_the_local_quality_gate():
    pyproject = _pyproject()
    assert pyproject["project"]["name"] == "power-orchestrator"
    assert pyproject["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]
    assert pyproject["tool"]["coverage"]["report"]["fail_under"] == 75

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
    procedure = (ROOT / "docs" / "development" / "verification.md").read_text()
    for heading in (
        "## 3. Installation/package check",
        "## 4. Config flow walkthrough",
        "## 6. Safe runtime checks",
        "## 9. Rollback",
    ):
        assert heading in procedure
    assert "service calls" in procedure
    assert "turn_off" in procedure
    assert "no normal automatic enabling" in procedure.lower()
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
