"""Contract tests for the HACS release asset."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from build_release_zip import (
    ReleaseAssetError,
    build_release_zip,
    integration_version,
    payload_files,
)

ROOT = Path(__file__).parents[1]


def _asset_name() -> str:
    return str(json.loads((ROOT / "hacs.json").read_text())["filename"])


def test_asset_holds_the_integration_files_at_its_root(tmp_path: Path) -> None:
    """HACS extracts the asset into the integration directory without stripping paths."""
    archive = build_release_zip(tmp_path / _asset_name())

    with zipfile.ZipFile(archive) as zip_file:
        names = set(zip_file.namelist())
        manifest = json.loads(zip_file.read("manifest.json"))

    assert "__init__.py" in names
    assert "translations/en.json" in names
    assert "brand/icon.png" in names
    assert manifest["version"] == integration_version()
    assert not [name for name in names if name.startswith("custom_components")]
    assert "hacs.json" not in names


def test_asset_excludes_caches_and_bytecode(tmp_path: Path) -> None:
    stale_cache = ROOT / "custom_components" / "power_orchestrator" / "__pycache__"
    stale_cache.mkdir(exist_ok=True)
    (stale_cache / "const.cpython-314.pyc").write_bytes(b"stale")

    archive = build_release_zip(tmp_path / _asset_name())

    with zipfile.ZipFile(archive) as zip_file:
        names = zip_file.namelist()

    assert not [name for name in names if "__pycache__" in name or name.endswith(".pyc")]


def test_builds_are_reproducible(tmp_path: Path) -> None:
    first = build_release_zip(tmp_path / "first" / _asset_name())
    second = build_release_zip(tmp_path / "second" / _asset_name())

    assert first.read_bytes() == second.read_bytes()


def test_a_tag_that_disagrees_with_the_manifest_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ReleaseAssetError, match="manifest.json declares"):
        build_release_zip(tmp_path / _asset_name(), expect_version="99.0.0")


def test_the_declared_version_is_accepted(tmp_path: Path) -> None:
    archive = build_release_zip(tmp_path / _asset_name(), expect_version=integration_version())

    assert archive.is_file()


def test_a_wrong_asset_name_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ReleaseAssetError, match="hacs.json requires"):
        build_release_zip(tmp_path / "power-orchestrator.zip")


def test_every_shipped_file_is_tracked_by_git() -> None:
    """The asset must not carry local scratch files into a user's install."""
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files", "custom_components/power_orchestrator"],
        capture_output=True,
        check=True,
        cwd=ROOT,
        text=True,
    ).stdout.split()
    tracked_relative = {
        Path(path).relative_to("custom_components/power_orchestrator").as_posix()
        for path in tracked
    }

    assert {path.as_posix() for path in payload_files()} <= tracked_relative
