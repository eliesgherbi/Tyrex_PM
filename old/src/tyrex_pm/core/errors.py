from __future__ import annotations


class TyrexError(Exception):
    """Base Tyrex error."""


class ConfigError(TyrexError):
    """Invalid configuration."""


class VenueError(TyrexError):
    """Venue adapter error."""
