"""Preprocessing for geostatistics. `13-kriging.md` §5.

Order matters and each step assumes the previous has run: validate, then
decluster, then transform, then screen. A transform fitted before declustering
is a transform fitted to the drilling pattern; screening before validation
reports on rows that are about to be dropped.
"""

from webmap_geo.prep.decluster import DeclusterResult, decluster, weighted_quantile
from webmap_geo.prep.screen import ScreenResult, screen
from webmap_geo.prep.transform import Transform, build_transform
from webmap_geo.prep.validate import Samples, samples_from, validate

__all__ = [
    "DeclusterResult",
    "Samples",
    "ScreenResult",
    "Transform",
    "build_transform",
    "decluster",
    "samples_from",
    "screen",
    "validate",
    "weighted_quantile",
]
