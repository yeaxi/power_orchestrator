"""Causal relay/actuator readback confirmation.

Bounded polling that confirms a logical device reached the expected state with a
report causally after the command, used to gate every physical action.
"""

from __future__ import annotations

import asyncio
import time

from homeassistant.const import STATE_OFF
from homeassistant.core import HomeAssistant

from .const import RELAY_READBACK_POLL_INTERVAL_SECONDS, RELAY_READBACK_TIMEOUT_SECONDS
from .power_model import ManagedDevice
from .states import logical_device_reported_at, logical_device_state


async def confirm_device_state(
    hass: HomeAssistant,
    device: ManagedDevice,
    expected_state: str,
    *,
    command_issued_at: float,
    pre_reported_at: float | None,
    timeout: float = RELAY_READBACK_TIMEOUT_SECONDS,
    poll_interval: float = RELAY_READBACK_POLL_INTERVAL_SECONDS,
) -> float | None:
    """Return the causal confirmed report timestamp, or ``None`` on timeout.

    The device must reach the expected on/off state with a ``last_reported`` that
    is newer than the pre-command report (or at least at/after the command was
    issued), within the bounded timeout.
    """
    deadline = time.monotonic() + timeout
    expected_on = expected_state != STATE_OFF
    while time.monotonic() <= deadline:
        logical = logical_device_state(hass, device)
        reported_at = logical_device_reported_at(hass, device)
        if logical is expected_on and reported_at is not None:
            if pre_reported_at is None or reported_at > pre_reported_at:
                return reported_at
            if reported_at >= command_issued_at:
                return reported_at
        await asyncio.sleep(poll_interval)
    return None
