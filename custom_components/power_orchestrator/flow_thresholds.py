"""Overload-threshold input parsing and form fields for the config/options flow.

Pure threshold-handling helpers, separated from the flow classes so the flow
only orchestrates steps. Names keep their leading underscore to preserve the
existing (re-exported) import surface.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, cast

import voluptuous as vol
from homeassistant.helpers import selector

from .const import (
    CONF_ADD_THRESHOLD,
    CONF_THRESHOLD_COUNT,
    CONF_THRESHOLD_DURATION,
    CONF_THRESHOLD_POWER,
    DEFAULT_HARD_INTERLOCK,
    DEFAULT_SHED_CRITICAL_DURATION,
    DEFAULT_SHED_CRITICAL_LIMIT,
    DEFAULT_SHED_FAST_DURATION,
    DEFAULT_SHED_FAST_LIMIT,
    DEFAULT_SHED_SUSTAINED_DURATION,
    DEFAULT_SHED_SUSTAINED_LIMIT,
    MAX_CUSTOM_THRESHOLDS,
)
from .policy import validate_threshold_pair

_CANONICAL_THRESHOLD_INPUTS = (
    {"power_limit": DEFAULT_SHED_SUSTAINED_LIMIT, "duration_s": DEFAULT_SHED_SUSTAINED_DURATION},
    {"power_limit": DEFAULT_SHED_FAST_LIMIT, "duration_s": DEFAULT_SHED_FAST_DURATION},
    {"power_limit": DEFAULT_SHED_CRITICAL_LIMIT, "duration_s": DEFAULT_SHED_CRITICAL_DURATION},
)


def _threshold_field(index: int, kind: str) -> str:
    return f"threshold_{index}_{kind}"


def _threshold_defaults(value: Any) -> list[dict[str, float]]:
    """Normalize persisted threshold pairs and fill canonical defaults."""
    result: list[dict[str, float]] = []
    if isinstance(value, (list, tuple)):
        for raw in value[:MAX_CUSTOM_THRESHOLDS]:
            if not isinstance(raw, Mapping):
                continue
            try:
                limit = float(raw.get("power_limit", raw.get("limit_w")))
                duration = float(raw.get("duration_s", raw.get("time_s")))
            except (TypeError, ValueError):
                continue
            if math.isfinite(limit) and math.isfinite(duration) and limit > 0 and duration >= 0:
                result.append({"power_limit": limit, "duration_s": duration})
    if not result:
        result = [dict(item) for item in _CANONICAL_THRESHOLD_INPUTS]
    return result


def _parse_threshold_input(
    user_input: Mapping[str, Any],
    defaults: list[dict[str, float]] | None = None,
) -> tuple[list[dict[str, float]] | None, str | None]:
    """Parse legacy numbered threshold pairs with the bounded runtime limit."""
    raw_count = user_input.get(CONF_THRESHOLD_COUNT)
    if raw_count is None:
        pairs = defaults or _threshold_defaults(None)
        return pairs, None
    if isinstance(raw_count, bool):
        return None, "invalid_thresholds"
    try:
        count = int(raw_count)
    except (TypeError, ValueError):
        return None, "invalid_thresholds"
    if count != raw_count or not 1 <= count <= MAX_CUSTOM_THRESHOLDS:
        return None, "invalid_thresholds"
    parsed: list[dict[str, float]] = []
    previous = 0.0
    for index in range(1, count + 1):
        power = user_input.get(_threshold_field(index, "power"))
        duration = user_input.get(_threshold_field(index, "time"))
        if (
            power is None
            or duration is None
            or isinstance(power, bool)
            or isinstance(duration, bool)
        ):
            return None, "invalid_thresholds"
        try:
            limit, dwell = validate_threshold_pair(float(power), float(duration), previous)
        except (TypeError, ValueError):
            return None, "invalid_thresholds"
        if limit > DEFAULT_HARD_INTERLOCK:
            return None, "invalid_thresholds"
        parsed.append({"power_limit": limit, "duration_s": dwell})
        previous = limit
    return parsed, None


def _threshold_step_fields(
    default: Mapping[str, Any] | None = None,
    *,
    add_another_default: bool = False,
) -> dict[Any, Any]:
    """Build one repeatable threshold form instead of fixed numbered fields."""
    pair = default or {
        "power_limit": DEFAULT_SHED_SUSTAINED_LIMIT,
        "duration_s": DEFAULT_SHED_SUSTAINED_DURATION,
    }
    return {
        vol.Required(CONF_THRESHOLD_POWER, default=pair["power_limit"]): selector.NumberSelector(
            cast(Any, selector.NumberSelectorConfig)(
                min=1,
                max=DEFAULT_HARD_INTERLOCK,
                mode="box",
                unit_of_measurement="W",
            )
        ),
        vol.Required(CONF_THRESHOLD_DURATION, default=pair["duration_s"]): selector.NumberSelector(
            cast(Any, selector.NumberSelectorConfig)(
                min=0,
                max=86400,
                mode="box",
                unit_of_measurement="s",
            )
        ),
        vol.Optional(CONF_ADD_THRESHOLD, default=add_another_default): bool,
    }


def _next_threshold_default(
    collected: list[dict[str, float]], seed: list[dict[str, float]]
) -> dict[str, float]:
    """Return the next useful default for a repeatable threshold step."""
    if len(collected) < len(seed):
        return dict(seed[len(collected)])
    previous = collected[-1] if collected else {"power_limit": 0.0, "duration_s": 0.0}
    return {
        "power_limit": min(DEFAULT_HARD_INTERLOCK, previous["power_limit"] + 500.0),
        "duration_s": previous["duration_s"],
    }


def _parse_threshold_step(
    user_input: Mapping[str, Any], previous: float
) -> tuple[dict[str, float] | None, str | None]:
    """Validate one repeatable threshold pair against the collected prefix."""
    raw_power = user_input.get(CONF_THRESHOLD_POWER)
    raw_duration = user_input.get(CONF_THRESHOLD_DURATION)
    if (
        raw_power is None
        or raw_duration is None
        or isinstance(raw_power, bool)
        or isinstance(raw_duration, bool)
    ):
        return None, "invalid_thresholds"
    try:
        limit, duration = validate_threshold_pair(
            float(raw_power),
            float(raw_duration),
            previous,
        )
    except (TypeError, ValueError):
        return None, "invalid_thresholds"
    if limit > DEFAULT_HARD_INTERLOCK:
        return None, "invalid_thresholds"
    return {"power_limit": limit, "duration_s": duration}, None


def _threshold_add_allowed(
    collected: list[dict[str, float]], pair: Mapping[str, float], user_input: Mapping[str, Any]
) -> bool:
    """Prevent an add-another request that cannot produce a valid next step."""
    if not user_input.get(CONF_ADD_THRESHOLD, False):
        return True
    return (
        pair["power_limit"] < DEFAULT_HARD_INTERLOCK and len(collected) + 1 < MAX_CUSTOM_THRESHOLDS
    )


def _validate_threshold_collection(value: Any) -> list[dict[str, float]]:
    """Validate a structured threshold list at the options boundary."""
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= MAX_CUSTOM_THRESHOLDS:
        raise ValueError("invalid thresholds")
    parsed: list[dict[str, float]] = []
    previous = 0.0
    for raw in value:
        if not isinstance(raw, Mapping):
            raise ValueError("invalid thresholds")
        try:
            limit, duration = validate_threshold_pair(
                float(raw.get("power_limit", raw.get("limit_w"))),
                float(raw.get("duration_s", raw.get("time_s"))),
                previous,
            )
        except (TypeError, ValueError):
            raise ValueError("invalid thresholds") from None
        if limit > DEFAULT_HARD_INTERLOCK:
            raise ValueError("invalid thresholds")
        parsed.append({"power_limit": limit, "duration_s": duration})
        previous = limit
    return parsed
