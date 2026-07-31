"""Tests for storage.py — no HA dependencies."""
import sys, os, json, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components"))

import pytest
from power_orchestrator.storage import RuntimeStore
from power_orchestrator.power_model import ManagedDevice, PowerModel


class FakeStore:
    """Mock HA Store."""
    def __init__(self):
        self._data = None

    async def async_load(self):
        return self._data

    async def async_save(self, data):
        self._data = data


def make_model():
    m = PowerModel()
    m.add_device(ManagedDevice(device_id="d1", name="D1", entity_id="switch.d1", expected_power=1000, priority=1))
    m.add_device(ManagedDevice(device_id="d2", name="D2", entity_id="switch.d2", expected_power=2000, priority=2))
    return m


@pytest.mark.asyncio
async def test_store_empty_start():
    store = RuntimeStore(FakeStore())
    await store.async_load()
    assert store._data == {}


@pytest.mark.asyncio
async def test_store_restore_pause_timestamps():
    import time
    fake = FakeStore()
    fake._data = {
        "pause_timestamps": {
            "d1": time.time() + 12345,
            "d2": time.time() + 67890,
        }
    }
    store = RuntimeStore(fake)
    await store.async_load()
    model = make_model()
    store.restore_pause_timestamps(model)
    assert model.get_device("d1").pause_until > time.time()
    assert model.get_device("d2").pause_until > time.time()


@pytest.mark.asyncio
async def test_store_set_pause():
    import time
    fake = FakeStore()
    fake._data = {}  # Initialize to empty dict
    store = RuntimeStore(fake)
    await store.async_load()
    store.set_pause("d1", 60)
    saved = fake._data
    assert saved is not None
    assert "pause_timestamps" in saved
    assert "d1" in saved["pause_timestamps"]
    assert saved["pause_timestamps"]["d1"] > time.time()


@pytest.mark.asyncio
async def test_store_clear_pause():
    fake = FakeStore()
    fake._data = {"pause_timestamps": {"d1": 12345.0}}
    store = RuntimeStore(fake)
    await store.async_load()
    store.clear_pause("d1")
    assert "d1" not in fake._data["pause_timestamps"]


@pytest.mark.asyncio
async def test_store_persist_and_restore():
    import time
    fake = FakeStore()
    store = RuntimeStore(fake)
    await store.async_load()

    store.set_pause("d1", 120)
    await store.async_save()

    # Simulate restart
    store2 = RuntimeStore(fake)
    await store2.async_load()
    model = make_model()
    store2.restore_pause_timestamps(model)
    assert model.get_device("d1").pause_until is not None
    assert model.get_device("d1").pause_until > time.time()