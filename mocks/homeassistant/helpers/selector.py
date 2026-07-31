"""Mock selector."""


class _Selector:
    """Minimal Voluptuous-compatible selector used by local tests."""

    def __voluptuous_compile__(self, schema):
        return lambda _path, value: value


class EntitySelector(_Selector):
    def __init__(self, config=None):
        self.config = config or {}


class EntitySelectorConfig:
    def __init__(self, domain=None, include_entities=None, multiple=False):
        self.domain = domain
        self.include_entities = include_entities
        self.multiple = multiple


class NumberSelector(_Selector):
    def __init__(self, config=None):
        self.config = config or {}


class NumberSelectorConfig:
    def __init__(self, min=0, max=100, mode="box", unit_of_measurement=""):
        self.min = min
        self.max = max
        self.mode = mode
        self.unit_of_measurement = unit_of_measurement


class TextSelector(_Selector):
    def __init__(self, config=None):
        self.config = config or {}


class TextSelectorConfig:
    def __init__(self, multiline=False):
        self.multiline = multiline


class SelectSelector(_Selector):
    def __init__(self, config=None):
        self.config = config or {}


class SelectSelectorConfig:
    def __init__(self, options=None, translation_key=None, multiple=False):
        self.options = options or []
        self.translation_key = translation_key
        self.multiple = multiple


class SelectOptionDict:
    def __init__(self, value="", label=""):
        self.value = value
        self.label = label


class ObjectSelector(_Selector):
    """Selector for structured JSON-like config-flow values."""

    def __init__(self, config=None):
        self.config = config or {}


class ConfigEntrySelector(_Selector):
    def __init__(self, config=None):
        self.config = config or {}


class ConfigEntrySelectorConfig:
    def __init__(self, integration=None):
        self.integration = integration
