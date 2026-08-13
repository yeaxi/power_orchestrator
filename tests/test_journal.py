"""Unit tests for audit-journal writes and event emission."""
from __future__ import annotations

from types import SimpleNamespace

from power_orchestrator.const import EVENT_SCHEMA_VERSION
from power_orchestrator.journal import emit_event, new_action_id, record_action


def test_new_action_id_is_prefixed_and_unique() -> None:
    a = new_action_id("stop")
    b = new_action_id("stop")
    assert a.startswith("stop_") and b.startswith("stop_") and a != b


def test_record_action_writes_and_normalizes() -> None:
    written: list[dict] = []
    store = SimpleNamespace(record_action=written.append)
    assert record_action(store, {"action_id": "a1", "action": "turn_off"}) is True
    assert written and written[0]["event_schema"] == EVENT_SCHEMA_VERSION
    assert "timestamp" in written[0]


def test_record_action_ignores_missing_action_id() -> None:
    written: list[dict] = []
    store = SimpleNamespace(record_action=written.append)
    assert record_action(store, {"action": "turn_off"}) is False
    assert not written


def test_record_action_without_writer_returns_false() -> None:
    assert record_action(SimpleNamespace(), {"action_id": "a1"}) is False


def test_emit_event_builds_bounded_envelope() -> None:
    fired: list[tuple[str, dict]] = []
    hass = SimpleNamespace(bus=SimpleNamespace(async_fire=lambda t, e: fired.append((t, e))))
    emit_event(
        hass,
        "power_orchestrator.action",
        {"action": "turn_off"},
        entry_id="entry-1",
        mode="auto",
    )
    assert fired
    event_type, event = fired[0]
    assert event_type == "power_orchestrator.action"
    assert event["schema_version"] == EVENT_SCHEMA_VERSION
    assert event["entry_id"] == "entry-1"
    assert "execution_mode" not in event
    assert event["mode"] == "auto"
    assert event["action"] == "turn_off"


def test_emit_event_survives_missing_bus() -> None:
    # No bus / non-callable emitter must not raise.
    emit_event(SimpleNamespace(), "x", {}, entry_id="e", mode="off")
