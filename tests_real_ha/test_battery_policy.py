"""Per-load battery policy during grid loss, against real Home Assistant core.

Runs with plain unittest so it works in an isolated Core environment without
pytest-homeassistant-custom-component:

    python -m unittest tests_real_ha.test_battery_policy
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components"))

GRID = "binary_sensor.managed_power_source_available"
SOC = "sensor.cerbo_gx_dc_battery_charge"
LOAD = "sensor.inverter_output_power"
BOILER = "switch.boiler"
HEATING = "switch.shelter_heating_plug"
DEHUMIDIFIER = "humidifier.shelter_dehumidifier"


def entry_data(**overrides):
    heating = {
        "device_id": "shelter_heating",
        "name": "Shelter heating",
        "entity": HEATING,
        "expected_power": 100,
        "priority": 7,
        "battery_min_soc": 40,
    }
    dehumidifier = {
        "device_id": "shelter_dehumidifier",
        "name": "Shelter dehumidifier",
        "entity": DEHUMIDIFIER,
        "expected_power": 400,
        "priority": 5,
        "battery_min_soc": 40,
    }
    boiler = {
        "device_id": "boiler",
        "name": "Boiler",
        "entity": BOILER,
        "expected_power": 2000,
        "priority": 1,
    }
    data = {
        "load_sensor": LOAD,
        "grid_loss_mode": "grid_loss_sensor",
        "grid_loss_sensor": GRID,
        "battery_soc": SOC,
        "pause_period": 60,
        "thresholds": [{"power_limit": 6000.0, "duration_s": 300.0}],
        "devices": [boiler, dehumidifier, heating],
    }
    data.update(overrides)
    return data


class BatteryPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from homeassistant.core import HomeAssistant
        from homeassistant.helpers import frame

        self.directory = tempfile.TemporaryDirectory(prefix="po-battery-")
        self.hass = HomeAssistant(self.directory.name)
        frame.async_setup(self.hass)
        self.off_calls: list[str] = []

        async def turn_off(call):
            for entity in self._targets(call):
                self.off_calls.append(entity)
                self.hass.states.async_set(entity, "off", force_update=True)

        async def turn_on(call):
            for entity in self._targets(call):
                self.hass.states.async_set(entity, "on", force_update=True)

        for domain in ("switch", "humidifier"):
            self.hass.services.async_register(domain, "turn_off", turn_off)
            self.hass.services.async_register(domain, "turn_on", turn_on)
        self.hass.services.async_register("persistent_notification", "create", lambda call: None)
        self.hass.services.async_register("persistent_notification", "dismiss", lambda call: None)

    async def asyncTearDown(self):
        await self.hass.async_stop(force=True)
        self.directory.cleanup()

    @staticmethod
    def _targets(call):
        entities = call.data.get("entity_id", [])
        return [entities] if isinstance(entities, str) else list(entities)

    def set_supply(self, grid: str, soc: str | float):
        self.hass.states.async_set(GRID, grid, force_update=True)
        self.hass.states.async_set(
            SOC, str(soc), {"unit_of_measurement": "%"}, force_update=True
        )
        self.hass.states.async_set(
            LOAD, "1200", {"unit_of_measurement": "W"}, force_update=True
        )

    async def start(self, data=None, *, soc: str | float = 80):
        from homeassistant.helpers.storage import Store
        from power_orchestrator import _build_model
        from power_orchestrator.coordinator import CoordinatorConfig, PowerOrchestratorCoordinator
        from power_orchestrator.policy import PolicyConfig
        from power_orchestrator.storage import RuntimeStore

        data = data or entry_data()
        for entity in (BOILER, HEATING, DEHUMIDIFIER):
            self.hass.states.async_set(entity, "on")
        self.set_supply("on", soc)
        store = RuntimeStore(Store(self.hass, 3, "power_orchestrator_runtime_test"))
        await store.async_load()
        self.coordinator = PowerOrchestratorCoordinator(
            self.hass,
            _build_model(data),
            store,
            CoordinatorConfig(
                load_sensor=data["load_sensor"],
                averaging_period=10,
                pause_period=data["pause_period"],
                grid_loss_mode=data["grid_loss_mode"],
                grid_loss_sensor=data["grid_loss_sensor"],
                battery_soc_sensor=data.get("battery_soc"),
                policy=PolicyConfig.from_mapping(data),
            ),
        )
        await self.coordinator.async_set_mode("auto")
        self.assertEqual(self.coordinator.status, "monitoring")

    async def evaluate(self):
        await self.coordinator.async_force_evaluate()
        await self.hass.async_block_till_done()

    def state(self, entity):
        return self.hass.states.get(entity).state

    async def test_grid_loss_keeps_battery_policy_loads_at_the_minimum_charge(self):
        await self.start(soc=40)
        self.set_supply("off", 40)
        await self.evaluate()

        self.assertEqual(self.state(BOILER), "off")
        self.assertEqual(self.state(HEATING), "on")
        self.assertEqual(self.state(DEHUMIDIFIER), "on")
        self.assertEqual(self.coordinator.data["pending_restore_ids"], ["boiler"])

    async def test_charge_falling_below_minimum_stops_policy_loads_without_a_restore_ticket(self):
        await self.start(soc=45)
        self.set_supply("off", 45)
        await self.evaluate()
        self.assertEqual(self.off_calls, [BOILER])

        self.set_supply("off", 39.9)
        await self.evaluate()

        self.assertEqual(self.state(HEATING), "off")
        self.assertEqual(self.state(DEHUMIDIFIER), "off")
        self.assertEqual(self.coordinator.data["pending_restore_ids"], ["boiler"])
        self.assertEqual(self.coordinator.data["faulted_devices"], [])

    async def test_a_load_turned_back_on_below_the_minimum_is_stopped_again(self):
        await self.start(soc=30)
        self.set_supply("off", 30)
        await self.evaluate()
        self.assertEqual(self.state(HEATING), "off")

        self.hass.states.async_set(HEATING, "on", force_update=True)
        self.set_supply("off", 30)
        await self.evaluate()

        self.assertEqual(self.state(HEATING), "off")
        self.assertEqual(self.off_calls.count(HEATING), 2)

    async def test_policy_loads_are_left_for_their_owner_after_charge_recovers(self):
        await self.start(soc=30)
        self.set_supply("off", 30)
        await self.evaluate()
        self.assertEqual(self.state(DEHUMIDIFIER), "off")

        self.set_supply("off", 60)
        await self.evaluate()
        self.hass.states.async_set(DEHUMIDIFIER, "on", force_update=True)
        await self.evaluate()

        self.assertEqual(self.state(DEHUMIDIFIER), "on")
        self.assertEqual(self.off_calls.count(DEHUMIDIFIER), 1)

    async def test_manual_start_of_a_shed_policy_load_on_battery_is_accepted(self):
        data = entry_data(thresholds=[{"power_limit": 6000.0, "duration_s": 0.0}])
        data["devices"] = [data["devices"][2]]
        await self.start(data)
        self.hass.states.async_set(LOAD, "7000", {"unit_of_measurement": "W"}, force_update=True)
        await self.evaluate()
        self.assertEqual(self.coordinator.data["pending_restore_ids"], ["shelter_heating"])

        self.set_supply("off", 80)
        await self.evaluate()
        self.hass.states.async_set(HEATING, "on", force_update=True)
        await self.evaluate()

        self.assertEqual(self.state(HEATING), "on")
        self.assertEqual(self.coordinator.data["pending_restore_ids"], [])

    async def test_unusable_charge_reading_stops_policy_loads(self):
        await self.start()
        self.set_supply("off", "unavailable")
        await self.evaluate()

        self.assertEqual(self.state(HEATING), "off")
        self.assertEqual(self.state(DEHUMIDIFIER), "off")

    async def test_without_a_charge_sensor_grid_loss_stops_every_load(self):
        data = entry_data()
        del data["battery_soc"]
        await self.start(data)
        self.set_supply("off", 90)
        await self.evaluate()

        self.assertEqual(self.state(HEATING), "off")
        self.assertEqual(self.state(DEHUMIDIFIER), "off")
        self.assertEqual(
            sorted(self.coordinator.data["pending_restore_ids"]),
            ["boiler", "shelter_dehumidifier", "shelter_heating"],
        )

    async def test_observe_mode_sends_no_battery_policy_commands(self):
        await self.start(soc=30)
        await self.coordinator.async_set_mode("observe")
        self.set_supply("off", 30)
        await self.evaluate()
        await self.evaluate()

        self.assertEqual(self.off_calls, [])


class BatteryPolicyConfigTests(unittest.TestCase):
    def test_runtime_model_reads_the_minimum_charge_per_load(self):
        from power_orchestrator import _build_model

        model = _build_model(entry_data())
        self.assertEqual(model.get_device("shelter_heating").battery_min_soc, 40.0)
        self.assertIsNone(model.get_device("boiler").battery_min_soc)

    def test_runtime_model_ignores_an_invalid_minimum_charge(self):
        from power_orchestrator import _build_model

        data = entry_data()
        data["devices"][2]["battery_min_soc"] = 140
        self.assertIsNone(_build_model(data).get_device("shelter_heating").battery_min_soc)

    def test_options_keep_the_minimum_charge_and_charge_sensor(self):
        from types import SimpleNamespace

        from power_orchestrator.config_flow import _prepare_options_submission

        entry = SimpleNamespace(data=entry_data(), options={})
        prepared, _, errors = _prepare_options_submission(entry, {})
        self.assertEqual(errors, {})
        self.assertEqual(prepared["battery_soc"], SOC)
        by_id = {device["device_id"]: device for device in prepared["devices"]}
        self.assertEqual(by_id["shelter_dehumidifier"]["battery_min_soc"], 40)
        self.assertNotIn("battery_min_soc", by_id["boiler"])

    def test_options_reject_an_invalid_minimum_charge(self):
        from types import SimpleNamespace

        from power_orchestrator.config_flow import _prepare_options_submission

        data = entry_data()
        data["devices"][1]["battery_min_soc"] = "high"
        prepared, _, errors = _prepare_options_submission(SimpleNamespace(data=data, options={}), {})
        self.assertIsNone(prepared)
        self.assertEqual(errors, {"base": "invalid_devices"})

    def test_options_require_a_charge_sensor_for_the_policy(self):
        from types import SimpleNamespace

        from power_orchestrator.config_flow import _prepare_options_submission

        data = entry_data()
        del data["battery_soc"]
        prepared, _, errors = _prepare_options_submission(SimpleNamespace(data=data, options={}), {})
        self.assertIsNone(prepared)
        self.assertEqual(errors, {"base": "missing_battery_soc_sensor"})

    def test_migration_keeps_the_minimum_charge(self):
        from power_orchestrator import _clean_migration_devices

        data = entry_data()
        _clean_migration_devices(data)
        self.assertEqual(data["devices"][2]["battery_min_soc"], 40)


if __name__ == "__main__":
    unittest.main()
