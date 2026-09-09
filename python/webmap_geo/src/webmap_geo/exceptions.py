"""Exceptions raised by webmap_geo.

Every message answers what happened, why, and what now (CLAUDE.md §8). These
surface through MCP tool responses, where the message *is* the interface.
"""


class GeoError(Exception):
    """Base for every error this package raises. Never raise bare Exception."""


class NotProjected(GeoError):
    """A geographic CRS was supplied where a projected one is required."""


class DegenerateInput(GeoError):
    """Input arrays cannot support the requested operation."""


class FrameMismatch(GeoError):
    """Two inputs declared different analysis frames."""
