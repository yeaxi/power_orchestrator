"""Telemetry loss/recovery contracts through public APIs on local HA Core.

All entities and services belong to an isolated in-memory Home Assistant. Runtime
storage is written only under a temporary directory; no live HA is contacted.
Run with plain unittest in a Core environment without the older repository mocks.
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components"))

GRID = "binary_sensor.test_supply"
LOAD = "sensor.test_aggregate"
DEVICE = "switch.test_load"


class TelemetryRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from homeassistant.core import HomeAssistant
        from homeassistant.helpers import frame
        from homeassistant.helpers.storage import Store
        from power_orchestrator.const import STORAGE_VERSION
        from power_orchestrator.coordinator import CoordinatorConfig, PowerOrchestratorCoordinator
        from power_orchestrator.policy import PolicyConfig
        from power_orchestrator.power_model import ManagedDevice, PowerModel
        from power_orchestrator.storage import RuntimeStore

        self.directory = tempfile.TemporaryDirectory(prefix="po-telemetry-")
        self.hass = HomeAssistant(self.directory.name)
        frame.async_setup(self.hass)
        self.commands: list[tuple[str, str]] = []
        self.notifications: dict[str, str] = {}
        self.notification_creates = 0

        async def turn_off(call):
            self.commands.append(("turn_off", call.data["entity_id"]))
            self.hass.states.async_set(DEVICE, "off", context=call.context, force_update=True)

        async def turn_on(call):
            self.commands.append(("turn_on", call.data["entity_id"]))
            self.hass.states.async_set(DEVICE, "on", context=call.context, force_update=True)

        async def create_notification(call):
            self.notification_creates += 1
            self.notifications[call.data["notification_id"]] = call.data["message"]

        async def dismiss_notification(call):
            self.notifications.pop(call.data["notification_id"], None)

        self.hass.services.async_register("switch", "turn_off", turn_off)
        self.hass.services.async_register("switch", "turn_on", turn_on)
        self.hass.services.async_register("persistent_notification", "create", create_notification)
        self.hass.services.async_register(
            "persistent_notification", "dismiss", dismiss_notification
        )
        self.hass.states.async_set(GRID, "on")
        self.hass.states.async_set(LOAD, "2710", {"unit_of_measurement": "W"})
        self.hass.states.async_set(DEVICE, "on")
        model = PowerModel()
        model.add_device(ManagedDevice("load", "Test load", DEVICE, expected_power=1000))
        self.backend = Store(self.hass, STORAGE_VERSION, "power_orchestrator_runtime_test")
        self.store = RuntimeStore(self.backend)
        await self.store.async_load()
        policy = PolicyConfig.from_mapping(
            {"thresholds": [{"power_limit": 6000.0, "duration_s": 300.0}]}
        )
        assert policy is not None
        self.config = CoordinatorConfig(
            load_sensor=LOAD,
            averaging_period=10,
            pause_period=60,
            grid_loss_mode="grid_loss_sensor",
            grid_loss_sensor=GRID,
            policy=policy,
            entry_id="test-entry",
        )
        self.control = PowerOrchestratorCoordinator(self.hass, model, self.store, self.config)
        self.control.mode = "auto"
        await self.control.async_refresh()
        self.assertEqual(self.control.data["status"], "monitoring")
        self.assertEqual(self.commands, [])

    async def asyncTearDown(self):
        await self.hass.async_stop(force=True)
        self.directory.cleanup()

    def report(self, entity: str, value: str | None, *, unit: str = "W") -> None:
        previous = self.hass.states.get(entity)
        previous_at = previous.last_reported.timestamp() if previous else None
        if value is None:
            self.hass.states.async_remove(entity)
        else:
            self.hass.states.async_set(
                entity, value, {"unit_of_measurement": unit}, force_update=True
            )
        self.control.observe_entity_report(entity, self.hass.states.get(entity), previous_at)

    async def test_missing_load_preserves_the_device_and_reports_the_fault(self):
        self.report(LOAD, "unavailable")
        await self.control.async_refresh()

        self.assertEqual(self.commands, [])
        self.assertEqual(self.hass.states.get(DEVICE).state, "on")
        self.assertEqual(self.control.data["status"], "safety_blocked")
        self.assertEqual(self.control.data["telemetry_fault_reason"], "load_unavailable")
        self.assertEqual(self.control.data["pending_restore_ids"], [])
        self.assertTrue(self.notifications)
        message = next(iter(self.notifications.values()))
        self.assertNotIn("Emergency OFF", message)
        self.assertNotIn("explicit clear_fault", message)
        self.assertIn("automatically", message)

    async def test_fresh_load_automatically_clears_the_fault_and_its_saved_reason(self):
        from homeassistant.helpers.storage import Store
        from power_orchestrator.const import STORAGE_VERSION
        from power_orchestrator.storage import RuntimeStore

        self.report(LOAD, "unavailable")
        await self.control.async_refresh()
        self.assertTrue(self.control.data["telemetry_fault_latched"])
        self.report(LOAD, "2710")
        await self.control.async_refresh()

        self.assertFalse(self.control.data["telemetry_fault_latched"])
        self.assertIsNone(self.control.data["telemetry_fault_reason"])
        self.assertIsNone(self.control.data["safety_fault_reason"])
        self.assertEqual(self.control.data["status"], "monitoring")
        self.assertEqual(self.commands, [])
        self.assertEqual(self.notifications, {})
        reloaded = RuntimeStore(
            Store(self.hass, STORAGE_VERSION, "power_orchestrator_runtime_test")
        )
        await reloaded.async_load()
        self.assertEqual(reloaded.restore_telemetry_fault(), (False, None))

    async def test_recovered_reports_before_refresh_do_not_leave_an_active_fault(self):
        self.report(GRID, "unavailable")
        self.report(LOAD, "unavailable")
        self.report(GRID, "on")
        self.report(LOAD, "2710")
        await self.control.async_refresh()

        self.assertEqual(self.commands, [])
        self.assertFalse(self.control.data["telemetry_fault_latched"])
        self.assertIsNone(self.control.data["safety_fault_reason"])
        self.assertEqual(self.control.data["status"], "monitoring")
        self.assertEqual(self.notifications, {})

    async def test_supply_data_loss_restarts_continuous_overload_evidence(self):
        now = 100.0
        clock = SimpleNamespace(time=time.time, monotonic=lambda: now)
        with patch("power_orchestrator.coordinator.time", clock):
            self.report(LOAD, "7000")
            await self.control.async_refresh()
            now = 399.0
            self.report(GRID, "unavailable")
            await self.control.async_refresh()
            now = 401.0
            self.report(GRID, "on")
            self.report(LOAD, "7000")
            await self.control.async_refresh()
            self.assertEqual(self.commands, [])

            now = 701.0
            self.report(LOAD, "7000")
            await self.control.async_refresh()
            self.assertEqual(self.commands, [("turn_off", DEVICE)])

    async def test_restart_during_loss_and_after_recovery_keeps_the_fault_cleared(self):
        from homeassistant.helpers.storage import Store
        from power_orchestrator.const import STORAGE_VERSION
        from power_orchestrator.coordinator import PowerOrchestratorCoordinator
        from power_orchestrator.power_model import ManagedDevice, PowerModel
        from power_orchestrator.storage import RuntimeStore

        self.report(LOAD, "unavailable")
        await self.control.async_refresh()
        for recovered in (False, True):
            store = RuntimeStore(
                Store(self.hass, STORAGE_VERSION, "power_orchestrator_runtime_test")
            )
            await store.async_load()
            model = PowerModel()
            model.add_device(ManagedDevice("load", "Test load", DEVICE, expected_power=1000))
            self.control = PowerOrchestratorCoordinator(self.hass, model, store, self.config)
            self.control.restore_telemetry_fault(
                *store.restore_telemetry_fault(),
                emergency_handled=store.restore_telemetry_emergency_handled(),
            )
            self.control.mode = "auto"
            await self.control.async_refresh()
            self.assertEqual(self.control.data["telemetry_fault_latched"], not recovered)
            self.assertEqual(self.commands, [])
            self.report(LOAD, "2710")
            await self.control.async_refresh()
            self.assertFalse(self.control.data["telemetry_fault_latched"])
            self.assertIsNone(self.control.data["telemetry_fault_reason"])
            self.assertEqual(self.control.data["status"], "monitoring")

    async def test_fresh_confirmed_supply_loss_clears_telemetry_fault_but_still_stops(self):
        self.report(GRID, "unavailable")
        self.report(LOAD, "unavailable")
        await self.control.async_refresh()
        self.report(GRID, "off")
        self.report(LOAD, "2710")
        await self.control.async_refresh()

        self.assertFalse(self.control.data["telemetry_fault_latched"])
        self.assertIsNone(self.control.data["telemetry_fault_reason"])
        self.assertEqual(self.control.data["status"], "grid_loss")
        self.assertEqual(self.commands, [("turn_off", DEVICE)])

    async def test_new_loss_during_recovery_notification_keeps_monitoring_blocked(self):
        self.report(LOAD, "unavailable")
        await self.control.async_refresh()

        async def dismiss_then_lose_data(call):
            self.notifications.pop(call.data["notification_id"], None)
            self.report(LOAD, "unavailable")

        self.hass.services.async_register(
            "persistent_notification", "dismiss", dismiss_then_lose_data
        )
        self.report(LOAD, "2710")
        await self.control.async_refresh()

        self.assertTrue(self.control.data["telemetry_fault_latched"])
        self.assertEqual(self.control.data["status"], "safety_blocked")
        self.assertFalse(self.control.data["load_sensor_valid"])
        self.assertEqual(self.commands, [])

    async def test_forced_evaluation_persists_automatic_fault_cleanup(self):
        from homeassistant.helpers.storage import Store
        from power_orchestrator.const import STORAGE_VERSION
        from power_orchestrator.storage import RuntimeStore

        self.report(LOAD, "unavailable")
        await self.control.async_force_evaluate()
        self.report(LOAD, "2710")
        await self.control.async_force_evaluate()
        reloaded = RuntimeStore(
            Store(self.hass, STORAGE_VERSION, "power_orchestrator_runtime_test")
        )
        await reloaded.async_load()
        self.assertEqual(reloaded.restore_telemetry_fault(), (False, None))
        self.assertEqual(self.commands, [])

    async def test_mode_changes_clear_only_telemetry_fault_and_persist_recovery(self):
        from homeassistant.helpers.storage import Store
        from power_orchestrator.const import STORAGE_VERSION
        from power_orchestrator.storage import RuntimeStore

        for mode in ("observe", "off", "auto"):
            with self.subTest(mode=mode):
                self.report(LOAD, "unavailable")
                await self.control.async_refresh()
                self.report(LOAD, "2710")
                await self.control.async_set_mode(mode)
                self.assertEqual(self.control.data["mode"], mode)
                self.assertFalse(self.control.data["telemetry_fault_latched"])
                reloaded = RuntimeStore(
                    Store(self.hass, STORAGE_VERSION, "power_orchestrator_runtime_test")
                )
                await reloaded.async_load()
                self.assertEqual(reloaded.restore_telemetry_fault(), (False, None))
        self.assertEqual(self.commands, [])

    async def test_legacy_telemetry_restore_ticket_cannot_turn_a_device_on_after_recovery(self):
        from power_orchestrator.requests import RestoreIntent, RestoreTicket

        self.hass.states.async_set(DEVICE, "off", force_update=True)
        deadline = time.time() + 3600
        self.control.restore_restore_tickets(
            {
                "load": RestoreTicket(
                    "load", "load_unavailable", "old-action", deadline - 3610, deadline
                )
            }
        )
        self.control.restore_requests({("load", "test"): RestoreIntent("test", True, deadline)})
        self.control.restore_telemetry_fault(True, "load_unavailable", emergency_handled=True)
        now = 100.0
        with patch(
            "power_orchestrator.coordinator.time",
            SimpleNamespace(time=time.time, monotonic=lambda: now),
        ):
            await self.control.async_refresh()
            self.assertFalse(self.control.data["telemetry_fault_latched"])
            now = 161.0
            self.report(LOAD, "2710")
            await self.control.async_refresh()
        self.assertEqual(self.commands, [])
        self.assertEqual(self.hass.states.get(DEVICE).state, "off")

    async def test_invalid_reports_and_partial_recovery_preserve_physical_state(self):
        cases = (
            (GRID, None, "W"),
            (GRID, "unavailable", "W"),
            (GRID, "unknown", "W"),
            (GRID, "not-a-source", "W"),
            (LOAD, "unavailable", "W"),
            (LOAD, None, "W"),
            (LOAD, "unknown", "W"),
            (LOAD, "nan", "W"),
            (LOAD, "-1", "W"),
            (LOAD, "not-a-number", "W"),
            (LOAD, "2710", "A"),
        )
        for entity, value, unit in cases:
            with self.subTest(entity=entity, value=value, unit=unit):
                self.report(entity, value, unit=unit)
                await self.control.async_refresh()
                self.assertEqual(self.control.data["status"], "safety_blocked")
                self.assertEqual(self.commands, [])
                self.report(GRID, "on")
                self.report(LOAD, "2710")
                await self.control.async_refresh()
                self.assertFalse(self.control.data["telemetry_fault_latched"])

        self.report(GRID, "unavailable")
        self.report(LOAD, "unavailable")
        await self.control.async_refresh()
        self.report(LOAD, "2710")
        await self.control.async_refresh()
        self.assertTrue(self.control.data["telemetry_fault_latched"])
        self.assertEqual(self.commands, [])

    async def test_stale_load_for_a_day_only_notifies_once_and_then_recovers(self):
        import power_orchestrator.telemetry as telemetry

        reported_at = self.hass.states.get(LOAD).last_reported.timestamp()
        for seconds in (181, 3600, 86400):
            with (
                self.subTest(seconds=seconds),
                patch.object(
                    telemetry, "time", SimpleNamespace(time=lambda: reported_at + seconds)
                ),
            ):
                await self.control.async_refresh()
                await self.control.async_refresh()
                self.assertEqual(self.commands, [])
                self.assertEqual(self.control.data["telemetry_fault_reason"], "load_stale")
                self.assertIsNone(self.control.data["current_load"])
                self.assertEqual(self.notification_creates, 1)
        self.report(LOAD, "2710")
        await self.control.async_refresh()
        self.assertFalse(self.control.data["telemetry_fault_latched"])
        self.report(LOAD, "unavailable")
        await self.control.async_refresh()
        self.assertEqual(self.notification_creates, 2)

    async def test_recovery_preserves_actuator_quarantine_and_unknown_persisted_faults(self):
        self.control.restore_device_runtime(
            {"load"}, {"load"}, fault_reasons={"load": "relay_readback_timeout"}
        )
        self.report(LOAD, "unavailable")
        await self.control.async_refresh()
        self.report(LOAD, "2710")
        await self.control.async_refresh()
        self.assertFalse(self.control.data["telemetry_fault_latched"])
        self.assertEqual(self.control.data["quarantined_devices"], ["load"])
        self.assertEqual(self.control.data["fault_reasons"], {"load": "relay_readback_timeout"})

        self.control.restore_telemetry_fault(True, "unclassified_legacy_fault")
        await self.control.async_refresh()
        self.assertTrue(self.control.data["telemetry_fault_latched"])
        self.assertEqual(self.control.data["telemetry_fault_reason"], "unclassified_legacy_fault")
        self.assertFalse(self.control.data["physical_commands_allowed"])
        self.assertEqual(self.commands, [])

    async def test_failed_notification_dismissal_does_not_keep_the_fault_active(self):
        self.report(LOAD, "unavailable")
        await self.control.async_refresh()

        async def failed_dismissal(_call):
            raise RuntimeError("Synthetic notification delivery failure")

        self.hass.services.async_register("persistent_notification", "dismiss", failed_dismissal)
        self.report(LOAD, "2710")
        await self.control.async_refresh()
        self.assertFalse(self.control.data["telemetry_fault_latched"])
        self.assertIsNone(self.control.data["telemetry_fault_reason"])
        self.assertEqual(self.control.data["status"], "monitoring")
        self.assertEqual(self.commands, [])

        async def successful_dismissal(call):
            self.notifications.pop(call.data["notification_id"], None)

        self.hass.services.async_register(
            "persistent_notification", "dismiss", successful_dismissal
        )
        await self.control.async_refresh()
        self.assertEqual(self.notifications, {})
        self.assertEqual(self.commands, [])

    async def test_mobile_delivery_failure_does_not_orphan_or_repeat_the_ha_notification(self):
        async def failed_mobile_delivery(_call):
            raise RuntimeError("Synthetic mobile delivery failure")

        self.hass.services.async_register(
            "notify", "mobile_app_iphone_rostyslav_pro", failed_mobile_delivery
        )
        self.report(LOAD, "unavailable")
        await self.control.async_refresh()
        await self.control.async_refresh()
        self.assertEqual(self.notification_creates, 1)
        self.assertTrue(self.notifications)
        self.report(LOAD, "2710")
        await self.control.async_refresh()
        self.assertEqual(self.notifications, {})
        self.assertFalse(self.control.data["telemetry_fault_latched"])
        self.assertEqual(self.control.data["status"], "monitoring")
        self.assertEqual(self.commands, [])

    async def test_new_loss_during_notification_retry_is_published_as_blocked(self):
        self.report(LOAD, "unavailable")
        await self.control.async_refresh()

        async def failed_dismissal(_call):
            raise RuntimeError("Synthetic notification delivery failure")

        self.hass.services.async_register("persistent_notification", "dismiss", failed_dismissal)
        self.report(LOAD, "2710")
        await self.control.async_refresh()
        self.assertEqual(self.control.data["status"], "monitoring")

        async def dismiss_then_lose_data(call):
            self.notifications.pop(call.data["notification_id"], None)
            self.report(LOAD, "unavailable")

        self.hass.services.async_register(
            "persistent_notification", "dismiss", dismiss_then_lose_data
        )
        await self.control.async_refresh()
        self.assertEqual(self.control.data["status"], "safety_blocked")
        self.assertFalse(self.control.data["load_sensor_valid"])
        self.assertIsNone(self.control.data["current_load"])
        self.assertEqual(self.control.data["safety_fault_reason"], "load_unavailable")
        self.assertTrue(self.notifications)
        self.assertEqual(self.commands, [])

    async def test_invalid_notification_translation_cannot_trigger_an_emergency_stop(self):
        from unittest.mock import AsyncMock

        translations = {
            "component.power_orchestrator.issues.telemetry_unavailable.description": "Bad {missing}"
        }
        with patch(
            "homeassistant.helpers.translation.async_get_translations",
            AsyncMock(return_value=translations),
        ):
            self.report(LOAD, "unavailable")
            await self.control.async_refresh()
        self.assertEqual(self.commands, [])
        self.assertEqual(self.control.data["telemetry_fault_reason"], "load_unavailable")
        self.assertTrue(self.notifications)

    async def test_loss_while_saving_off_intent_withdraws_dispatch_without_quarantine(self):
        original_save = self.backend.async_save
        invalidated = False

        async def save_with_new_loss(data):
            nonlocal invalidated
            if not invalidated and any(
                row.get("action") == "turn_off" and row.get("phase") == "prepared"
                for row in data.get("audit_history", [])
            ):
                invalidated = True
                self.report(LOAD, "unavailable")
            await original_save(data)

        now = 100.0
        with (
            patch.object(self.backend, "async_save", save_with_new_loss),
            patch(
                "power_orchestrator.coordinator.time",
                SimpleNamespace(time=time.time, monotonic=lambda: now),
            ),
        ):
            self.report(LOAD, "7000")
            await self.control.async_refresh()
            now = 401.0
            self.report(LOAD, "7000")
            await self.control.async_refresh()

        self.assertTrue(invalidated)
        self.assertEqual(self.commands, [])
        self.assertEqual(self.control.data["quarantined_devices"], [])
        self.assertEqual(self.control.data["pending_restore_ids"], [])
        self.assertTrue(self.control.data["telemetry_fault_latched"])

    async def defer_emergency_dispatch_with_source_loss(self):
        original_save = self.backend.async_save
        invalidated = False

        async def save_with_source_loss(data):
            nonlocal invalidated
            if not invalidated and any(
                row.get("action") == "turn_off" and row.get("phase") == "prepared"
                for row in data.get("audit_history", [])
            ):
                invalidated = True
                self.report(GRID, "unavailable")
            await original_save(data)

        with patch.object(self.backend, "async_save", save_with_source_loss):
            self.report(GRID, "off")
            await self.control.async_refresh()
        self.assertTrue(invalidated)
        self.assertEqual(self.commands, [])
        self.assertEqual(self.control.data["quarantined_devices"], [])
        self.assertEqual(self.control.data["pending_restore_ids"], [])

    async def test_supply_report_loss_before_emergency_dispatch_does_not_quarantine(self):
        await self.defer_emergency_dispatch_with_source_loss()
        self.report(GRID, "off")
        self.report(LOAD, "2710")
        await self.control.async_refresh()
        self.assertEqual(self.commands, [("turn_off", DEVICE)])
        self.assertFalse(self.control.data["telemetry_fault_latched"])
        await self.control.async_refresh()
        self.assertEqual(self.commands, [("turn_off", DEVICE)])

    async def test_deferred_emergency_stop_is_not_replayed_when_supply_returns_on(self):
        await self.defer_emergency_dispatch_with_source_loss()
        self.report(GRID, "on")
        self.report(LOAD, "2710")
        await self.control.async_refresh()
        self.assertEqual(self.commands, [])
        self.assertEqual(self.control.data["status"], "monitoring")
        self.assertEqual(self.control.data["pending_restore_ids"], [])

    async def test_recovery_does_not_clear_post_shed_causal_fence(self):
        now = 100.0
        with patch(
            "power_orchestrator.coordinator.time",
            SimpleNamespace(time=time.time, monotonic=lambda: now),
        ):
            self.report(LOAD, "7000")
            await self.control.async_refresh()
            now = 401.0
            self.report(LOAD, "7000")
            await self.control.async_refresh()
            self.assertTrue(self.control.data["shed_barrier_pending"])
            self.report(GRID, "unavailable")
            await self.control.async_refresh()
            self.report(GRID, "on")
            await self.control.async_refresh()
        self.assertFalse(self.control.data["telemetry_fault_latched"])
        self.assertTrue(self.control.data["shed_barrier_pending"])
        self.assertFalse(self.control.data["restore_commands_allowed"])
        self.assertEqual(self.commands, [("turn_off", DEVICE)])

    async def test_storage_failure_during_cleanup_keeps_the_system_blocked(self):
        from unittest.mock import AsyncMock

        self.report(LOAD, "unavailable")
        await self.control.async_refresh()
        self.report(LOAD, "2710")
        with patch.object(
            self.backend, "async_save", AsyncMock(side_effect=OSError("Save failed"))
        ):
            await self.control.async_refresh()
        self.assertEqual(self.control.data["status"], "safety_blocked")
        self.assertTrue(self.control.data["journal_persistence_blocked"])
        self.assertEqual(self.commands, [])
        await self.control.async_refresh()
        self.assertFalse(self.control.data["journal_persistence_blocked"])
        self.assertFalse(self.control.data["telemetry_fault_latched"])

    async def test_new_loss_during_recovery_save_is_blocked_notified_and_persisted(self):
        for update in (self.control.async_refresh, self.control.async_force_evaluate):
            with self.subTest(update=update.__name__):
                self.report(LOAD, "unavailable")
                await self.control.async_refresh()
                original_save = self.backend.async_save
                invalidated = False

                async def save_with_new_loss(data):
                    nonlocal invalidated
                    if not invalidated and not data["telemetry_fault"]["latched"]:
                        invalidated = True
                        self.report(LOAD, "unavailable")
                    await original_save(data)

                self.report(LOAD, "2710")
                with patch.object(self.backend, "async_save", save_with_new_loss):
                    await update()
                self.assertTrue(invalidated)
                self.assertEqual(self.control.data["status"], "safety_blocked")
                self.assertFalse(self.control.data["load_sensor_valid"])
                self.assertIsNone(self.control.data["current_load"])
                self.assertEqual(self.control.data["safety_fault_reason"], "load_unavailable")
                self.assertEqual(self.store.restore_telemetry_fault(), (True, "load_unavailable"))
                self.assertTrue(self.notifications)
                self.assertEqual(self.commands, [])
                self.report(LOAD, "2710")
                await update()
                self.assertEqual(self.control.data["status"], "monitoring")
                self.assertEqual(self.store.restore_telemetry_fault(), (False, None))
                self.assertEqual(self.notifications, {})

    async def test_cold_start_with_missing_data_after_grace_preserves_the_device(self):
        from power_orchestrator.const import STARTUP_TELEMETRY_GRACE_SECONDS
        from power_orchestrator.coordinator import PowerOrchestratorCoordinator
        from power_orchestrator.power_model import ManagedDevice, PowerModel

        model = PowerModel()
        model.add_device(ManagedDevice("load", "Test load", DEVICE))
        self.control = PowerOrchestratorCoordinator(self.hass, model, self.store, self.config)
        self.control.mode = "auto"
        self.report(LOAD, None)
        elapsed = time.monotonic() + STARTUP_TELEMETRY_GRACE_SECONDS + 1
        with patch(
            "power_orchestrator.coordinator.time",
            SimpleNamespace(time=time.time, monotonic=lambda: elapsed),
        ):
            await self.control.async_config_entry_first_refresh()
        self.assertEqual(self.commands, [])
        self.assertEqual(self.control.data["status"], "safety_blocked")
        self.assertTrue(self.notifications)
        self.report(LOAD, "2710")
        await self.control.async_refresh()
        self.assertFalse(self.control.data["telemetry_fault_latched"])


if __name__ == "__main__":
    unittest.main()
