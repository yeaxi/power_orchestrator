#!/usr/bin/env python3
"""Build and verify the release asset that HACS downloads.

HACS extracts a ``zip_release`` asset straight into
``config/custom_components/<domain>/`` without stripping any path prefix, so the
zip must contain the bare integration files at its root. A wrapping
``custom_components/power_orchestrator/`` directory would nest the integration
inside itself on every user's install.

Run it directly to produce ``dist/power_orchestrator.zip``:

    python scripts/build_release_zip.py --expect-version 0.5.0
"""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "power_orchestrator"
INTEGRATION_DIR = REPO_ROOT / "custom_components" / DOMAIN

# Reproducible archives: a fixed DOS timestamp (the zip format epoch) and sorted
# entries make two builds of one commit byte-identical.
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
EXCLUDED_DIRECTORIES = frozenset({"__pycache__"})
EXCLUDED_SUFFIXES = frozenset({".pyc", ".pyo"})


class ReleaseAssetError(Exception):
    """Raised when the integration or the built asset violates the HACS contract."""


def hacs_manifest() -> dict:
    return json.loads((REPO_ROOT / "hacs.json").read_text())


def integration_version() -> str:
    return str(json.loads((INTEGRATION_DIR / "manifest.json").read_text())["version"])


def payload_files() -> list[Path]:
    """Return the integration files to ship, relative to the integration directory."""
    files = [
        path.relative_to(INTEGRATION_DIR)
        for path in INTEGRATION_DIR.rglob("*")
        if path.is_file()
        and path.suffix not in EXCLUDED_SUFFIXES
        and EXCLUDED_DIRECTORIES.isdisjoint(path.relative_to(INTEGRATION_DIR).parts)
    ]
    return sorted(files)


def verify_payload(files: list[Path]) -> None:
    names = {path.as_posix() for path in files}
    required = {"__init__.py", "manifest.json", "brand/icon.png"}
    if missing := required - names:
        raise ReleaseAssetError(f"Integration is missing required files: {sorted(missing)}")


def verify_archive(archive: Path, expected_filename: str) -> None:
    """Check the built asset against the way HACS installs it."""
    if archive.name != expected_filename:
        raise ReleaseAssetError(
            f"Asset is named {archive.name!r} but hacs.json requires {expected_filename!r}"
        )

    with zipfile.ZipFile(archive) as zip_file:
        if broken := zip_file.testzip():
            raise ReleaseAssetError(f"Corrupt entry in {archive.name}: {broken}")
        names = zip_file.namelist()

    if "manifest.json" not in names:
        raise ReleaseAssetError(
            "manifest.json is not at the zip root, so HACS would install a nested copy "
            f"of the integration. Entries: {sorted(names)}"
        )
    if nested := [name for name in names if name.startswith("custom_components")]:
        raise ReleaseAssetError(f"Zip wraps the integration directory: {nested}")
    if "hacs.json" in names:
        raise ReleaseAssetError("hacs.json must stay out of the asset; HACS reads it from the repo")


def build_release_zip(
    output: Path | None = None,
    *,
    expect_version: str | None = None,
) -> Path:
    """Write the release asset and return its path."""
    manifest = hacs_manifest()
    expected_filename = manifest["filename"]
    version = integration_version()

    if expect_version is not None and expect_version != version:
        raise ReleaseAssetError(
            f"Tag declares version {expect_version!r} but "
            f"custom_components/{DOMAIN}/manifest.json declares {version!r}"
        )
    if manifest.get("zip_release") is not True:
        raise ReleaseAssetError("hacs.json does not set zip_release, so no asset is expected")

    archive = output or REPO_ROOT / "dist" / expected_filename
    archive.parent.mkdir(parents=True, exist_ok=True)

    files = payload_files()
    verify_payload(files)

    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
        for relative in files:
            info = zipfile.ZipInfo(relative.as_posix(), date_time=ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zip_file.writestr(info, (INTEGRATION_DIR / relative).read_bytes())

    verify_archive(archive, expected_filename)
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Asset path. Defaults to dist/<filename from hacs.json>.",
    )
    parser.add_argument(
        "--expect-version",
        default=None,
        help="Fail unless the integration manifest declares this version.",
    )
    args = parser.parse_args()

    try:
        archive = build_release_zip(args.output, expect_version=args.expect_version)
    except ReleaseAssetError as error:
        print(f"::error::{error}")
        return 1

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    with zipfile.ZipFile(archive) as zip_file:
        count = len(zip_file.namelist())
    print(f"{archive} ({count} files, {archive.stat().st_size} bytes)")
    print(f"sha256:{digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
