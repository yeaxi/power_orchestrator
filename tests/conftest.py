"""Add mocks to path before any HA imports."""
import os
import sys
import pytest

# Add mocks directory to sys.path BEFORE any other imports
mocks_dir = os.path.join(os.path.dirname(__file__), "..", "mocks")
if mocks_dir not in sys.path:
    sys.path.insert(0, mocks_dir)

# Set asyncio mode
pytest_plugins = ("pytest_asyncio",)

# Now import homeassistant to verify it works
import homeassistant.config_entries
import homeassistant.core
import homeassistant.const
import homeassistant.helpers.storage
import homeassistant.helpers.update_coordinator
import homeassistant.helpers.entity_platform
import homeassistant.helpers.selector
import homeassistant.components.energy
import homeassistant.components.sensor
import homeassistant.components.binary_sensor
import homeassistant.components.select