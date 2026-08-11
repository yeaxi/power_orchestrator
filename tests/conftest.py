"""Shared fixtures for the Power Orchestrator test suite.

The suite runs against a real Home Assistant runtime via
``pytest-homeassistant-custom-component``. There is no bespoke mock package;
unit tests that need a lightweight ``hass`` build their own ``MagicMock``,
while integration tests use the plugin-provided ``hass`` fixture together with
``enable_custom_integrations``.
"""

import pytest

pytest_plugins = ("pytest_homeassistant_custom_component",)


@pytest.fixture(autouse=True)
def _auto_enable_custom_integrations(enable_custom_integrations):
    """Permit the custom integration to load in every test that boots hass."""
    yield
