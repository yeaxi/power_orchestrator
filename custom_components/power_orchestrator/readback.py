"""Capture and confirm causal Logical device reports through one bounded wait.

The observation owns member snapshots, temporal fences and the idle climate
exception. Dispatch and permission checks stay with the caller.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any

from homeassistant.const import STATE_OFF
from homeassistant.core import HomeAssistant

from .const import RELAY_READBACK_POLL_INTERVAL_SECONDS, RELAY_READBACK_TIMEOUT_SECONDS
from .power_model import ManagedDevice
from .states import actuator_state_on, state_reported_timestamp

_IDLE_RELAY_MAX_AGE_SECONDS = 180


class CausalReadback:
    """Capture before dispatch, then wait using the command's issuance timestamp.

    Home Assistant's state reader and the local test state reader satisfy the
    same seam. Callers never need per-member timestamps or polling knowledge.
    """

    def __init__(
        self, hass: HomeAssistant, device: ManagedDevice, expected_state: str
    ) -> None:
        self._hass = hass
        self._members = device.readback_entities
        self._command_entity = device.command_entity
        self._expected_on = expected_state != STATE_OFF
        self._previous = {
            entity: state_reported_timestamp(hass.states.get(entity))
            for entity in dict.fromkeys((*self._members, self._command_entity))
        }

    def _causal_report(
        self, entity: str, state: Any, command_issued_at: float, now: float
    ) -> float | None:
        reported = state_reported_timestamp(state)
        previous = self._previous[entity]
        if (
            reported is None
            or not command_issued_at <= reported <= now
            or (previous is not None and reported <= previous)
        ):
            return None
        return reported

    def _idle_climate_report(self, command_issued_at: float, now: float) -> float | None:
        """Accept a causal thermostat report at target with fresh OFF relays.

        The thermostat must newly report enabled and idle after the command;
        already-OFF relays need only be fresh, since reaching target correctly
        leaves them OFF without causing a new relay report.
        """
        if not self._command_entity.startswith("climate."):
            return None
        state = self._hass.states.get(self._command_entity)
        reported = self._causal_report(self._command_entity, state, command_issued_at, now)
        if (
            state is None
            or reported is None
            or actuator_state_on(self._command_entity, state) is not True
            or state.attributes.get("hvac_action") != "idle"
        ):
            return None
        confirmed = [reported]
        for entity in self._members:
            if entity == self._command_entity:
                continue
            relay = self._hass.states.get(entity)
            stamp = state_reported_timestamp(relay)
            if (
                actuator_state_on(entity, relay) is not False
                or stamp is None
                or not 0 <= now - stamp <= _IDLE_RELAY_MAX_AGE_SECONDS
            ):
                return None
            confirmed.append(stamp)
        return max(confirmed)

    def _confirmed_report(self, command_issued_at: float) -> float | None:
        # An empty required member set must never produce successful readback.
        if not self._members:
            return None
        now = time.time()
        if self._expected_on:
            idle_report = self._idle_climate_report(command_issued_at, now)
            if idle_report is not None:
                return idle_report
        confirmed = []
        for entity in self._members:
            state = self._hass.states.get(entity)
            reported = self._causal_report(entity, state, command_issued_at, now)
            if actuator_state_on(entity, state) is not self._expected_on or reported is None:
                return None
            confirmed.append(reported)
        # The logical device is confirmed only when its final member reports.
        # Using the earliest member would let a partial aggregate load report
        # clear the post-action fence before the whole group has transitioned.
        return max(confirmed)

    async def wait(
        self,
        command_issued_at: float,
        *,
        timeout: float = RELAY_READBACK_TIMEOUT_SECONDS,
        poll_interval: float = RELAY_READBACK_POLL_INTERVAL_SECONDS,
    ) -> float | None:
        """Return the confirmed report time, or ``None`` after the bounded wait.

        Every ordinary member report must be at/after command issuance AND
        strictly newer than its captured report. The deadline uses monotonic
        time, and the final sleep is limited to the remaining budget.
        """
        if (
            not math.isfinite(timeout)
            or not math.isfinite(poll_interval)
            or timeout < 0
            or poll_interval <= 0
        ):
            raise ValueError("Readback requires a non-negative timeout and positive poll interval")
        deadline = time.monotonic() + timeout
        first_poll = True
        while first_poll or time.monotonic() <= deadline:
            first_poll = False
            confirmed = self._confirmed_report(command_issued_at)
            if confirmed is not None:
                return confirmed
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(poll_interval, remaining))
        return None
