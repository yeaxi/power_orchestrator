"""Selection helper tests."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from power_orchestrator.power_model import ManagedDevice, PowerModel
from power_orchestrator.selection import restore_candidates, shed_candidates, shed_rejection_summary


def test_shed_rejection_summary_is_bounded() -> None:
    summary = shed_rejection_summary({"off": 2, "quarantined": 1})
    assert "off=2" in summary


def test_shed_candidates_skip_off_and_quarantined() -> None:
    model = PowerModel()
    on = ManagedDevice("d1", "On", "switch.d1", expected_power=1000, shed_priority=1)
    off = ManagedDevice("d2", "Off", "switch.d2", expected_power=1000, shed_priority=2)
    on.is_on = True
    off.is_on = False
    model.add_device(on)
    model.add_device(off)
    candidates, rejections = shed_candidates(model, {"d1"}, now=1.0)
    assert candidates == []
    assert rejections.counts["quarantined"] == 1


def test_restore_candidates_reverse_order_and_capacity_gate() -> None:
    hass = MagicMock()
    hass.states.get.side_effect = lambda entity_id: SimpleNamespace(state="off")
    model = PowerModel()
    first = ManagedDevice("d1", "First", "switch.d1", expected_power=500)
    second = ManagedDevice("d2", "Second", "switch.d2", expected_power=500)
    first.is_on = False
    second.is_on = False
    model.add_device(first)
    model.add_device(second)
    candidates = restore_candidates(
        hass,
        model,
        planner_shed=["d1", "d2"],
        faulted=set(),
        quarantined=set(),
        lowest_limit_w=5000,
        current_load=4400,
    )
    assert [device.device_id for device in candidates] == ["d2", "d1"]
    blocked = restore_candidates(
        hass,
        model,
        planner_shed=["d1", "d2"],
        faulted=set(),
        quarantined=set(),
        lowest_limit_w=5000,
        current_load=4600,
    )
    assert blocked == []


def test_restore_candidates_exclude_climate() -> None:
    hass = MagicMock()
    hass.states.get.side_effect = lambda entity_id: SimpleNamespace(state="off")
    model = PowerModel()
    device = ManagedDevice(
        "d1",
        "HVAC",
        "switch.d1",
        expected_power=500,
        actuator_entity_ids=("climate.hvac",),
    )
    device.is_on = False
    model.add_device(device)
    candidates = restore_candidates(
        hass,
        model,
        planner_shed=["d1"],
        faulted=set(),
        quarantined=set(),
        lowest_limit_w=9000,
        current_load=1000,
    )
    assert candidates == []
