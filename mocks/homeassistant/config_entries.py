"""Mock config_entries for testing."""

from enum import Enum


class ConfigEntryState(str, Enum):
    """Subset of Home Assistant config-entry lifecycle states used by smoke tests."""

    LOADED = "loaded"
    NOT_LOADED = "not_loaded"

class ConfigEntry:
    entry_id = ""
    data = {}
    options = {}
    title = ""


class OptionsFlow:
    def __init__(self, config_entry=None):
        self._config_entry = config_entry

    def async_show_form(self, *, step_id, data_schema=None, errors=None, description_placeholders=None, last_step=None):
        return {"type": "form", "step_id": step_id, "data_schema": data_schema, "errors": errors, "description_placeholders": description_placeholders, "last_step": last_step}

    def async_create_entry(self, title, data):
        return {"type": "create_entry", "title": title, "data": data}


class ConfigFlow:
    domain = ""

    def __init_subclass__(cls, domain=None, **kwargs):
        if domain:
            cls.domain = domain

    def __init__(self):
        self.hass = None

    def async_show_form(self, *, step_id, data_schema=None, errors=None, description_placeholders=None, last_step=None):
        return {"type": "form", "step_id": step_id, "data_schema": data_schema, "errors": errors, "description_placeholders": description_placeholders, "last_step": last_step}

    def async_create_entry(self, title, data):
        return {"type": "create_entry", "title": title, "data": data}

    def async_abort(self, reason):
        return {"type": "abort", "reason": reason}

    def _get_reconfigure_entry(self):
        """Return the entry referenced by a reconfigure context."""
        entry_id = getattr(self, "_reconfigure_entry_id", None)
        if entry_id is None:
            context = getattr(self, "context", {}) or {}
            entry_id = context.get("entry_id") if isinstance(context, dict) else None
        entries = getattr(self.hass, "config_entries", None)
        getter = getattr(entries, "async_get_entry", None)
        return getter(entry_id) if callable(getter) and entry_id else None

    def async_update_and_abort(
        self,
        entry,
        *,
        data=None,
        data_updates=None,
        options=None,
        reason="reconfigure_successful",
        **kwargs,
    ):
        """Mock the synchronous HA config-flow update callback."""
        return {
            "type": "abort",
            "reason": reason,
            "entry": entry,
            "data": data,
            "data_updates": data_updates,
            "options": options,
            **kwargs,
        }

    async def async_update_reload_and_abort(
        self,
        entry,
        *,
        data_updates=None,
        options=None,
        reason="reconfigure_successful",
        **kwargs,
    ):
        return {
            "type": "abort",
            "reason": reason,
            "entry": entry,
            "data_updates": data_updates,
            "options": options,
            **kwargs,
        }

    @staticmethod
    def async_get_options_flow(config_entry):
        return None
