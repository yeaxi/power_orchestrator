"""Optional bounded JSON projection binding for an external intent producer.

The producer selects desired-state fields, not execution/readback fields. Read
them directly on every permission check; no template-entity update is awaited.
"""
from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from typing import Any


def _valid_scalar(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, str):
        return len(value) <= 80
    if isinstance(value, (int, float)):
        try:
            return math.isfinite(value)
        except OverflowError:
            return False
    return False


def validate_binding(entity: str | None, data: Any) -> dict[str, Any] | None:
    """Reject partial, oversized or non-scalar bindings; copy caller input."""
    if entity is None and data is None:
        return None
    if not isinstance(entity, str) or not entity.startswith("input_text."):
        raise ValueError("request_entity must be an input_text entity")
    if not isinstance(data, dict) or not 1 <= len(data) <= 16:
        raise ValueError("request_data must contain 1..16 desired-state fields")
    for key, value in data.items():
        if not isinstance(key, str) or not 1 <= len(key) <= 64 or not _valid_scalar(value):
            raise ValueError("invalid desired-state field")
    return dict(data)


def _same_scalar(expected: Any, actual: Any) -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(expected) is type(actual) and expected == actual
    return expected == actual


def binding_permits(entity: str | None, expected: Mapping[str, Any] | None,
                    state_lookup: Callable[[str], Any]) -> bool:
    """Unknown, invalid or replaced producer state never permits restoration."""
    if entity is None and expected is None:
        return True
    if entity is None or not expected:
        return False
    state = state_lookup(entity)
    raw = getattr(state, "state", None)
    if not isinstance(raw, str):
        return False
    try:
        actual = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(actual, Mapping):
        return False
    return all(key in actual and _same_scalar(value, actual[key])
               for key, value in expected.items())
