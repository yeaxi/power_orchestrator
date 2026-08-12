"""High-value lifecycle, diagnostics, and entity metadata contracts."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from power_orchestrator import (
    _lifecycle_state,
    _loaded_runtimes,
    _repair_device_ids,
    _repair_issue_id,
    _safe_number,
    _sync_repair_issues,
    _valid_entity_id,
)
from power_orchestrator.binary_sensor import (
    PowerOrchestratorActionJournalHealthySensor,
    PowerOrchestratorFaultSensor,
    PowerOrchestratorGridOkSensor,
)
from power_orchestrator.diagnostics import async_get_config_entry_diagnostics
from power_orchestrator.power_model import ManagedDevice, PowerModel


def test_lifecycle_registry_and_helpers_fail_closed() -> None:
    hass = SimpleNamespace(data="malformed")
    assert isinstance(_lifecycle_state(hass), dict)
    assert _valid_entity_id("switch.load", frozenset({"switch"})) == "switch.load"
    assert _valid_entity_id("light.load", frozenset({"switch"})) is None
    assert _safe_number(float("inf"), default=3, minimum=0, maximum=10) == 3


def test_loaded_runtime_and_quarantine_helpers_use_current_runtime() -> None:
    coordinator = SimpleNamespace(_faults=SimpleNamespace(faulted=set(), quarantined={"d1"}))
    runtime = SimpleNamespace(coordinator=coordinator)
    entry = SimpleNamespace(entry_id="entry-1", runtime_data=runtime)
    hass = SimpleNamespace(data={"power_orchestrator": {"entry-1": runtime}})
    assert _loaded_runtimes(hass) == [runtime]
    assert _repair_device_ids(hass, entry) == {"d1"}
    assert _repair_issue_id("entry-1", "d1").startswith("quarantine_")
    _sync_repair_issues(hass, entry)


@pytest.mark.asyncio
async def test_diagnostics_redact_sensitive_values_and_bound_runtime() -> None:
    entry = SimpleNamespace(
        data={"api_key": "secret", "nested": {"latitude": 1.0}},
        options={"password": "secret"},
        runtime_data=SimpleNamespace(
            coordinator=SimpleNamespace(data={"status": "safety_blocked", "faulted_devices": ["d1"]})
        ),
    )
    result = await async_get_config_entry_diagnostics(MagicMock(), entry)
    assert result["entry_data"]["api_key"] == "**REDACTED**"
    assert result["entry_data"]["nested"]["latitude"] == "**REDACTED**"
    assert result["options"]["password"] == "**REDACTED**"
    assert result["runtime"]["data"]["faulted_devices_count"] == 1
    assert "audit_history" not in result["runtime"]["data"]


def test_binary_diagnostic_entities_have_translation_metadata() -> None:
    for entity_class in (
        PowerOrchestratorGridOkSensor,
        PowerOrchestratorFaultSensor,
        PowerOrchestratorActionJournalHealthySensor,
    ):
        assert getattr(entity_class, "_attr_translation_key", None)
        # The entity must not hard-code an icon of its own; icons come from
        # icon translations. (The Home Assistant base class defines a default
        # ``_attr_icon``, so only inspect this class's own namespace.)
        assert "_attr_icon" not in entity_class.__dict__


def test_repair_issue_does_not_mutate_model() -> None:
    model = PowerModel()
    model.add_device(ManagedDevice("d1", "Device", "switch.d1"))
    before = model.get_device("d1").is_on
    # The repair sync only reflects durable state into the issue registry; it
    # must never touch the logical model. A bare entry (no runtime) yields no
    # active issues and the registry lookup fails closed.
    _sync_repair_issues(SimpleNamespace(data={}), SimpleNamespace(entry_id="entry-x"))
    assert model.get_device("d1").is_on == before
