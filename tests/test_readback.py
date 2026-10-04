"""Causal Logical device observations exercised through capture and wait."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from power_orchestrator.power_model import ManagedDevice
from power_orchestrator.policy import PolicyEngine, policy_for_tests
from power_orchestrator.readback import CausalReadback
from power_orchestrator import readback as readback_module


class _Hass:
    def __init__(self, mapping):
        self.mapping = mapping
        self.states = SimpleNamespace(get=lambda entity_id: self.mapping.get(entity_id))


def _state(value, last_reported, **attributes):
    return SimpleNamespace(state=value, attributes=attributes, last_reported=last_reported)


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    # Reports use finite epochs relative to a known current wall clock. The
    # timeout still uses the actual monotonic clock unless a test overrides it.
    monkeypatch.setattr(
        readback_module,
        "time",
        SimpleNamespace(time=lambda: 200.0, monotonic=time.monotonic),
    )


def _capture(*, members=(), expected="off", prior=100.0):
    device = ManagedDevice("d1", "D1", "switch.d1", readback_entity_ids=members)
    hass = _Hass({entity: _state("on", prior) for entity in device.readback_entities})
    return hass, CausalReadback(hass, device, expected)


@pytest.mark.asyncio
async def test_confirms_off_after_command_and_newer_than_captured_report():
    hass, observation = _capture()
    hass.mapping["switch.d1"] = _state("off", 200.0)
    assert await observation.wait(150.0, timeout=0) == 200.0


@pytest.mark.asyncio
async def test_missing_prior_can_confirm_report_at_command_timestamp():
    hass, observation = _capture(expected="on", prior=None)
    hass.mapping["switch.d1"] = _state("on", 150.0)
    assert await observation.wait(150.0, timeout=0) == 150.0


@pytest.mark.asyncio
@pytest.mark.parametrize("reported", [90.0, 100.0, 149.0, None, 201.0])
async def test_pre_command_missing_stale_and_future_reports_never_confirm(reported):
    hass, observation = _capture()
    hass.mapping["switch.d1"] = _state("off", reported)
    # 149 is newer than the 100 snapshot but predates command issuance: the
    # prior implementation incorrectly accepted this temporal contradiction.
    assert await observation.wait(150.0, timeout=0) is None


@pytest.mark.asyncio
async def test_report_must_be_newer_than_snapshot_even_when_after_command():
    hass, observation = _capture(prior=175.0)
    hass.mapping["switch.d1"] = _state("off", 175.0)
    assert await observation.wait(150.0, timeout=0) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["on", "unknown", "unavailable"])
async def test_fresh_report_of_wrong_or_unknown_state_does_not_confirm(value):
    hass, observation = _capture()
    hass.mapping["switch.d1"] = _state(value, 200.0)
    assert await observation.wait(150.0, timeout=0) is None


@pytest.mark.asyncio
async def test_all_group_members_require_independent_causal_reports():
    hass, observation = _capture(members=("switch.d1", "switch.d2"))
    hass.mapping["switch.d1"] = _state("off", 190.0)
    hass.mapping["switch.d2"] = _state("off", 149.0)
    assert await observation.wait(150.0, timeout=0) is None
    hass.mapping["switch.d2"] = _state("off", 180.0)
    assert await observation.wait(150.0, timeout=0) == 190.0


@pytest.mark.asyncio
async def test_group_member_cannot_reuse_its_newer_baseline_report():
    device = ManagedDevice(
        "d1", "D1", "switch.d1", readback_entity_ids=("switch.d1", "switch.d2")
    )
    hass = _Hass({"switch.d1": _state("on", 100.0), "switch.d2": _state("off", 170.0)})
    observation = CausalReadback(hass, device, "off")
    hass.mapping["switch.d1"] = _state("off", 180.0)
    assert await observation.wait(150.0, timeout=0) is None
    hass.mapping["switch.d2"] = _state("off", 190.0)
    assert await observation.wait(150.0, timeout=0) == 190.0


def _idle_capture(*, prior_climate=100.0, relay_report=100.0):
    device = ManagedDevice(
        "d1", "D1", "switch.d1", command_entity_id="climate.d1",
        readback_entity_ids=("switch.d1", "switch.d2"),
    )
    hass = _Hass({
        "climate.d1": _state("off", prior_climate),
        "switch.d1": _state("off", relay_report),
        "switch.d2": _state("off", relay_report),
    })
    return hass, CausalReadback(hass, device, "on")


@pytest.mark.asyncio
async def test_idle_climate_confirms_new_enabled_report_with_unchanged_fresh_off_relays():
    hass, observation = _idle_capture()
    hass.mapping["climate.d1"] = _state("heat", 175.0, hvac_action="idle")
    assert await observation.wait(150.0, timeout=0) == 175.0


@pytest.mark.asyncio
@pytest.mark.parametrize("reported", [149.0, 175.0, 201.0])
async def test_idle_climate_cannot_use_old_repeated_or_future_command_report(reported):
    hass, observation = _idle_capture(prior_climate=175.0)
    hass.mapping["climate.d1"] = _state("heat", reported, hvac_action="idle")
    assert await observation.wait(150.0, timeout=0) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("relay_state,reported", [
    ("off", 19.0), ("off", 201.0), ("off", None), ("unknown", 180.0), ("on", 180.0),
])
async def test_idle_climate_requires_every_relay_fresh_and_off(relay_state, reported):
    hass, observation = _idle_capture()
    hass.mapping["climate.d1"] = _state("heat", 175.0, hvac_action="idle")
    hass.mapping["switch.d2"] = _state(relay_state, reported)
    assert await observation.wait(150.0, timeout=0) is None


@pytest.mark.asyncio
async def test_heating_climate_with_causal_on_relays_uses_ordinary_readback():
    hass, observation = _idle_capture()
    hass.mapping["climate.d1"] = _state("heat", 175.0, hvac_action="heating")
    hass.mapping["switch.d1"] = _state("on", 180.0)
    hass.mapping["switch.d2"] = _state("on", 190.0)
    assert await observation.wait(150.0, timeout=0) == 190.0


@pytest.mark.asyncio
@pytest.mark.parametrize("idle_climate", [False, True])
async def test_group_confirmation_keeps_restore_fence_until_complete_aggregate_report(idle_climate):
    if idle_climate:
        hass, observation = _idle_capture()
        hass.mapping["climate.d1"] = _state("heat", 175.0, hvac_action="idle")
        state = "off"
    else:
        hass, observation = _capture(
            members=("switch.d1", "switch.d2"), expected="on"
        )
        state = "on"
    hass.mapping["switch.d1"] = _state(state, 180.0)
    hass.mapping["switch.d2"] = _state(state, 190.0)
    confirmed_at = await observation.wait(150.0, timeout=0)
    engine = PolicyEngine(policy_for_tests((5000.0, 0.0)))
    engine.append_restore(operation_id="group-restore", load_generation=5)
    engine.set_post_restore_fence(confirmed_at)

    # Aggregate reporting between member transitions observes only partial
    # restored load and must not grant the next restore, even with a new generation.
    assert engine.reconcile_restore(6, reported_at=185.0) is False
    assert engine.runtime.pending_post_restore_generation == 5
    assert engine.reconcile_restore(7, reported_at=190.0) is False
    assert engine.reconcile_restore(8, reported_at=191.0) is True


@pytest.mark.asyncio
async def test_wait_observes_reports_arriving_during_poll(monkeypatch):
    hass, observation = _capture()
    sleeps = []

    async def deliver_report(interval):
        sleeps.append(interval)
        hass.mapping["switch.d1"] = _state("off", 200.0)

    monkeypatch.setattr(readback_module, "asyncio", SimpleNamespace(sleep=deliver_report))
    assert await observation.wait(150.0, timeout=1, poll_interval=0.2) == 200.0
    assert sleeps == [0.2]


@pytest.mark.asyncio
async def test_timeout_caps_sleep_to_remaining_monotonic_budget(monkeypatch):
    _, observation = _capture()
    now = [0.0]
    sleeps = []

    async def advance(interval):
        sleeps.append(interval)
        now[0] += interval

    monkeypatch.setattr(readback_module, "time", SimpleNamespace(
        time=lambda: 200.0, monotonic=lambda: now[0],
    ))
    monkeypatch.setattr(readback_module, "asyncio", SimpleNamespace(sleep=advance))
    assert await observation.wait(150.0, timeout=0.25, poll_interval=1) is None
    assert sleeps == [0.25]
    assert now[0] == 0.25


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout,poll_interval", [(-1, 0.1), (1, 0), (float("nan"), 0.1), (float("inf"), 0.1), (1, float("nan"))])
async def test_invalid_wait_budget_is_rejected(timeout, poll_interval):
    _, observation = _capture()
    with pytest.raises(ValueError):
        await observation.wait(150.0, timeout=timeout, poll_interval=poll_interval)


@pytest.mark.asyncio
async def test_report_arriving_after_monotonic_deadline_does_not_confirm(monkeypatch):
    hass, observation = _capture()
    now = [0.0]

    async def deliver_late_report(interval):
        now[0] += 2.0
        hass.mapping["switch.d1"] = _state("off", 200.0)

    monkeypatch.setattr(readback_module, "time", SimpleNamespace(
        time=lambda: 200.0, monotonic=lambda: now[0],
    ))
    monkeypatch.setattr(readback_module, "asyncio", SimpleNamespace(sleep=deliver_late_report))
    assert await observation.wait(150.0, timeout=1, poll_interval=0.1) is None
