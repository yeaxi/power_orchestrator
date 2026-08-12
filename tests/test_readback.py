"""Unit tests for causal readback confirmation."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from power_orchestrator.power_model import ManagedDevice
from power_orchestrator.readback import confirm_device_state


class _Hass:
    def __init__(self, mapping):
        self.states = SimpleNamespace(get=lambda entity_id: mapping.get(entity_id))


def _state(value, last_reported):
    return SimpleNamespace(state=value, attributes={}, last_reported=last_reported)


@pytest.mark.asyncio
async def test_confirms_off_with_causal_report() -> None:
    device = ManagedDevice("d1", "D1", "switch.d1")
    hass = _Hass({"switch.d1": _state("off", 200.0)})
    confirmed = await confirm_device_state(
        hass, device, "off", command_issued_at=150.0, pre_reported_at=100.0
    )
    assert confirmed == 200.0


@pytest.mark.asyncio
async def test_confirms_on_at_or_after_command_when_no_prior_report() -> None:
    device = ManagedDevice("d1", "D1", "switch.d1")
    hass = _Hass({"switch.d1": _state("on", 150.0)})
    confirmed = await confirm_device_state(
        hass, device, "on", command_issued_at=150.0, pre_reported_at=None
    )
    assert confirmed == 150.0


@pytest.mark.asyncio
async def test_stale_report_is_not_causal() -> None:
    device = ManagedDevice("d1", "D1", "switch.d1")
    # Report predates both the pre-report and the command -> not causal -> timeout.
    hass = _Hass({"switch.d1": _state("off", 90.0)})
    confirmed = await confirm_device_state(
        hass, device, "off", command_issued_at=150.0, pre_reported_at=100.0,
        timeout=0.02, poll_interval=0.005,
    )
    assert confirmed is None


@pytest.mark.asyncio
async def test_wrong_state_times_out() -> None:
    device = ManagedDevice("d1", "D1", "switch.d1")
    hass = _Hass({"switch.d1": _state("on", 200.0)})
    confirmed = await confirm_device_state(
        hass, device, "off", command_issued_at=150.0, pre_reported_at=100.0,
        timeout=0.02, poll_interval=0.005,
    )
    assert confirmed is None
