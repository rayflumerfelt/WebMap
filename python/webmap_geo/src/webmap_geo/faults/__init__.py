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
from webmap_geo.faults.raster import blocked_edges, compartments, control_per_compartment

__all__ = [
    "Constraint",
    "ConstraintKind",
    "ValidationReport",
    "blocked_edges",
    "clean_network",
    "compartments",
    "control_per_compartment",
    "validate_network",
]
