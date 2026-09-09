"""Interpolation. `05-geoprocessing.md` §6.

One entry point per method, all writing into a `GridDefinition` in the
analysis frame. Nothing here reprojects: arrays arrive in the frame they claim
(`adr/0003-geoprocessing-owns-crs.md`), and a transformer call inside a solver
means coordinates were not in the frame they said they were.
"""

# Re-exported: the grid lives at the package root (it is shared with
# faults and contour), and callers reasonably reach for it here.
from webmap_geo.grid import SOFT_CELL_LIMIT, GridDefinition
from webmap_geo.interpolate.dispatch import (
    EXTRAPOLATION_WARNING,
    OVERSHOOT_WARNING,
    InterpolationResult,
    Method,
    interpolate,
)
from webmap_geo.interpolate.kriging import (
    DEFAULT_NEIGHBORS,
    KrigingResult,
    cross_validate,
    ordinary_kriging,
)
from webmap_geo.interpolate.minimum_curvature import (
    MinimumCurvatureResult,
    minimum_curvature,
)

__all__ = [
    "DEFAULT_NEIGHBORS",
    "EXTRAPOLATION_WARNING",
    "OVERSHOOT_WARNING",
    "SOFT_CELL_LIMIT",
    "GridDefinition",
    "InterpolationResult",
    "KrigingResult",
    "Method",
    "MinimumCurvatureResult",
    "cross_validate",
    "interpolate",
    "minimum_curvature",
    "ordinary_kriging",
]
