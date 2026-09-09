"""Contouring. `05-geoprocessing.md` §7.

A contour map is how a geologist reads a surface, so its properties are
interpretive rather than cosmetic: round intervals, index contours every fifth,
and a smoothing cap beyond which a contour drifts off the value it claims.

Lines and filled bands come from the same level list and are smoothed by the
same rule, so a band's edge sits exactly under the contour drawn over it.
"""

from webmap_geo.contour.bands import ContourBand, contour_bands
from webmap_geo.contour.lines import (
    INDEX_EVERY,
    ContourLine,
    auto_levels,
    contour_grid,
    index_levels,
)
from webmap_geo.contour.smooth import MAX_SMOOTHING

__all__ = [
    "INDEX_EVERY",
    "MAX_SMOOTHING",
    "ContourBand",
    "ContourLine",
    "auto_levels",
    "contour_bands",
    "contour_grid",
    "index_levels",
]
