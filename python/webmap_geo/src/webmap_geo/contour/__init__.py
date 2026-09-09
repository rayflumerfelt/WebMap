"""Contouring. `05-geoprocessing.md` §7.

A contour map is how a geologist reads a surface, so its properties are
interpretive rather than cosmetic: round intervals, index contours every fifth,
and a smoothing cap beyond which a contour drifts off the value it claims.
"""

from webmap_geo.contour.lines import (
    INDEX_EVERY,
    MAX_SMOOTHING,
    ContourLine,
    auto_levels,
    contour_grid,
    index_levels,
)

__all__ = [
    "INDEX_EVERY",
    "MAX_SMOOTHING",
    "ContourLine",
    "auto_levels",
    "contour_grid",
    "index_levels",
]
