"""Durable, ordered restore ownership and journaled physical transitions.

Policy decides whether to attempt an action. The physical adapter still checks
permission immediately before each service call. This module owns the protocol
between those decisions: persist dispatch intent, run once, and commit the
terminal result together with ticket/order and policy-fence changes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from enum import StrEnum
from typing import Any

from .requests import MAX_INTENT_TTL_S, RestoreIntentRegistry, RestoreTicket


class TransitionResult(StrEnum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    FAILED = "failed"
    STORAGE_BLOCKED = "storage_blocked"


class RestoreTransactions:
    """Own tickets and enforce the same durable protocol for stop and restore."""

    def __init__(
        self,
        *,
        record: Callable[[dict[str, Any]], None],
        persist: Callable[[], Awaitable[bool]],
        persistence_failed: Callable[[str, str], None],
    ) -> None:
        self.tickets: dict[str, RestoreTicket] = {}
        self.pending: list[str] = []
        self._record = record
        self._persist = persist
        self._persistence_failed = persistence_failed

    def hydrate(self, tickets: Mapping[str, RestoreTicket]) -> None:
        """Replace ownership with validated durable proof, preserving shed order."""
        self.tickets = dict(tickets)
        self.pending = list(tickets)

    def create(
        self,
        device_id: str,
        *,
        cause: str,
        operation_id: str,
        restore_state: Mapping[str, Any],
        intent_sources: tuple[str, ...],
        now: float,
    ) -> None:
        self.remove(device_id)
        self.tickets[device_id] = RestoreTicket(
            device_id=device_id, cause=cause, operation_id=operation_id,
            created_at=now, expires_at=now + MAX_INTENT_TTL_S,
            restore_state=dict(restore_state), intent_sources=intent_sources,
        )
        self.pending.append(device_id)

    def remove(self, device_id: str) -> None:
        if device_id in self.pending:
            self.pending.remove(device_id)
        self.tickets.pop(device_id, None)

    def reconcile(self, intents: RestoreIntentRegistry, *, now: float) -> bool:
        """Retire expired/orphan proof without implying a physical action."""
        changed = intents.prune(now)
        for device_id, ticket in tuple(self.tickets.items()):
            if ticket.expired(now) or not intents.active_sources(device_id, now):
                self.remove(device_id)
                changed = True
        return changed

    async def execute(
        self,
        base: dict[str, Any],
        *,
        dispatch: Callable[[], Awaitable[TransitionResult]],
        complete: Callable[[TransitionResult, str], None],
    ) -> TransitionResult:
        """Publish durable intent before actuation and one atomic terminal snapshot.

        ``dispatch`` includes fresh permission and causal physical readback.
        ``complete`` applies the policy fence, pause and fault projection without
        awaiting. Failed persistence cannot roll those projections back into a
        second attempt: ownership is retired and the device is quarantined. A
        restart from the last durable dispatch instead recovers as unresolved.
        """
        device_id = str(base["device_id"])
        try:
            for phase in ("prepared", "dispatched"):
                self._record({**base, "phase": phase, "result": phase})
                if not await self._persist():
                    return self._storage_failure(base, device_id)
            try:
                result = await dispatch()
                if result == TransitionResult.CONFIRMED:
                    reason = "confirmed"
                elif result == TransitionResult.REJECTED:
                    reason = (
                        "restore_permission_withdrawn" if base["action"] == "turn_on"
                        else "off_permission_withdrawn"
                    )
                else:
                    reason = "relay_readback_timeout"
                outcome_reason = reason
            except Exception as exc:
                result = TransitionResult.FAILED
                reason = str(exc)[:160] or "service_error"
                outcome_reason = "service_error"
            complete(result, reason)
            self._record(
                {**base, "phase": result.value, "result": result.value,
                 "reason": base["decision_reason"] if result == TransitionResult.CONFIRMED else reason,
                 "outcome_reason": outcome_reason}
            )
            if not await self._persist():
                return self._storage_failure(base, device_id)
            return result

        except asyncio.CancelledError:
            # Cancellation is not proof that a dispatched physical call failed
            # to run. Retire ownership and quarantine before propagating it.
            self.remove(device_id)
            complete(TransitionResult.FAILED, "action_cancelled")
            self._persistence_failed(device_id, "action_cancelled")
            self._record(
                {**base, "phase": "failed", "result": "failed",
                 "reason": "action_cancelled", "outcome_reason": "action_cancelled"}
            )
            await self._persist()
            raise

    def _storage_failure(self, base: dict[str, Any], device_id: str) -> TransitionResult:
        # Keep the last durable phase recoverable. A later successful save carries
        # explicit failure + quarantine, never the obsolete ticket as retry proof.
        self.remove(device_id)
        self._persistence_failed(device_id, "action_persistence_failed")
        self._record(
            {**base, "phase": "failed", "result": "failed",
             "reason": "action_persistence_failed", "outcome_reason": "action_persistence_failed"}
        )
        return TransitionResult.STORAGE_BLOCKED
