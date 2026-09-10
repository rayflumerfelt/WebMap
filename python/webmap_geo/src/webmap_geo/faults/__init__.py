"""Fault networks: validation, cleaning, and rasterisation onto a grid.

`05-geoprocessing.md` §3 and §4. The design rule governing everything here:
**never silently repair a fault network.** The distinction between "this fault
tips out here" and "this fault trace is incomplete" is geological judgment, not
a preprocessing decision.
"""

from webmap_geo.faults.network import (
    Constraint,
    ConstraintKind,
    ValidationReport,
    clean_network,
    validate_network,
)
from webmap_geo.faults.raster import (
    blocked_edges,
    breakline_control,
    compartments,
    control_per_compartment,
    soft_edges,
)

__all__ = [
    "Constraint",
    "ConstraintKind",
    "ValidationReport",
    "blocked_edges",
    "breakline_control",
    "clean_network",
    "compartments",
    "control_per_compartment",
    "soft_edges",
    "validate_network",
]
