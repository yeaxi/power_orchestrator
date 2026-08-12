"""Unit tests for the fault/quarantine registry."""
from __future__ import annotations

from power_orchestrator.fault_registry import FaultRegistry


def test_latch_marks_faulted_quarantined_and_dirty() -> None:
    registry = FaultRegistry()
    registry.latch("d1", "relay_readback_timeout")
    assert registry.faulted == {"d1"}
    assert registry.quarantined == {"d1"}
    assert registry.reasons["d1"] == "relay_readback_timeout"
    assert registry.dirty is True
    assert registry.is_flagged("d1") is True
    assert registry.is_flagged("other") is False


def test_latch_bounds_reason_length() -> None:
    registry = FaultRegistry()
    registry.latch("d1", "x" * 500)
    assert len(registry.reasons["d1"]) == 160


def test_clear_removes_all_state() -> None:
    registry = FaultRegistry()
    registry.latch("d1", "reason")
    registry.dirty = False
    registry.clear("d1")
    assert registry.faulted == set()
    assert registry.quarantined == set()
    assert "d1" not in registry.reasons
    assert registry.dirty is True
    assert registry.is_flagged("d1") is False
