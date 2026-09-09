"""Exceptions raised by webmap_geo.

Every message answers what happened, why, and what now (CLAUDE.md §8). These
surface through MCP tool responses, where the message *is* the interface.
"""


class GeoError(Exception):
    """Base for every error this package raises. Never raise bare Exception."""


class UnknownCrs(GeoError):
    """An EPSG code PROJ does not recognise.

    Its own kind rather than a bare `GeoError` because SRIDs arrive from
    callers — a project's `analysis_srid`, a `--srid` on import — so an
    unknown one is a caller mistake with a fixable cause, not a server fault.
    Left unwrapped it surfaces as pyproj's `CRSError`, which nothing maps and
    which therefore becomes a 500.
    """


class NotProjected(GeoError):
    """A geographic CRS was supplied where a projected one is required."""


class DegenerateInput(GeoError):
    """Input arrays cannot support the requested operation."""


class FrameMismatch(GeoError):
    """Two inputs declared different analysis frames."""
