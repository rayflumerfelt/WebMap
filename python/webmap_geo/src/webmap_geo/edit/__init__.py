"""Geometry operations the editor runs. `09-editing.md` §11.

Here rather than in `apps/web` because every one of them reads and writes
geometry, which `adr/0004` puts in this package. The editing UI decides *when*
an operation runs and what the undo entry says; what the geometry becomes is
decided here, once, for the browser and the MCP tool and the worker alike.
"""

from webmap_geo.edit.combine import AttributePolicy, combine, dissolve, explode
from webmap_geo.edit.shape import reshape, shared_vertices, simplify, smooth
from webmap_geo.edit.split import split_features, split_geometry

__all__ = [
    "AttributePolicy",
    "combine",
    "dissolve",
    "explode",
    "reshape",
    "shared_vertices",
    "simplify",
    "smooth",
    "split_features",
    "split_geometry",
]
