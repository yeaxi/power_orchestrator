"""Mock homeassistant package for local testing."""
import sys
import os

# Add mocks directory to path
_mocks_dir = os.path.join(os.path.dirname(__file__), "..", "mocks")
if _mocks_dir not in sys.path:
    sys.path.insert(0, _mocks_dir)