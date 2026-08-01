"""Tests for config_flow.py — mock HA dependencies."""
import sys, os
from types import SimpleNamespace
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components"))

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import voluptuous as vol

from power_orchestrator.config_flow import (
    PowerOrchestratorConfigFlow,
    _discover_energy,
    _entity_id,
    _friendly,
    _normalize_options_devices,
    _parse_threshold_input,
    _sensor_entity_id,
    _threshold_defaults,
    _threshold_field,
    _threshold_form_fields,
)
from power_orchestrator.const import (
    CONF_DEVICE_ENTITY, CONF_DEVICE_NAME, CONF_DEVICE_EXPECTED_POWER,
    CONF_DEVICE_POWER_SENSOR, CONF_DEVICE_ONLY_SOLAR, CONF_DEVICE_ACTUATORS,
    CONF_DEVICE_ID, CONF_DEVICE_HVAC_MODE_ON, CONF_PRIORITY, CONF_SHED_PRIORITY,
    CONF_RESTORE_PRIORITY, CONF_PAUSE_PERIOD,
    CONF_GRID_LOSS_MODE, CONF_GRID_LOSS_SENSOR, CONF_BATTERY_THRESHOLD,
    CONF_LOAD_SENSOR, CONF_THRESHOLDS, CONF_THRESHOLD_COUNT,
)


# ── _friendly tests ────────────────────────────────────────────────


def test_friendly_with_state():
    hass = MagicMock()
    state = MagicMock()
    state.attributes = {"friendly_name": "My Sensor"}
    hass.states.get.return_value = state
    assert _friendly(hass, "sensor.test") == "My Sensor"


def test_friendly_without_state():
    hass = MagicMock()
    hass.states.get.return_value = None
    assert _friendly(hass, "sensor.test") == "sensor.test"


def test_friendly_empty_name_falls_back_to_entity_id():
    hass = MagicMock()
    state = MagicMock()
    state.attributes = {"friendly_name": ""}
    hass.states.get.return_value = state
    assert _friendly(hass, "sensor.test") == "sensor.test"


def test_friendly_empty():
    hass = MagicMock()
    assert _friendly(hass, "") == ""


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("sensor.load", "sensor.load"),
        (" sensor.load ", "sensor.load"),
        (None, None),
        ("switch.load", None),
        ("sensor.", None),
        ("sensor", None),
        ("", None),
    ],
)
def test_sensor_entity_id_normalization(value, expected):
    assert _sensor_entity_id(value) == expected


@pytest.mark.parametrize(
    ("value", "domains", "expected"),
    [
        ("switch.load", frozenset({"switch"}), "switch.load"),
        (" switch.load ", frozenset({"switch"}), "switch.load"),
        ("sensor.load", frozenset({"switch"}), None),
        ("switch", frozenset({"switch"}), None),
        ("switch.", frozenset({"switch"}), None),
        (None, frozenset({"switch"}), None),
    ],
)
def test_entity_id_normalization(value, domains, expected):
    assert _entity_id(value, domains) == expected


def test_threshold_helpers_cover_defaults_aliases_and_form_shape():
    defaults = _threshold_defaults(
        [
            {"limit_w": "6500", "time_s": "300"},
            {"power_limit": 7000, "duration_s": 30},
            {"power_limit": "bad", "duration_s": 5},
        ]
    )
    assert defaults[:2] == [
        {"power_limit": 6500.0, "duration_s": 300.0},
        {"power_limit": 7000.0, "duration_s": 30.0},
    ]
    assert _threshold_defaults(None)[0]["power_limit"] == 6500.0
    assert _threshold_defaults([])[-1]["duration_s"] == 5.0

    fields = _threshold_form_fields(defaults[:1])
    assert len(fields) == 21
    assert _threshold_field(10, "time") in [
        getattr(key, "schema", key) for key in fields
    ]


def test_threshold_parser_accepts_one_and_ten_tiers():
    one, error = _parse_threshold_input(
        {
            "threshold_count": 1,
            _threshold_field(1, "power"): 6500,
            _threshold_field(1, "time"): 300,
        }
    )
    assert error is None
    assert one == [{"power_limit": 6500.0, "duration_s": 300.0}]

    ten_input = {"threshold_count": 10}
    for index in range(1, 11):
        ten_input[_threshold_field(index, "power")] = 1000 + index
        ten_input[_threshold_field(index, "time")] = index
    ten, error = _parse_threshold_input(ten_input)
    assert error is None
    assert ten is not None and len(ten) == 10
    assert ten[-1]["power_limit"] == 1010.0


@pytest.mark.parametrize(
    "user_input",
    [
        {"threshold_count": True},
        {"threshold_count": "bad"},
        {"threshold_count": 1.5},
        {"threshold_count": 0},
        {"threshold_count": 11},
        {
            "threshold_count": 1,
            "threshold_1_power": 0,
            "threshold_1_time": 1,
        },
        {
            "threshold_count": 2,
            "threshold_1_power": 2000,
            "threshold_1_time": 1,
            "threshold_2_power": 1000,
            "threshold_2_time": 1,
        },
    ],
)
def test_threshold_parser_rejects_invalid_tiers(user_input):
    parsed, error = _parse_threshold_input(user_input)
    assert parsed is None
    assert error == "invalid_thresholds"


def _option_device(**overrides):
    """Build a valid structured OptionsFlow device mapping."""
    device = {
        CONF_DEVICE_ID: "device-1",
        CONF_DEVICE_NAME: " Boiler ",
        CONF_DEVICE_ENTITY: "switch.boiler",
        CONF_DEVICE_EXPECTED_POWER: 1800,
        CONF_DEVICE_POWER_SENSOR: "sensor.boiler_power",
        CONF_DEVICE_ONLY_SOLAR: True,
        CONF_DEVICE_ACTUATORS: ["climate.kitchen"],
        CONF_DEVICE_HVAC_MODE_ON: " heat ",
        CONF_PRIORITY: 2,
        CONF_SHED_PRIORITY: 3,
        CONF_RESTORE_PRIORITY: 4,
    }
    device.update(overrides)
    return device


def test_normalize_options_devices_accepts_structured_mapping():
    normalized = _normalize_options_devices([_option_device()])
    assert normalized == [
        {
            CONF_DEVICE_ID: "device-1",
            CONF_DEVICE_NAME: "Boiler",
            CONF_DEVICE_ENTITY: "switch.boiler",
            CONF_DEVICE_EXPECTED_POWER: 1800,
            CONF_DEVICE_POWER_SENSOR: "sensor.boiler_power",
            CONF_DEVICE_ONLY_SOLAR: True,
            CONF_PRIORITY: 2,
            CONF_DEVICE_ACTUATORS: ["climate.kitchen"],
            CONF_DEVICE_HVAC_MODE_ON: "heat",
            CONF_SHED_PRIORITY: 3,
            CONF_RESTORE_PRIORITY: 4,
        }
    ]
    assert _normalize_options_devices([_option_device(**{CONF_DEVICE_ACTUATORS: None})])[0][
        CONF_DEVICE_ACTUATORS
    ] == []
    assert _normalize_options_devices([_option_device(**{CONF_DEVICE_POWER_SENSOR: None})])[0][
        CONF_DEVICE_POWER_SENSOR
    ] is None


@pytest.mark.parametrize(
    "override",
    [
        {CONF_DEVICE_ID: ""},
        {CONF_DEVICE_ENTITY: "sensor.boiler"},
        {CONF_DEVICE_ACTUATORS: object()},
        {CONF_DEVICE_ACTUATORS: ["switch.boiler"]},
        {CONF_DEVICE_ACTUATORS: ["climate.kitchen", "climate.kitchen"]},
        {CONF_DEVICE_EXPECTED_POWER: True},
        {CONF_DEVICE_EXPECTED_POWER: "bad"},
        {CONF_DEVICE_EXPECTED_POWER: float("inf")},
        {CONF_DEVICE_POWER_SENSOR: "switch.power"},
        {CONF_PRIORITY: True},
        {CONF_PRIORITY: "bad"},
        {CONF_PRIORITY: 1.5},
        {CONF_PRIORITY: 0},
        {CONF_SHED_PRIORITY: False},
        {CONF_SHED_PRIORITY: "bad"},
        {CONF_RESTORE_PRIORITY: 0},
        {CONF_DEVICE_ONLY_SOLAR: "yes"},
        {CONF_DEVICE_HVAC_MODE_ON: ""},
    ],
)
def test_normalize_options_devices_rejects_invalid_mapping(override):
    with pytest.raises(ValueError):
        _normalize_options_devices([_option_device(**override)])


def test_normalize_options_devices_rejects_duplicate_ids_priorities_and_entities():
    with pytest.raises(ValueError, match="list"):
        _normalize_options_devices(None)
    with pytest.raises(ValueError, match="object"):
        _normalize_options_devices([None])
    with pytest.raises(ValueError, match="unique"):
        _normalize_options_devices([_option_device(), _option_device()])
    with pytest.raises(ValueError, match="priorities"):
        _normalize_options_devices(
            [
                _option_device(),
                _option_device(
                    **{
                        CONF_DEVICE_ID: "device-2",
                        CONF_DEVICE_ENTITY: "switch.other",
                        CONF_DEVICE_ACTUATORS: ["climate.other"],
                    }
                ),
            ]
        )
    with pytest.raises(ValueError, match="control entities"):
        _normalize_options_devices(
            [
                _option_device(),
                _option_device(
                    **{
                        CONF_DEVICE_ID: "device-2",
                        CONF_PRIORITY: 5,
                        CONF_DEVICE_ENTITY: "switch.boiler",
                    }
                ),
            ]
        )


@pytest.mark.asyncio
async def test_user_step_allows_missing_optional_energy_grid_source():
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    with patch(
        "power_orchestrator.config_flow._discover_energy",
        new=AsyncMock(
            return_value={
                "grid_power": None,
                "solar_power": None,
                "solar_forecast_entity": None,
                "solar_forecast_entry": None,
                "battery_soc": None,
                "battery_power": None,
                "devices": [],
            }
        ),
    ):
        result = await flow.async_step_user()

    grid_key = next(
        key for key in result["data_schema"].schema if getattr(key, "schema", None) == "grid_power"
    )
    assert isinstance(grid_key, vol.Optional)


@pytest.mark.asyncio
async def test_user_step_aborts_when_singleton_entry_exists():
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    flow.hass.config_entries.async_entries.return_value = [object()]

    result = await flow.async_step_user()

    assert result == {"type": "abort", "reason": "single_instance"}


# ── _discover_energy tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_discover_energy_no_data():
    hass = MagicMock()
    async def mock_get_manager(hass):
        mgr = MagicMock()
        mgr.data = None
        return mgr
    with patch("homeassistant.components.energy.async_get_manager", mock_get_manager):
        result = await _discover_energy(hass)
        assert result["grid_power"] is None
        assert result["solar_power"] is None
        assert result["devices"] == []


@pytest.mark.asyncio
async def test_discover_energy_grid():
    hass = MagicMock()
    async def mock_get_manager(hass):
        mgr = MagicMock()
        mgr.data = {
            "energy_sources": [
                {"type": "grid", "stat_rate": "sensor.grid_power_l1", "power_config": {"stat_rate": "sensor.grid_power_l1"}}
            ],
            "device_consumption": [],
        }
        return mgr
    with patch("homeassistant.components.energy.async_get_manager", mock_get_manager):
        result = await _discover_energy(hass)
        assert result["grid_power"] == "sensor.grid_power_l1"


@pytest.mark.asyncio
async def test_discover_energy_solar():
    hass = MagicMock()
    async def mock_get_manager(hass):
        mgr = MagicMock()
        mgr.data = {
            "energy_sources": [
                {"type": "solar", "stat_rate": "sensor.pv_power", "config_entry_solar_forecast": ["entry_123"]}
            ],
            "device_consumption": [],
        }
        return mgr
    ce = MagicMock()
    ce.title = "Forecast.Solar"
    hass.config_entries.async_get_entry.return_value = ce
    with patch("homeassistant.components.energy.async_get_manager", mock_get_manager):
        result = await _discover_energy(hass)
        assert result["solar_power"] == "sensor.pv_power"


@pytest.mark.asyncio
async def test_discover_energy_battery():
    hass = MagicMock()
    async def mock_get_manager(hass):
        mgr = MagicMock()
        mgr.data = {
            "energy_sources": [
                {"type": "battery", "stat_soc": "sensor.battery_soc", "stat_rate": "sensor.battery_power"}
            ],
            "device_consumption": [],
        }
        return mgr
    with patch("homeassistant.components.energy.async_get_manager", mock_get_manager):
        result = await _discover_energy(hass)
        assert result["battery_soc"] == "sensor.battery_soc"
        assert result["battery_power"] == "sensor.battery_power"


@pytest.mark.asyncio
async def test_discover_energy_devices():
    hass = MagicMock()
    async def mock_get_manager(hass):
        mgr = MagicMock()
        mgr.data = {
            "energy_sources": [],
            "device_consumption": [
                {"stat_consumption": "sensor.boiler_energy", "stat_rate": "sensor.boiler_power", "name": "Boiler"},
                {"stat_consumption": "sensor.ac_energy", "name": "AC"},
            ],
        }
        return mgr
    with patch("homeassistant.components.energy.async_get_manager", mock_get_manager):
        result = await _discover_energy(hass)
        assert len(result["devices"]) == 2
        assert result["devices"][0]["entity_id"] == "sensor.boiler_energy"
        assert result["devices"][0]["power_sensor"] == "sensor.boiler_power"


@pytest.mark.asyncio
async def test_discover_energy_ignores_non_sensor_stat_rate():
    """A malformed Energy Dashboard rate must not become a sensor selector default."""
    hass = MagicMock()

    async def mock_get_manager(hass):
        mgr = MagicMock()
        mgr.data = {
            "energy_sources": [],
            "device_consumption": [
                {
                    "stat_consumption": "sensor.boiler_energy",
                    "stat_rate": "switch.boiler",
                    "name": "Boiler",
                }
            ],
        }
        return mgr

    with patch("homeassistant.components.energy.async_get_manager", mock_get_manager):
        result = await _discover_energy(hass)

    assert result["devices"] == [
        {
            "entity_id": "sensor.boiler_energy",
            "name": "Boiler",
            "power_sensor": None,
        }
    ]


@pytest.mark.asyncio
async def test_discover_energy_resolves_first_forecast_entity_that_exists():
    hass = MagicMock()

    async def mock_get_manager(hass):
        manager = MagicMock()
        manager.data = {
            "energy_sources": [
                {
                    "type": "solar",
                    "stat_rate": "sensor.pv_power",
                    "config_entry_solar_forecast": ["bad-entry", "good-entry"],
                }
            ],
            "device_consumption": [],
        }
        return manager

    with (
        patch("homeassistant.components.energy.async_get_manager", mock_get_manager),
        patch(
            "power_orchestrator.config_flow.resolve_current_power_forecast_entity",
            side_effect=[None, "sensor.forecast_power"],
        ),
    ):
        result = await _discover_energy(hass)

    assert result["solar_forecast_entry"] == "good-entry"
    assert result["solar_forecast_entity"] == "sensor.forecast_power"


@pytest.mark.asyncio
async def test_discover_energy_failure_degrades_to_empty_result():
    async def failing_get_manager(hass):
        raise RuntimeError("energy manager unavailable")

    with patch(
        "homeassistant.components.energy.async_get_manager", failing_get_manager
    ):
        result = await _discover_energy(MagicMock())

    assert result == {
        "grid_power": None,
        "solar_power": None,
        "solar_forecast_entity": None,
        "solar_forecast_entry": None,
        "battery_soc": None,
        "battery_power": None,
        "devices": [],
    }


@pytest.mark.asyncio
async def test_user_step_persists_explicit_discovery_inputs():
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    with patch(
        "power_orchestrator.config_flow.resolve_current_power_forecast_entity",
        return_value="sensor.forecast_power",
    ) as resolve:
        result = await flow.async_step_user(
            {
                "grid_power": "sensor.grid",
                "solar_power": "sensor.pv",
                "solar_forecast": "forecast-entry",
                "battery_soc": "sensor.soc",
                "battery_power": "sensor.battery_power",
            }
        )

    assert result["step_id"] == "load_monitoring"
    assert flow._discovered["grid_power"] == "sensor.grid"
    assert flow._discovered["solar_forecast_entity"] == "sensor.forecast_power"
    resolve.assert_called_once_with(flow.hass, "forecast-entry")


def test_discovery_summary_includes_all_sources_and_safe_fallbacks():
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    state = MagicMock()
    state.state = "55"
    state.attributes = {"friendly_name": "Battery"}
    flow.hass.states.get.return_value = state
    entry = MagicMock()
    entry.title = "Forecast Solar"
    flow.hass.config_entries.async_get_entry.return_value = entry
    flow._discovered = {
        "grid_power": "sensor.grid",
        "solar_power": "sensor.pv",
        "solar_forecast_entry": "forecast-entry",
        "solar_forecast_entity": "sensor.forecast",
        "battery_soc": "sensor.soc",
        "battery_power": "sensor.battery_power",
        "devices": [{"entity_id": "sensor.device", "name": "Device"}],
    }
    summary = flow._discovery_summary()
    assert "Grid power: sensor.grid" in summary
    assert "Solar power: sensor.pv" in summary
    assert "Solar estimated power: sensor.forecast (Forecast Solar)" in summary
    assert "Battery SoC: sensor.soc" in summary
    assert "Battery power: sensor.battery_power" in summary
    assert "Devices found: 1" in summary

    flow._discovered["solar_forecast_entity"] = None
    assert "no exact estimated-power entity resolved" in flow._discovery_summary()


# ── Config flow tests (only steps that don't build vol.Schema forms) ──


def test_config_flow_init():
    flow = PowerOrchestratorConfigFlow()
    assert flow.VERSION == 1
    assert hasattr(flow, "_discovered")
    assert hasattr(flow, "_devices")


@pytest.mark.asyncio
async def test_config_flow_reconfigure_exposes_existing_entry_settings():
    """Reconfigure is a first-class flow and does not require removing the entry."""
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    flow.context = {"entry_id": "entry-1"}
    entry = MagicMock()
    entry.data = {"load_sensor": "sensor.load", "max_load": 5000}
    entry.options = {}
    flow.hass.config_entries.async_get_entry.return_value = entry

    result = await flow.async_step_reconfigure()

    assert result["type"] == "form"
    assert result["step_id"] == "reconfigure"
    assert result["data_schema"] is not None


@pytest.mark.asyncio
async def test_config_flow_reconfigure_aborts_when_entry_is_missing():
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    flow.context = {"entry_id": "missing"}
    flow.hass.config_entries.async_get_entry.return_value = None

    result = await flow.async_step_reconfigure()

    assert result == {"type": "abort", "reason": "entry_not_loaded"}


@pytest.mark.asyncio
async def test_config_flow_reconfigure_validates_and_updates_existing_entry_data():
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    flow.context = {"entry_id": "entry-1"}
    entry = SimpleNamespace(
        data={
            "load_sensor": "sensor.old_load",
            "max_load": 5000,
            "averaging_period": 30,
            "safety_reserve": 200,
            "hysteresis": 100,
            "pause_period": 60,
            "grid_loss_mode": "grid_loss_sensor",
            "grid_loss_sensor": "binary_sensor.grid",
            "thresholds": [{"power_limit": 6500, "duration_s": 300}],
            "devices": [
                {
                    "device_id": "d1",
                    "name": "Device",
                    "entity": "switch.d1",
                    "expected_power": 1000,
                    "power_sensor": None,
                    "only_from_solar": False,
                    "priority": 1,
                    "shed_priority": 1,
                    "actuators": [],
                    "hvac_mode_on": "heat",
                }
            ],
        },
        options={},
    )
    flow.hass.config_entries.async_get_entry.return_value = entry

    result = await flow.async_step_reconfigure({"load_sensor": "sensor.new_load"})

    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    normalized = result["data"]
    assert normalized["load_sensor"] == "sensor.new_load"
    assert normalized["devices"][0]["device_id"] == "d1"
    assert normalized["grid_loss_sensor"] == "binary_sensor.grid"
    assert normalized["thresholds"] == [{"power_limit": 6500.0, "duration_s": 300.0}]


@pytest.mark.asyncio
async def test_config_flow_reconfigure_returns_form_for_invalid_submission():
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    flow.context = {"entry_id": "entry-1"}
    entry = SimpleNamespace(
        data={
            "load_sensor": "sensor.old_load",
            "grid_loss_mode": "grid_loss_sensor",
            "grid_loss_sensor": "binary_sensor.grid",
            "thresholds": [{"power_limit": 6500, "duration_s": 300}],
            "devices": [],
        },
        options={},
    )
    flow.hass.config_entries.async_get_entry.return_value = entry

    result = await flow.async_step_reconfigure({"load_sensor": "switch.not_a_sensor"})

    assert result["type"] == "form"
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "invalid_load_sensor"}


@pytest.mark.asyncio
async def test_config_flow_grid_loss_sensor_mode():
    """Test grid loss sensor mode creates entry correctly."""
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    flow._discovered = {"grid_power": "sensor.grid_power", "max_load": 5000}
    flow._devices = [{"device_id": "dev1", "entity": "switch.boiler"}]
    flow._pause_period = 60

    result = await flow.async_step_grid_loss(user_input={
        "grid_loss_mode": "grid_loss_sensor",
        "grid_loss_sensor": "binary_sensor.grid",
        "battery_threshold": 20,
    })
    assert result["type"] == "create_entry"
    assert result["data"]["load_sensor"] == "sensor.grid_power"
    assert result["data"]["grid_loss_sensor"] == "binary_sensor.grid"
    assert result["data"]["grid_loss_mode"] == "grid_loss_sensor"
    assert len(result["data"]["devices"]) == 1


@pytest.mark.asyncio
async def test_config_flow_grid_loss_battery_mode():
    """Test battery threshold mode creates entry correctly."""
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    flow._discovered = {"grid_power": "sensor.grid_power", "max_load": 5000}
    flow._devices = [{"device_id": "dev1", "entity": "switch.boiler"}]
    flow._pause_period = 60

    result = await flow.async_step_grid_loss(user_input={
        "grid_loss_mode": "battery_threshold",
        "grid_loss_sensor": "",
        "battery_soc": "sensor.battery_soc",
        "battery_threshold": 30,
    })
    assert result["type"] == "create_entry"
    assert result["data"]["grid_loss_mode"] == "battery_threshold"
    assert result["data"]["battery_threshold"] == 30
    # grid_loss_sensor should not be in data when using battery mode
    assert result["data"].get("grid_loss_sensor") is None


@pytest.mark.asyncio
async def test_grid_loss_sensor_mode_requires_sensor_source():
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    flow._discovered = {"grid_power": "sensor.grid_power"}
    flow._devices = [{"device_id": "dev1", "entity": "switch.boiler"}]
    flow._pause_period = 60

    result = await flow.async_step_grid_loss(
        user_input={"grid_loss_mode": "grid_loss_sensor", "grid_loss_sensor": ""}
    )

    assert result["type"] == "form"
    assert result["errors"]["base"] == "missing_grid_loss_sensor"


@pytest.mark.asyncio
async def test_battery_mode_requires_soc_source():
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    flow._discovered = {"grid_power": "sensor.grid_power"}
    flow._devices = [{"device_id": "dev1", "entity": "switch.boiler"}]
    flow._pause_period = 60

    result = await flow.async_step_grid_loss(
        user_input={"grid_loss_mode": "battery_threshold", "battery_threshold": 30}
    )

    assert result["type"] == "form"
    assert result["errors"]["base"] == "missing_battery_soc_sensor"


@pytest.mark.asyncio
async def test_priority_form_has_one_named_selector_per_device():
    """Priority UI must expose named device positions, not a text ID list."""
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    flow._devices = [
        {
            "device_id": "dev1",
            "entity": "switch.boiler",
            "name": "Bathroom boiler",
        },
        {
            "device_id": "dev2",
            "entity": "switch.dehumidifier",
            "name": "Shelter dehumidifier",
        },
    ]

    result = await flow.async_step_priority()

    schema = result["data_schema"].schema
    assert "priority_order" not in schema
    assert "priority_1" in schema
    assert "priority_2" in schema

    first_selector = schema["priority_1"]
    option_labels = [option.label for option in first_selector.config.options]
    assert option_labels == ["Bathroom boiler", "Shelter dehumidifier"]


@pytest.mark.asyncio
async def test_priority_submission_reorders_devices_by_selected_positions():
    """Selected position values must become the persisted 1..N priorities."""
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    flow._devices = [
        {"device_id": "dev1", "entity": "switch.boiler", "name": "Bathroom boiler"},
        {"device_id": "dev2", "entity": "switch.dehumidifier", "name": "Shelter dehumidifier"},
    ]

    result = await flow.async_step_priority(
        user_input={
            "priority_1": "dev2",
            "priority_2": "dev1",
            "pause_period": 90,
        }
    )

    assert result["step_id"] == "grid_loss"
    assert [device["device_id"] for device in flow._devices] == ["dev2", "dev1"]
    assert [device["priority"] for device in flow._devices] == [1, 2]
    assert flow._pause_period == 90


@pytest.mark.asyncio
async def test_devices_form_lists_discovered_candidates_with_friendly_names():
    """Step 3 must let the user confirm/remove discovered devices by name."""
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    flow._discovered = {
        "devices": [
            {
                "entity_id": "sensor.boiler_power",
                "name": "Bathroom boiler",
                "power_sensor": "sensor.boiler_power",
            },
            {
                "entity_id": "sensor.dehumidifier_power",
                "name": "Shelter dehumidifier",
                "power_sensor": "sensor.dehumidifier_power",
            },
        ]
    }

    result = await flow.async_step_devices()

    schema = result["data_schema"].schema
    discovered_selector = schema["discovered_devices"]
    assert discovered_selector.config.multiple is True
    assert [option.label for option in discovered_selector.config.options] == [
        "Bathroom boiler",
        "Shelter dehumidifier",
    ]
    assert "add_custom_device" in schema


def test_device_expected_power_must_be_positive():
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    result = flow._device_config_form()

    assert result["data_schema"].schema["expected_power"].config.min == 1


def _schema_field(schema, field_name):
    """Return a field validator while accepting vol.Optional keys."""
    for key, validator in schema.schema.items():
        if getattr(key, "schema", key) == field_name:
            return key, validator
    raise AssertionError(f"missing schema field: {field_name}")


def test_discovered_power_sensor_is_prefilled_and_overrideable():
    """Energy Dashboard stat_rate is a visible default, not a fixed choice."""
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    flow._discovered = {"devices": []}
    candidate = {
        "entity_id": "sensor.boiler_energy",
        "name": "Bathroom boiler",
        "power_sensor": "sensor.boiler_power",
    }

    result = flow._device_config_form(candidate)
    power_key, power_validator = _schema_field(
        result["data_schema"], CONF_DEVICE_POWER_SENSOR
    )

    default = power_key.default() if callable(power_key.default) else power_key.default
    assert default == "sensor.boiler_power"
    assert power_validator.config.domain == "sensor"
    assert "sensor.boiler_power" in result["description_placeholders"]["discovered"]

    custom = flow._build_device(
        {
            CONF_DEVICE_ENTITY: "switch.bathroom_boiler",
            CONF_DEVICE_NAME: "Bathroom boiler",
            CONF_DEVICE_EXPECTED_POWER: 1800,
            CONF_DEVICE_POWER_SENSOR: "sensor.custom_boiler_power",
        },
        candidate,
    )
    assert custom[CONF_DEVICE_POWER_SENSOR] == "sensor.custom_boiler_power"


def test_discovered_power_sensor_can_be_cleared_explicitly():
    """A user can disable an unsuitable auto-discovered sensor."""
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    candidate = {
        "entity_id": "sensor.boiler_energy",
        "name": "Bathroom boiler",
        "power_sensor": "sensor.boiler_power",
    }

    device = flow._build_device(
        {
            CONF_DEVICE_ENTITY: "switch.bathroom_boiler",
            CONF_DEVICE_EXPECTED_POWER: 1800,
            CONF_DEVICE_POWER_SENSOR: "",
        },
        candidate,
    )

    assert device[CONF_DEVICE_POWER_SENSOR] is None


@pytest.mark.asyncio
async def test_devices_selection_configures_only_confirmed_candidate():
    """Unselected candidates are removed; selected ones get an on/off entity form."""
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    flow._discovered = {
        "devices": [
            {
                "entity_id": "sensor.boiler_power",
                "name": "Bathroom boiler",
                "power_sensor": "sensor.boiler_power",
            },
            {
                "entity_id": "sensor.dehumidifier_power",
                "name": "Shelter dehumidifier",
                "power_sensor": "sensor.dehumidifier_power",
            },
        ]
    }

    result = await flow.async_step_devices(
        user_input={
            "discovered_devices": ["sensor.boiler_power"],
            "add_custom_device": False,
        }
    )
    assert result["type"] == "form"
    assert result["step_id"] == "devices"
    assert "entity" in result["data_schema"].schema

    result = await flow.async_step_devices(
        user_input={
            "entity": "switch.bathroom_boiler",
            "name": "",
            "expected_power": 1800,
            "power_sensor": "sensor.boiler_power",
            "only_from_solar": True,
        }
    )

    assert result["type"] == "form"
    assert result["step_id"] == "priority"
    assert len(flow._devices) == 1
    assert flow._devices[0]["entity"] == "switch.bathroom_boiler"
    assert flow._devices[0]["name"] == "Bathroom boiler"
    assert flow._devices[0]["power_sensor"] == "sensor.boiler_power"
    assert flow._devices[0]["only_from_solar"] is True


@pytest.mark.asyncio
async def test_devices_flow_allows_adding_a_custom_device_without_discovery():
    """Users can add a custom optional device when discovery found nothing."""
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    flow._discovered = {"devices": []}

    result = await flow.async_step_devices(
        user_input={
            "discovered_devices": [],
            "add_custom_device": True,
        }
    )
    assert result["type"] == "form"
    assert result["step_id"] == "devices"

    result = await flow.async_step_devices(
        user_input={
            "entity": "switch.custom_load",
            "name": "Custom load",
            "expected_power": 750,
            "power_sensor": "sensor.custom_power",
            "only_from_solar": False,
            "add_another": False,
        }
    )

    assert result["type"] == "form"
    assert result["step_id"] == "priority"
    assert len(flow._devices) == 1
    assert flow._devices[0]["name"] == "Custom load"
    assert flow._devices[0]["entity"] == "switch.custom_load"


@pytest.mark.asyncio
async def test_devices_flow_can_add_custom_device_after_discovered_devices():
    """Custom devices can be appended after all confirmed candidates are configured."""
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    flow._discovered = {
        "devices": [
            {
                "entity_id": "sensor.boiler_power",
                "name": "Bathroom boiler",
                "power_sensor": "sensor.boiler_power",
            }
        ]
    }

    await flow.async_step_devices(
        user_input={
            "discovered_devices": ["sensor.boiler_power"],
            "add_custom_device": True,
        }
    )
    await flow.async_step_devices(
        user_input={
            "entity": "switch.bathroom_boiler",
            "name": "",
            "expected_power": 1800,
            "power_sensor": "sensor.boiler_power",
            "only_from_solar": False,
        }
    )
    result = await flow.async_step_devices(
        user_input={
            "entity": "switch.custom_load",
            "name": "Custom load",
            "expected_power": 750,
            "power_sensor": "sensor.custom_power",
            "only_from_solar": False,
            "add_another": False,
        }
    )

    assert result["step_id"] == "priority"
    assert [device["name"] for device in flow._devices] == [
        "Bathroom boiler",
        "Custom load",
    ]


@pytest.mark.asyncio
async def test_devices_flow_rejects_unknown_discovered_candidate():
    """A submitted candidate must come from the current discovery result."""
    flow = PowerOrchestratorConfigFlow()
    flow.hass = MagicMock()
    flow._discovered = {
        "devices": [{"entity_id": "sensor.boiler_power", "name": "Bathroom boiler"}]
    }

    result = await flow.async_step_devices(
        user_input={
            "discovered_devices": ["sensor.not_discovered"],
            "add_custom_device": False,
        }
    )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_discovered_devices"}
    assert flow._devices == []
