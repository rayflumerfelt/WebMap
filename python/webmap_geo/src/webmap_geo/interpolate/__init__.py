"""Interpolation. `05-geoprocessing.md` §6.

One entry point per method, all writing into a `GridDefinition` in the
analysis frame. Nothing here reprojects: arrays arrive in the frame they claim
(`adr/0003-geoprocessing-owns-crs.md`), and a transformer call inside a solver
means coordinates were not in the frame they said they were.
"""

from webmap_geo.interpolate.grid import SOFT_CELL_LIMIT, GridDefinition
from webmap_geo.interpolate.kriging import (
    DEFAULT_NEIGHBORS,
    KrigingResult,
    cross_validate,
    ordinary_kriging,
)

__all__ = [
    "DEFAULT_NEIGHBORS",
    "SOFT_CELL_LIMIT",
    "GridDefinition",
    "KrigingResult",
    "cross_validate",
    "ordinary_kriging",
]
