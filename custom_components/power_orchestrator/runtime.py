"""Runtime data owned by a Power Orchestrator config entry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class PowerOrchestratorRuntimeData:
    """Objects created for one loaded config entry."""

    coordinator: Any
    model: Any
    store: Any
    repair_listener_remove: Callable[[], None] | None = None
