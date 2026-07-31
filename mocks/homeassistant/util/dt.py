"""Minimal datetime utility mock for local tests."""
from datetime import datetime


def now(time_zone=None):
    """Return current aware time in the requested/system timezone."""
    return datetime.now(time_zone).astimezone() if time_zone is None else datetime.now(time_zone)
