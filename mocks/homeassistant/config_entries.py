"""Mock config_entries for testing."""

class ConfigEntry:
    entry_id = ""
    data = {}
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

    @staticmethod
    def async_get_options_flow(config_entry):
        return None
