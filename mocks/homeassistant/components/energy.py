"""Mock energy."""
from unittest.mock import AsyncMock

async def async_get_manager(hass):
    mgr = AsyncMock()
    mgr.data = None
    return mgr
