"""Focused tests for the Home Assistant Platinum-targeted contract."""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace

import pytest

from power_orchestrator import (
    _REPAIR_ISSUE_IDS_KEY,
    _lifecycle_state,
    _loaded_runtimes,
    _LIFECYCLE_KEY,
    _repair_issue_id,
    _safe_number,
    _sync_repair_issues,
    _translated_error,
    _valid_entity_id,
)
from power_orchestrator.binary_sensor import (
    PowerOrchestratorActionJournalHealthySensor,
    PowerOrchestratorFaultSensor,
    PowerOrchestratorGridOkSensor,
    PowerOrchestratorRecoveryBlockedSensor,
)
from power_orchestrator.diagnostics import async_get_config_entry_diagnostics
from power_orchestrator.power_model import ManagedDevice, PowerModel
from power_orchestrator.sensor import (
    PowerOrchestratorExecutionModeSensor,
    PowerOrchestratorLastOperationSensor,
    PowerOrchestratorReasonCodeSensor,
    PowerOrchestratorStatusSensor,
)


@pytest.mark.parametrize(
    ("value", "domains", "expected"),
    [
        ("switch.boiler", frozenset({"switch"}), True),
        ("light.room", frozenset({"switch"}), False),
        ("switch.", frozenset({"switch"}), False),
        ("switch", frozenset({"switch"}), False),
        (None, frozenset({"switch"}), False),
        (42, frozenset({"switch"}), False),
    ],
)
def test_persisted_entity_id_validation_is_fail_closed(value, domains, expected):
    assert _valid_entity_id(value, domains) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (10, 10.0),
        ("12.5", 12.5),
        (True, 3.0),
        ("bad", 3.0),
        (float("nan"), 3.0),
        (float("inf"), 3.0),
        (-1, 3.0),
        (101, 3.0),
    ],
)
def test_persisted_numeric_normalization_is_bounded(value, expected):
    assert _safe_number(value, default=3.0, minimum=0.0, maximum=100.0) == expected


def test_lifecycle_state_repairs_malformed_domain_state():
    hass = SimpleNamespace(data="invalid")
    state = _lifecycle_state(hass)
    assert isinstance(state["lock"], asyncio.Lock)
    assert state["reservations"] == set()
    assert hass.data[_LIFECYCLE_KEY] is state

    state["lock"] = "invalid"
    state["reservations"] = ["invalid"]
    repaired = _lifecycle_state(hass)
    assert isinstance(repaired["lock"], asyncio.Lock)
    assert repaired["reservations"] == set()


def test_loaded_runtimes_filters_entries_and_bad_entry_api():
    assert _loaded_runtimes(SimpleNamespace()) == []
    assert _loaded_runtimes(
        SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda _: "bad"))
    ) == []

    loaded = SimpleNamespace(
        state=SimpleNamespace(value="loaded"), runtime_data="runtime-loaded"
    )
    unloaded = SimpleNamespace(
        state=SimpleNamespace(value="not_loaded"), runtime_data="runtime-unloaded"
    )
    state_free = SimpleNamespace(state=None, runtime_data="runtime-state-free")
    no_runtime = SimpleNamespace(state="loaded", runtime_data=None)
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_entries=lambda _: [loaded, unloaded, state_free, no_runtime]
        )
    )
    assert _loaded_runtimes(hass) == ["runtime-loaded", "runtime-state-free"]


def test_translated_error_supports_legacy_exception_constructor():
    class LegacyError(Exception):
        pass

    error = _translated_error(LegacyError, "legacy_key", reason="reason")
    assert isinstance(error, LegacyError)
    assert error.args == ("legacy_key",)


@pytest.mark.asyncio
async def test_diagnostics_redacts_sensitive_nested_values() -> None:
    """Diagnostics must not expose credential-like or location-sensitive values."""
    coordinator = SimpleNamespace(
        data={
            "status": "safety_blocked",
            "audit_history": [{"actor_id": "user-1", "entity_id": "switch.secret"}],
            "faulted_devices": ["device-1"],
        }
    )
    entry = SimpleNamespace(
        data={
            "load_sensor": "sensor.load",
            "api_key": "secret-key",
            "nested": {"client_secret": "secret-client", "latitude": 50.0},
        },
        options={"password": "secret-password"},
        runtime_data=SimpleNamespace(coordinator=coordinator),
    )

    result = await async_get_config_entry_diagnostics(SimpleNamespace(), entry)

    assert result["entry_data"]["api_key"] == "**REDACTED**"
    assert result["entry_data"]["nested"]["client_secret"] == "**REDACTED**"
    assert result["entry_data"]["nested"]["latitude"] == "**REDACTED**"
    assert result["options"]["password"] == "**REDACTED**"
    assert result["runtime"]["data"]["status"] == "safety_blocked"
    assert result["runtime"]["data"]["faulted_devices_count"] == 1
    assert "audit_history" not in result["runtime"]["data"]


@pytest.mark.asyncio
async def test_diagnostics_bounds_malformed_runtime_and_unloaded_entry() -> None:
    """Malformed persisted collections must not leak or crash diagnostics."""
    malformed_entry = SimpleNamespace(
        data=["not-a-mapping"],
        options=None,
        runtime_data=SimpleNamespace(
            coordinator=SimpleNamespace(
                data={
                    "faulted_devices": None,
                    "recovery_blocked_devices": {"device-1": "unexpected"},
                }
            )
        ),
    )
    result = await async_get_config_entry_diagnostics(SimpleNamespace(), malformed_entry)
    assert result["entry_data"] == {}
    assert result["options"] == {}
    assert result["runtime"]["data"]["faulted_devices_count"] == 0
    assert result["runtime"]["data"]["recovery_blocked_devices_count"] == 0

    unloaded_entry = SimpleNamespace(data=None, options=None, runtime_data=None)
    unloaded = await async_get_config_entry_diagnostics(SimpleNamespace(), unloaded_entry)
    assert unloaded["runtime"] == {
        "loaded": False,
        "data": {
            "faulted_devices_count": 0,
            "recovery_blocked_devices_count": 0,
        },
    }

    non_mapping_entry = SimpleNamespace(
        data=None,
        options=None,
        runtime_data=SimpleNamespace(coordinator=SimpleNamespace(data=42)),
    )
    non_mapping = await async_get_config_entry_diagnostics(
        SimpleNamespace(), non_mapping_entry
    )
    assert non_mapping["runtime"]["data"] == {"value_type": "int"}


@pytest.mark.asyncio
async def test_diagnostics_preserves_explicit_bounded_counts_without_raw_collections():
    entry = SimpleNamespace(
        data={},
        options={},
        runtime_data=SimpleNamespace(
            coordinator=SimpleNamespace(
                data={
                    "status": "idle",
                    "faulted_devices_count": 3,
                    "recovery_blocked_devices_count": 1,
                }
            )
        ),
    )

    result = await async_get_config_entry_diagnostics(SimpleNamespace(), entry)

    assert result["runtime"]["data"] == {
        "status": "idle",
        "faulted_devices_count": 3,
        "recovery_blocked_devices_count": 1,
    }


def test_repair_issue_registry_handles_union_and_malformed_storage(monkeypatch) -> None:
    """Repair sync filters invalid IDs and replaces malformed HA data safely."""
    calls: list[tuple[str, str, dict]] = []

    class Severity:
        ERROR = "error"

    issue_registry = ModuleType("homeassistant.helpers.issue_registry")
    issue_registry.IssueSeverity = Severity
    issue_registry.async_create_issue = lambda hass, domain, issue_id, **kwargs: calls.append(
        ("create", issue_id, kwargs)
    )
    issue_registry.async_delete_issue = lambda hass, domain, issue_id: calls.append(
        ("delete", issue_id, {})
    )
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.issue_registry", issue_registry)

    hass = SimpleNamespace(data="malformed-domain-data")
    coordinator = SimpleNamespace(
        data={
            "faulted_devices": ["device-z", "", None, 42],
            "recovery_blocked_devices": {"device-a", "device-z"},
        }
    )
    _sync_repair_issues(hass, coordinator, PowerModel())

    assert isinstance(hass.data, dict)
    assert {call[0:2] for call in calls} == {
        ("create", _repair_issue_id("device-a")),
        ("create", _repair_issue_id("device-z")),
    }
    assert hass.data[_REPAIR_ISSUE_IDS_KEY] == {
        _repair_issue_id("device-a"),
        _repair_issue_id("device-z"),
    }

    calls.clear()
    coordinator.data = {"faulted_devices": "invalid", "recovery_blocked_devices": None}
    _sync_repair_issues(hass, coordinator, PowerModel())
    assert {call[0:2] for call in calls} == {
        ("delete", _repair_issue_id("device-a")),
        ("delete", _repair_issue_id("device-z")),
    }


def test_repair_issue_registry_mirrors_durable_quarantine(monkeypatch) -> None:
    """Quarantine issue creation/deletion is derived and does not mutate safety state."""
    calls: list[tuple[str, str, dict]] = []

    class Severity:
        ERROR = "error"

    issue_registry = ModuleType("homeassistant.helpers.issue_registry")
    issue_registry.IssueSeverity = Severity
    issue_registry.async_create_issue = lambda hass, domain, issue_id, **kwargs: calls.append(
        ("create", issue_id, kwargs)
    )
    issue_registry.async_delete_issue = lambda hass, domain, issue_id: calls.append(
        ("delete", issue_id, {})
    )
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.issue_registry", issue_registry)

    model = PowerModel()
    model.add_device(ManagedDevice("device-1", "Device 1", "switch.device_1"))
    model.add_device(ManagedDevice("device-2", "Device 2", "switch.device_2"))
    coordinator = SimpleNamespace(
        data={
            "faulted_devices": ["device-1"],
            "recovery_blocked_devices": [],
        }
    )

    hass = SimpleNamespace()
    _sync_repair_issues(hass, coordinator, model)
    assert calls[0][0:2] == ("create", _repair_issue_id("device-1"))
    assert calls[0][2]["is_fixable"] is False
    assert calls[0][2]["is_persistent"] is True
    assert calls[0][2]["translation_key"] == "quarantine_requires_reconciliation"

    calls.clear()
    coordinator.data = {"faulted_devices": [], "recovery_blocked_devices": []}
    _sync_repair_issues(hass, coordinator, model)
    assert {call[0:2] for call in calls} == {
        ("delete", _repair_issue_id("device-1")),
    }


def test_diagnostic_entities_use_categories_and_translation_keys() -> None:
    """Diagnostic entities use HA metadata instead of hardcoded icons."""
    diagnostic_classes = (
        PowerOrchestratorStatusSensor,
        PowerOrchestratorExecutionModeSensor,
        PowerOrchestratorReasonCodeSensor,
        PowerOrchestratorLastOperationSensor,
        PowerOrchestratorGridOkSensor,
        PowerOrchestratorFaultSensor,
        PowerOrchestratorRecoveryBlockedSensor,
        PowerOrchestratorActionJournalHealthySensor,
    )
    assert all(
        getattr(entity_class, "_attr_entity_category", None) == "diagnostic"
        for entity_class in diagnostic_classes
    )
    assert all(hasattr(entity_class, "_attr_translation_key") for entity_class in diagnostic_classes)
    assert all(not hasattr(entity_class, "_attr_icon") for entity_class in diagnostic_classes)
