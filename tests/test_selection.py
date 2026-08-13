"""Unit tests for shed/restore candidate selection."""
from __future__ import annotations

import time
from types import SimpleNamespace

from power_orchestrator.power_model import ManagedDevice, PowerModel
from power_orchestrator.selection import (
    restore_candidates,
    shed_candidates,
    shed_rejection_summary,
)


class _Hass:
    def __init__(self, mapping):
        self.states = SimpleNamespace(get=lambda entity_id: mapping.get(entity_id))


def _state(value):
    return SimpleNamespace(state=value, attributes={}, last_reported=None)


def _model(*devices: ManagedDevice) -> PowerModel:
    model = PowerModel()
    for device in devices:
        model.add_device(device)
    return model


def test_shed_candidates_selects_already_on_load() -> None:
    device = ManagedDevice("d1", "D1", "switch.d1", priority=1)
    device.is_on = True
    candidates, rejections = shed_candidates(_model(device), set(), now=time.time())
    assert [d.device_id for d in candidates] == ["d1"]
    assert rejections.counts == {}


def test_shed_candidates_reports_rejections() -> None:
    off = ManagedDevice("d1", "D1", "switch.d1")
    off.is_on = False
    quarantined_on = ManagedDevice("d2", "D2", "switch.d2", priority=2)
    quarantined_on.is_on = True
    candidates, rejections = shed_candidates(_model(off, quarantined_on), {"d2"}, now=time.time())
    assert candidates == []
    assert rejections.counts == {"off": 1, "quarantined": 1}
    assert rejections.total == 2


def test_shed_rejection_summary() -> None:
    assert shed_rejection_summary({}) == "no configured devices"
    assert shed_rejection_summary({"off": 2, "quarantined": 1}) == "off=2, quarantined=1"


def test_restore_candidates_eligibility() -> None:
    device = ManagedDevice("d1", "Boiler", "switch.d1", expected_power=500, restore_enabled=True)
    hass = _Hass({"switch.d1": _state("off")})
    common = dict(
        planner_shed=["d1"],
        faulted=set(),
        quarantined=set(),
        cooldown_until={},
        restore_threshold_w=4000,
        safety_reserve=200,
        current_load=3000,
        now=time.time(),
    )
    assert [d.device_id for d in restore_candidates(hass, _model(device), **common)] == ["d1"]

    # Not planner-shed -> not eligible.
    assert restore_candidates(hass, _model(device), **{**common, "planner_shed": []}) == []
    # Insufficient headroom (3000 + 500 + 200 > threshold 3500) -> not eligible.
    assert restore_candidates(hass, _model(device), **{**common, "restore_threshold_w": 3500}) == []
    # Cooldown active -> not eligible.
    future = {"d1": time.time() + 600}
    assert restore_candidates(hass, _model(device), **{**common, "cooldown_until": future}) == []
    # Not confirmed OFF -> not eligible.
    on_hass = _Hass({"switch.d1": _state("on")})
    assert restore_candidates(on_hass, _model(device), **common) == []


def test_restore_candidates_excludes_climate() -> None:
    device = ManagedDevice(
        "d1", "HVAC", "switch.d1", expected_power=500, restore_enabled=True,
        actuator_entity_ids=("climate.d1",),
    )
    hass = _Hass({"switch.d1": _state("off"), "climate.d1": _state("off")})
    result = restore_candidates(
        hass, _model(device), planner_shed=["d1"], faulted=set(), quarantined=set(),
        cooldown_until={}, restore_threshold_w=9000, safety_reserve=200,
        current_load=1000, now=time.time(),
    )
    assert result == []


def test_restore_candidates_reverse_actual_shed_order() -> None:
    first = ManagedDevice(
        "d1", "First", "switch.d1", expected_power=500, restore_enabled=True
    )
    second = ManagedDevice(
        "d2", "Second", "switch.d2", expected_power=500, restore_enabled=True
    )
    hass = _Hass({"switch.d1": _state("off"), "switch.d2": _state("off")})

    result = restore_candidates(
        hass,
        _model(first, second),
        planner_shed=["d1", "d2"],
        faulted=set(),
        quarantined=set(),
        cooldown_until={},
        restore_threshold_w=9000,
        safety_reserve=200,
        current_load=1000,
        now=time.time(),
    )

    assert [device.device_id for device in result] == ["d2", "d1"]
