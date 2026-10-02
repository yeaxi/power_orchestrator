"""Native report listeners: immediate safety invalidation, serialized fresh evaluation."""

from collections.abc import Callable, Coroutine, Iterable
from typing import Any

from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.event import (
    EventStateChangedData,
    EventStateReportedData,
    async_track_state_change_event,
    async_track_state_report_event,
)

from .states import state_reported_timestamp


def async_track_reports(
    hass: HomeAssistant,
    entity_ids: Iterable[str],
    load_sensor: str,
    observe_state: Callable[[str, State | None, float | None], None],
    refresh: Callable[[], Coroutine[Any, Any, None]],
) -> Callable[[], None]:
    """No historical ON/OFF commands are queued by these observers."""
    remaining = set(entity_ids)
    removers: list[Callable[[], None]] = []

    @callback
    def load_changed(event: Event[EventStateChangedData]) -> None:
        observe_state(event.data['entity_id'], event.data['new_state'],
                      state_reported_timestamp(event.data['old_state']))
        hass.async_create_task(refresh())

    @callback
    def load_reported(event: Event[EventStateReportedData]) -> None:
        observe_state(event.data['entity_id'], event.data['new_state'],
                      event.data['old_last_reported'].timestamp())
        hass.async_create_task(refresh())

    @callback
    def other_changed(event: Event[EventStateChangedData]) -> None:
        observe_state(event.data['entity_id'], event.data['new_state'],
                      state_reported_timestamp(event.data['old_state']))
        hass.async_create_task(refresh())

    if load_sensor in remaining:
        remaining.remove(load_sensor)
        removers.append(async_track_state_change_event(hass, load_sensor, load_changed))
        removers.append(async_track_state_report_event(hass, load_sensor, load_reported))
    if remaining:
        removers.append(async_track_state_change_event(hass, remaining, other_changed))
    active = True

    @callback
    def remove() -> None:
        nonlocal active
        if not active:
            return
        active = False
        for remover in removers:
            remover()

    return remove
