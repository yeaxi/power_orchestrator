"""Mock storage."""
from unittest.mock import AsyncMock

class Store:
    def __init__(self, hass, version, key):
        self._data = {}
        self.async_load = AsyncMock(return_value=None)
        self.async_save = AsyncMock()
