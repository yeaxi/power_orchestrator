"""Read-only source baseline checks before editing or running local gates."""

from __future__ import annotations

import argparse
import json
import subprocess
import tomllib
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "custom_components/power_orchestrator/manifest.json"


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).rstrip("\n")


def inspect_source(root: Path, baseline: str) -> dict:
    """Check local lineage and packaging; dirty paths are reported, never changed."""
    commit = git(root, "rev-parse", "HEAD")
    reference = git(root, "rev-parse", "--verify", f"{baseline}^{{commit}}")
    version = json.loads((root / MANIFEST).read_text())["version"]
    project_version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    baseline_version = json.loads(git(root, "show", f"{reference}:{MANIFEST}"))["version"]
    descendant = (
        subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", reference, commit], check=False
        ).returncode
        == 0
    )
    blockers = []
    if not descendant:
        blockers.append("HEAD does not descend from the selected baseline")
    if version != project_version:
        blockers.append("manifest and pyproject versions disagree")
    changes = git(root, "status", "--porcelain=v1")
    return {
        "checked_at_utc": datetime.now(UTC).isoformat(),
        "workspace": str(root),
        "branch": git(root, "branch", "--show-current") or "detached",
        "commit": commit,
        "baseline_ref": baseline,
        "baseline_commit": reference,
        "manifest_version": version,
        "project_version": project_version,
        "baseline_version": baseline_version,
        "dirty_paths": changes.splitlines() if changes else [],
        "blockers": blockers,
        "ready_for_local_work": not blockers,
        "runtime_correspondence": "unverified; local Git refs are not live evidence",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="origin/main", help="Local commit/ref; never fetched")
    args = parser.parse_args()
    try:
        report = inspect_source(ROOT, args.baseline)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Preflight blocked: {exc}\n")
    print(json.dumps(report, indent=2))
    return 0 if report["ready_for_local_work"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
