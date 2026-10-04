"""One-shot, byte/time-bounded read-only HA capture over SSH; private local storage."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import selectors
import shutil
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

MAX_BYTES = 2 * 1024 * 1024
MAX_SECONDS = 45
KEEP_FILES = 3
DEFAULT_DIRECTORY = Path("/tmp/power-orchestrator-captures")


def bounded_process(
    command: list[str], *, source: bytes = b"", limit: int = MAX_BYTES, seconds: float = MAX_SECONDS
) -> bytes:
    """Kill/reap a reader on either bound; never return a silently partial capture."""
    deadline = time.monotonic() + seconds
    with subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as process:
        try:
            assert process.stdin is not None
            offset = 0
            output = bytearray()
            errors = bytearray()
            with selectors.DefaultSelector() as selector:
                if source:
                    os.set_blocking(process.stdin.fileno(), False)
                    selector.register(process.stdin, selectors.EVENT_WRITE, None)
                else:
                    process.stdin.close()
                selector.register(process.stdout, selectors.EVENT_READ, output)
                selector.register(process.stderr, selectors.EVENT_READ, errors)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("capture exceeded time limit")
                    for key, _ in selector.select(remaining):
                        if key.data is None:
                            try:
                                offset += os.write(key.fd, source[offset : offset + 65536])
                            except BrokenPipeError as exc:
                                raise ValueError("capture reader rejected input") from exc
                            if offset == len(source):
                                selector.unregister(key.fileobj)
                                process.stdin.close()
                            continue
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        buffer = key.data
                        cap = limit if buffer is output else 8192
                        if len(buffer) + len(chunk) > cap:
                            raise ValueError("capture exceeded byte limit")
                        buffer.extend(chunk)
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
            if process.returncode != 0:
                # stderr may contain private configuration or credentials.
                raise ValueError("capture reader failed")
            return bytes(output)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def remote_capture() -> dict:
    """Run on the SSH addon: GET diagnostics, read bounded journal and recent logs."""
    import urllib.request

    config = Path("/config")
    if shutil.disk_usage(config).free < 1024**3:
        raise ValueError("HA free-space gate failed")

    def read_json(path: Path) -> dict:
        with path.open("rb") as stream:
            raw = stream.read(1024**2 + 1)
        if len(raw) > 1024**2:
            raise ValueError("source file exceeded byte limit")
        return json.loads(raw)

    started = datetime.now(UTC).isoformat()
    entries = read_json(config / ".storage/core.config_entries")["data"]["entries"]
    matches = [entry for entry in entries if entry["domain"] == "power_orchestrator"]
    if len(matches) != 1:
        raise ValueError("expected one integration entry")
    entry_id = matches[0]["entry_id"]
    token = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN")
    if not token:
        raise ValueError("Supervisor credentials unavailable")
    request = urllib.request.Request(
        "http://supervisor/core/api/diagnostics/config_entry/" + entry_id,
        headers={"Authorization": "Bearer " + token},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        raw = response.read(1024**2 + 1)
    if len(raw) > 1024**2:
        raise ValueError("diagnostics exceeded byte limit")
    diagnostics = json.loads(raw)
    snapshot = {
        "checked_at_utc": datetime.now(UTC).isoformat(),
        "loaded_manifest": diagnostics.get("integration_manifest"),
        "runtime": diagnostics.get("data", {}).get("runtime"),
    }
    persisted = read_json(config / (".storage/power_orchestrator_runtime_" + entry_id))
    rows = persisted.get("data", {}).get("audit_history", [])
    if not isinstance(rows, list):
        raise ValueError("journal is malformed")
    snapshot["audit_history"] = rows[-100:]
    try:
        logs = bounded_process(
            ["ha", "core", "logs", "--lines", "200"], limit=128 * 1024, seconds=10
        )
        snapshot["core_logs"] = logs.decode("utf-8", errors="replace")
        log_coverage = {
            "available": True,
            "returned_lines": len(logs.splitlines()),
            "returned_bytes": len(logs),
        }
    except FileNotFoundError:
        log_coverage = {"available": False, "gap": "log reader unavailable"}
    snapshot["capture"] = {
        "started_at_utc": started,
        "ended_at_utc": datetime.now(UTC).isoformat(),
        "logs": log_coverage,
        "requested_log_lines": 200,
        "received_journal_records": len(rows),
        "journal_limit": 100,
        "continuous": False,
        "complete_history": False,
    }
    return snapshot


def save_capture(raw: bytes, directory: Path) -> Path:
    """Serialize writers and rotate only this tool's private, regular JSON files."""
    if len(raw) > MAX_BYTES:
        raise ValueError("capture exceeded byte limit")
    if not isinstance(json.loads(raw), dict):
        raise ValueError("capture must be an object")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid():
        raise ValueError("capture directory must be owned and not a symlink")
    os.chmod(directory, 0o700)
    if shutil.disk_usage(directory).free < 256 * 1024**2:
        raise ValueError("local free-space gate failed")
    lock_fd = os.open(directory / ".capture.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = sorted(directory.glob("po-capture-*.json"))
        if any(
            path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid()
            for path in previous
        ):
            raise ValueError("unexpected retention file")
        name = "po-capture-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        destination = directory / (name + "-" + uuid.uuid4().hex + ".json")
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
            for path in previous[: max(0, len(previous) - KEEP_FILES + 1)]:
                path.unlink()
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="root@homeassistant.local")
    parser.add_argument("--directory", type=Path, default=DEFAULT_DIRECTORY)
    parser.add_argument("--remote", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.remote:
            raw = json.dumps(remote_capture(), allow_nan=False).encode()
            if len(raw) > MAX_BYTES:
                raise ValueError("capture exceeded byte limit")
            sys.stdout.buffer.write(raw)
            return 0
        if args.host.startswith("-") or any(char.isspace() for char in args.host):
            raise ValueError("invalid SSH destination")
        root = Path(__file__).resolve().parents[1]
        if args.directory.resolve().is_relative_to(root):
            raise ValueError("private captures must be stored outside the repository")
        raw = bounded_process(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=5",
                args.host,
                "python3",
                "-",
                "--remote",
            ],
            source=Path(__file__).read_bytes(),
        )
        destination = save_capture(raw, args.directory)
        print(
            json.dumps(
                {
                    "path": str(destination.resolve()),
                    "bytes": len(raw),
                    "retained_limit_bytes": KEEP_FILES * MAX_BYTES,
                }
            )
        )
        return 0
    except (OSError, ValueError, KeyError, TimeoutError, subprocess.TimeoutExpired) as exc:
        # Never echo exception text from a remote API or raw SSH stderr.
        print("Capture blocked: " + type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
