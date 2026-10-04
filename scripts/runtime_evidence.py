"""Validate or sanitize a saved diagnostics snapshot; never contact Home Assistant."""

from __future__ import annotations

import argparse
import ast
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOL_FIELDS = {
    "grid_ok",
    "load_sensor_valid",
    "startup_safe",
    "physical_commands_allowed",
    "journal_persistence_blocked",
    "action_journal_invalid",
    "telemetry_fault_latched",
    "safety_storage_invalid",
    "grid_safety_source_available",
    "restore_commands_allowed",
}
COUNT_FIELDS = {
    "journal_unresolved_count",
    "faulted_devices_count",
    "quarantined_devices_count",
    "pending_restore_count",
    "audit_history_total",
}


def timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("snapshot timestamp is missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("snapshot timestamp needs a timezone")
    return parsed.astimezone(UTC)


def readiness(snapshot: dict, expected: str, *, now: datetime, max_age: float = 120) -> list[str]:
    """Require fresh, explicit healthy Observe evidence, never registry model text."""
    blockers = []
    try:
        age = (now - timestamp(snapshot.get("checked_at_utc"))).total_seconds()
        if not 0 <= age <= max_age:
            blockers.append("snapshot is stale or in the future")
    except ValueError as exc:
        blockers.append(str(exc))
    manifest = snapshot.get("loaded_manifest")
    if not isinstance(manifest, dict) or manifest.get("domain") != "power_orchestrator":
        blockers.append("loaded process manifest is missing or belongs to another integration")
    elif manifest.get("version") != expected:
        blockers.append("loaded process version does not match the expected release")
    runtime = snapshot.get("runtime")
    if not isinstance(runtime, dict) or runtime.get("loaded") is not True:
        blockers.append("integration runtime is not explicitly loaded")
    data = runtime.get("data", {}) if isinstance(runtime, dict) else {}
    if not isinstance(data, dict):
        data = {}
    required = {
        "mode": "observe",
        "status": "observe",
        "policy_phase": "monitoring",
        "reason_code": "normal_monitoring",
        "grid_ok": True,
        "load_sensor_valid": True,
        "physical_commands_allowed": False,
        "journal_persistence_blocked": False,
        "action_journal_invalid": False,
        "journal_unresolved_count": 0,
        "faulted_devices_count": 0,
        "quarantined_devices_count": 0,
        "safety_fault_reason": None,
        "telemetry_fault_latched": False,
        "safety_storage_invalid": False,
        "grid_safety_source_available": True,
        "restore_commands_allowed": False,
    }
    for key, expected_value in required.items():
        value = data.get(key)
        if key not in data or type(value) is not type(expected_value) or value != expected_value:
            blockers.append(f"missing, invalid or unhealthy runtime field: {key}")
    return blockers


def known_codes() -> set[str]:
    """Use domain enums as the allowlist, without importing Home Assistant."""
    tree = ast.parse((ROOT / "custom_components/power_orchestrator/policy.py").read_text())
    return {
        node.value.value
        for cls in tree.body
        if isinstance(cls, ast.ClassDef) and cls.name in {"ReasonCode", "PolicyPhase"}
        for node in cls.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    } | {
        "observe",
        "auto",
        "off",
        "monitoring",
        "confirmed",
        "failed",
        "prepared",
        "dispatched",
        "rejected",
        "observe_only",
        "grid_loss",
        "turn_off",
        "turn_on",
        "emergency",
        "battery_minimum",
        "requested_stop",
        "service_error",
        "off_permission_withdrawn",
        "restore_permission_withdrawn",
    }


def incident_bundle(snapshot: dict, *, limit: int = 100) -> dict:
    """Export bounded scalar evidence; omit credentials, IDs, config and free text."""
    codes = known_codes()
    runtime = snapshot.get("runtime", {})
    data = runtime.get("data", {}) if isinstance(runtime, dict) else {}
    safe = {}
    if isinstance(data, dict):
        for key in BOOL_FIELDS:
            if type(data.get(key)) is bool:
                safe[key] = data[key]
        for key in COUNT_FIELDS:
            if type(data.get(key)) is int and data[key] >= 0:
                safe[key] = data[key]
        for key in ("mode", "status", "policy_phase", "reason_code"):
            if isinstance(data.get(key), str) and data[key] in codes:
                safe[key] = data[key]
    manifest = snapshot.get("loaded_manifest", {})
    version = manifest.get("version") if isinstance(manifest, dict) else None
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        version = None
    records = snapshot.get("audit_history", [])
    if not isinstance(records, list):
        records = []
    actions = []
    for record in records[-limit:]:
        if not isinstance(record, dict):
            continue
        action = {
            key: record[key]
            for key in ("action", "phase", "result", "decision_reason", "outcome_reason")
            if isinstance(record.get(key), str) and record[key] in codes
        }
        at = record.get("timestamp")
        if type(at) in (int, float) and math.isfinite(at):
            action["timestamp"] = at
        evidence = record.get("input_snapshot")
        if isinstance(evidence, dict):
            # Context already has no identities; project numeric facts again at export.
            action["inputs"] = {
                key: value
                for key, value in evidence.items()
                if key
                in {
                    "captured_at",
                    "load_w",
                    "load_valid",
                    "load_age_s",
                    "load_max_age_s",
                    "safety_available",
                    "safety_ok",
                    "battery_threshold",
                    "battery_charge",
                    "battery_min_soc",
                }
                and (
                    value is None
                    or type(value) is bool
                    or (type(value) in (int, float) and math.isfinite(value))
                )
            }
            thresholds = evidence.get("thresholds")
            if isinstance(thresholds, list):
                action["inputs"]["thresholds"] = [
                    {key: tier[key] for key in ("limit_w", "duration_s")}
                    for tier in thresholds[:64]
                    if isinstance(tier, dict)
                    and all(
                        type(tier.get(key)) in (int, float)
                        and math.isfinite(tier[key])
                        and tier[key] >= 0
                        for key in ("limit_w", "duration_s")
                    )
                ]
        actions.append(action)
    try:
        checked_at = timestamp(snapshot.get("checked_at_utc")).isoformat()
    except ValueError:
        checked_at = None
    return {
        "schema_version": 1,
        "checked_at_utc": checked_at,
        "loaded_version": version,
        "runtime": safe,
        "recent_actions": actions,
        "coverage": {
            "received_records": len(records),
            "exported_records": len(actions),
            "truncated": len(records) > limit,
            "complete_history": False,
        },
        "uncertainty": "saved snapshot only; no live access or continuous physical-state proof",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("readiness", "incident"))
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--expected-version")
    parser.add_argument("--max-age", type=float, default=120)
    args = parser.parse_args()
    if args.operation == "readiness" and not args.expected_version:
        parser.error("readiness requires --expected-version")
    if not math.isfinite(args.max_age) or not 0 < args.max_age <= 300:
        parser.error("--max-age must be within 0–300 seconds")
    try:
        snapshot = json.loads(args.snapshot.read_text())
        if not isinstance(snapshot, dict):
            raise ValueError("snapshot must be an object")
        if args.operation == "incident":
            print(json.dumps(incident_bundle(snapshot), indent=2, allow_nan=False))
            return 0
        blockers = readiness(
            snapshot, args.expected_version, now=datetime.now(UTC), max_age=args.max_age
        )
        print(json.dumps({"ready": not blockers, "blockers": blockers}, indent=2))
        return 1 if blockers else 0
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Evidence blocked: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
