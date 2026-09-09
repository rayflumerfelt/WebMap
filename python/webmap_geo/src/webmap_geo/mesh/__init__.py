"""Constrained triangulation and path distance. `05-geoprocessing.md` §5.

The mesh is what makes "how far apart are these two wells, given you cannot
walk through the fault" answerable. `faults.raster` answers a different and
narrower question — which cell-centre links a fault blocks — which is right
for a finite-difference stencil and useless for a neighbourhood search.
"""

from webmap_geo.mesh.constrained import (
    BREAKLINE_SEGMENT,
    FAULT_SEGMENT,
    ConstrainedMesh,
    build_mesh,
)
from webmap_geo.mesh.distance import (
    Neighbourhood,
    barrier_aware_neighbours,
    compartment_of,
    nearest_vertex,
    path_distances,
)

__all__ = [
    "BREAKLINE_SEGMENT",
    "FAULT_SEGMENT",
    "ConstrainedMesh",
    "Neighbourhood",
    "barrier_aware_neighbours",
    "build_mesh",
    "compartment_of",
    "nearest_vertex",
    "path_distances",
]
