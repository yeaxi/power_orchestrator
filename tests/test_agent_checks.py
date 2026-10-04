"""Regressions for wrong-baseline work and misleading runtime evidence."""

from __future__ import annotations

import copy
import json
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from preflight import inspect_source
from runtime_evidence import incident_bundle, readiness


def healthy_snapshot():
    return {
        "checked_at_utc": datetime.now(UTC).isoformat(),
        "loaded_manifest": {"domain": "power_orchestrator", "version": "0.7.4"},
        "device_registry_model": "v0.7.2",
        "runtime": {
            "loaded": True,
            "data": {
                "mode": "observe",
                "status": "observe",
                "policy_phase": "monitoring",
                "reason_code": "normal_monitoring",
                "grid_ok": True,
                "load_sensor_valid": True,
                "physical_commands_allowed": False,
                "restore_commands_allowed": False,
                "grid_safety_source_available": True,
                "safety_storage_invalid": False,
                "telemetry_fault_latched": False,
                "journal_persistence_blocked": False,
                "action_journal_invalid": False,
                "journal_unresolved_count": 0,
                "faulted_devices_count": 0,
                "quarantined_devices_count": 0,
                "safety_fault_reason": None,
            },
        },
    }


def test_stale_registry_model_does_not_reject_a_verified_process():
    assert readiness(healthy_snapshot(), "0.7.4", now=datetime.now(UTC)) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("physical_commands_allowed", True),
        ("grid_ok", None),
        ("load_sensor_valid", 1),
        ("journal_unresolved_count", False),
        ("safety_storage_invalid", True),
        ("telemetry_fault_latched", True),
        ("mode", "auto"),
    ],
)
def test_readiness_blocks_missing_malformed_or_unsafe_fields(field, value):
    snapshot = healthy_snapshot()
    snapshot["runtime"]["data"][field] = value
    assert readiness(snapshot, "0.7.4", now=datetime.now(UTC))
    del snapshot["runtime"]["data"][field]
    assert readiness(snapshot, "0.7.4", now=datetime.now(UTC))


def test_readiness_blocks_stale_future_wrong_version_and_unloaded_evidence():
    now = datetime.now(UTC)
    for age in (timedelta(seconds=121), timedelta(seconds=-1)):
        snapshot = healthy_snapshot()
        snapshot["checked_at_utc"] = (now - age).isoformat()
        assert readiness(snapshot, "0.7.4", now=now)
    snapshot = healthy_snapshot()
    assert readiness(snapshot, "0.7.3", now=now)
    snapshot["runtime"]["loaded"] = False
    assert readiness(snapshot, "0.7.4", now=now)


def test_incident_export_drops_credentials_identities_and_free_text_and_bounds_history():
    snapshot = healthy_snapshot()
    snapshot["password"] = "SYNTHETIC_SECRET"
    snapshot["runtime"]["data"]["reason_code"] = "SYNTHETIC_SECRET"
    snapshot["options"] = {"token": "SYNTHETIC_SECRET"}
    snapshot["audit_history"] = [
        {
            "action": "turn_off",
            "phase": "failed",
            "decision_reason": "grid_loss",
            "outcome_reason": "service_error",
            "reason": "SYNTHETIC_SECRET",
            "device_id": "private_identity",
            "input_snapshot": {
                "load_w": None,
                "load_valid": False,
                "entity_id": "private_identity",
                "password": "SYNTHETIC_SECRET",
                "thresholds": [{"limit_w": 6000, "duration_s": 300, "token": "SYNTHETIC_SECRET"}],
            },
        }
    ] * 101
    before = copy.deepcopy(snapshot)
    bundle = incident_bundle(snapshot)
    assert bundle["coverage"] == {
        "received_records": 101,
        "exported_records": 100,
        "truncated": True,
        "complete_history": False,
    }
    assert bundle["recent_actions"][0]["decision_reason"] == "grid_loss"
    assert bundle["recent_actions"][0]["inputs"]["load_w"] is None
    assert bundle["recent_actions"][0]["inputs"]["thresholds"] == [
        {"limit_w": 6000, "duration_s": 300}
    ]
    assert "SYNTHETIC_SECRET" not in json.dumps(bundle)
    assert "private_identity" not in json.dumps(bundle)
    assert snapshot == before


def test_preflight_blocks_divergent_history_and_version_drift_without_touching_files(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()

    git("init", "-q")
    integration = tmp_path / "custom_components/power_orchestrator"
    integration.mkdir(parents=True)
    (integration / "manifest.json").write_text('{"version":"0.7.4"}')
    project = tmp_path / "pyproject.toml"
    project.write_text('[project]\nversion="0.7.4"')
    git("add", ".")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "-qm",
        "baseline",
    )
    baseline = git("rev-parse", "HEAD")
    assert inspect_source(tmp_path, baseline)["ready_for_local_work"] is True
    project.write_text('[project]\nversion="0.5.0"')
    report = inspect_source(tmp_path, baseline)
    assert report["ready_for_local_work"] is False
    assert report["dirty_paths"]
    assert project.read_text() == '[project]\nversion="0.5.0"'
    assert git("rev-parse", "HEAD") == baseline
    git("checkout", "--orphan", "unrelated")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "-qm",
        "independent",
    )
    assert "HEAD does not descend" in inspect_source(tmp_path, baseline)["blockers"][0]
