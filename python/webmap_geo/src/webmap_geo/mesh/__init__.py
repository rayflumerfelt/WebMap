"""Constrained triangulation and fault compartments. `05-geoprocessing.md` §5.

The triangulation is what makes a fault a structural fact rather than a note
on a map: constrained edges are guaranteed present in the output, and vertices
on a hard fault are split per side, so the two sides of a sealing fault share
no vertex and nothing downstream has to remember to check.

`faults.raster` answers a narrower question — which cell-centre links a fault
blocks — which is what minimum curvature's finite-difference stencil needs and
is not a substitute for this.
"""

from webmap_geo.mesh.constrained import (
    BREAKLINE_SEGMENT,
    FAULT_SEGMENT,
    ConstrainedMesh,
    build_mesh,
)
from webmap_geo.mesh.distance import assign_compartments, compartment_of

__all__ = [
    "BREAKLINE_SEGMENT",
    "FAULT_SEGMENT",
    "ConstrainedMesh",
    "assign_compartments",
    "build_mesh",
    "compartment_of",
]
