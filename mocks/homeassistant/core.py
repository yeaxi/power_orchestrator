"""Mock core."""
from unittest.mock import MagicMock

HomeAssistant = MagicMock

def callback(func=None):
    if func:
        return func
    return lambda f: f
