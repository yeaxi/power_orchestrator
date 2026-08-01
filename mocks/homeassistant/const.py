"""Mock const."""

class Platform:
    SENSOR = "sensor"
    BINARY_SENSOR = "binary_sensor"
    SELECT = "select"

SERVICE_RELOAD = "reload"
STATE_ON = "on"
STATE_OFF = "off"
STATE_UNKNOWN = "unknown"
STATE_UNAVAILABLE = "unavailable"

class EntityCategory:
    """Subset of Home Assistant entity categories used by the integration."""

    CONFIG = "config"
    DIAGNOSTIC = "diagnostic"


class UnitOfPower:
    WATT = "W"
