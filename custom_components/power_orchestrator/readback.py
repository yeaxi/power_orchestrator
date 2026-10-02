"""Causal relay/actuator readback confirmation.

Bounded polling that confirms a logical device reached the expected state with a
report causally after the command, used to gate every physical action.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping

from homeassistant.const import STATE_OFF
from homeassistant.core import HomeAssistant

from .const import RELAY_READBACK_POLL_INTERVAL_SECONDS, RELAY_READBACK_TIMEOUT_SECONDS
from .power_model import ManagedDevice
from .states import actuator_state_on, state_reported_timestamp


def _idle_climate_report(hass, device, command_issued_at):
    """A thermostat can accept HEAT while its relay correctly stays OFF at target."""
    if not device.command_entity.startswith('climate.'):
        return None
    state = hass.states.get(device.command_entity)
    reported = state_reported_timestamp(state)
    if actuator_state_on(device.command_entity, state) is not True or reported is None:
        return None
    if reported < command_issued_at or state.attributes.get('hvac_action') != 'idle':
        return None
    for entity in device.readback_entities:
        if entity == device.command_entity:
            continue
        relay = hass.states.get(entity)
        stamp = state_reported_timestamp(relay)
        if actuator_state_on(entity, relay) is not False or stamp is None:
            return None
        if not 0 <= time.time() - stamp <= 180:
            return None
    return reported


def _member_is_causal(
    state: object,
    entity_id: str,
    expected_on: bool,
    command_issued_at: float,
    previous: float | None,
) -> tuple[bool, float | None]:
    reported_at = state_reported_timestamp(state)
    if actuator_state_on(entity_id, state) is not expected_on or reported_at is None:
        return False, reported_at
    boundary = previous if previous is not None else command_issued_at
    return reported_at > boundary if previous is not None else reported_at >= boundary, reported_at


async def confirm_device_state(
    hass: HomeAssistant,
    device: ManagedDevice,
    expected_state: str,
    *,
    command_issued_at: float,
    pre_reported_at: float | None = None,
    pre_reported_by_entity: Mapping[str, float | None] | None = None,
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
    previous_by_entity = dict(pre_reported_by_entity or {})
    while time.monotonic() <= deadline:
        if expected_on and (idle_report := _idle_climate_report(hass, device, command_issued_at)) is not None:
            return idle_report
        confirmed: list[float] = []
        for entity_id in device.readback_entities:
            previous = previous_by_entity.get(entity_id, pre_reported_at)
            causal, reported_at = _member_is_causal(
                hass.states.get(entity_id),
                entity_id,
                expected_on,
                command_issued_at,
                previous,
            )
            if not causal or reported_at is None:
                break
            confirmed.append(reported_at)
        if len(confirmed) == len(device.readback_entities):
            return min(confirmed)
        await asyncio.sleep(poll_interval)
    return None
