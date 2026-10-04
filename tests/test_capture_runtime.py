"""Capture bounds must hold even when a reader is noisy, stuck or unsuccessful."""

import json
import io
import os
import subprocess
import sys

import pytest

from capture_runtime import MAX_BYTES, bounded_process, save_capture
import capture_runtime


def test_reader_byte_limit_and_stderr_failure_do_not_return_partial_secrets():
    with pytest.raises(ValueError, match="byte limit"):
        bounded_process([sys.executable, "-c", "print('x' * 65536)"], limit=100)
    with pytest.raises(ValueError, match="reader failed") as error:
        bounded_process(
            [sys.executable, "-c", "import sys; sys.stderr.write('private-token'); sys.exit(1)"]
        )
    assert "private-token" not in str(error.value)


def test_stuck_reader_is_killed_and_reaped():
    with pytest.raises((TimeoutError, subprocess.TimeoutExpired)):
        bounded_process([sys.executable, "-c", "import time; time.sleep(30)"], seconds=0.05)


def test_input_backpressure_is_included_in_deadline():
    with pytest.raises((TimeoutError, subprocess.TimeoutExpired)):
        bounded_process(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            source=b"x" * 1024**2,
            seconds=0.05,
        )


def test_nonblocking_input_is_fully_delivered():
    raw = bounded_process(
        [sys.executable, "-c", "import sys; print(len(sys.stdin.buffer.read()))"],
        source=b"x" * 1024**2,
    )
    assert raw.strip() == b"1048576"


def test_rotation_keeps_three_private_files_and_preserves_unrelated_files(tmp_path):
    unrelated = tmp_path / "notes.json"
    unrelated.write_text("preserve")
    for count in range(6):
        saved = save_capture(json.dumps({"count": count}).encode(), tmp_path)
    files = sorted(tmp_path.glob("po-capture-*.json"))
    assert len(files) == 3
    assert [json.loads(path.read_text())["count"] for path in files] == [3, 4, 5]
    assert os.stat(saved).st_mode & 0o777 == 0o600
    assert os.stat(tmp_path).st_mode & 0o777 == 0o700
    assert unrelated.read_text() == "preserve"


def test_oversize_or_symlink_capture_does_not_delete_previous_evidence(tmp_path):
    old = save_capture(b'{"previous":true}', tmp_path)
    with pytest.raises(ValueError, match="byte limit"):
        save_capture(b"x" * (MAX_BYTES + 1), tmp_path)
    link = tmp_path / "po-capture-danger.json"
    link.symlink_to(old)
    with pytest.raises(ValueError, match="retention file"):
        save_capture(b"{}", tmp_path)
    assert old.exists()


@pytest.mark.parametrize("failure", [ValueError("byte limit"), TimeoutError("time limit")])
def test_remote_log_bound_rejects_entire_capture(monkeypatch, tmp_path, failure):
    import urllib.request
    from pathlib import Path
    from types import SimpleNamespace

    storage = tmp_path / ".storage"
    storage.mkdir()
    (storage / "core.config_entries").write_text(
        json.dumps({"data": {"entries": [{"domain": "power_orchestrator", "entry_id": "fixture"}]}})
    )
    (storage / "power_orchestrator_runtime_fixture").write_text('{"data":{"audit_history":[]}}')
    monkeypatch.setattr(
        capture_runtime, "Path", lambda value: tmp_path if value == "/config" else Path(value)
    )
    monkeypatch.setattr(
        capture_runtime.shutil, "disk_usage", lambda path: SimpleNamespace(free=2 * 1024**3)
    )
    monkeypatch.setenv("SUPERVISOR_TOKEN", "fixture")
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(b'{"data":{"runtime":{}}}')
    )

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(capture_runtime, "bounded_process", fail)
    with pytest.raises(type(failure)):
        capture_runtime.remote_capture()
