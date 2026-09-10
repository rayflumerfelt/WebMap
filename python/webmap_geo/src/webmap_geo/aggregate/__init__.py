"""Spatial aggregation. `05-geoprocessing.md` §8.

Runs **in-process** over Shapely and DuckDB. Not the hard part of this package,
but breadth is what makes it usable for anything other than gridding.

These were once "thin wrappers over PostGIS" — SQL issued at a database. That
put geometry operations outside the geoprocessing module, so "where does
geometry get transformed?" had a different answer depending which operation you
asked about, and §8's analysis-CRS rule was enforced by nothing
(`adr/0004-geoprocessing-owns-geometry.md`).

**Every operation runs in the analysis frame.** Buffering in EPSG:4326 produces
distances in degrees, which vary with latitude and are never what anyone wanted.
`FeatureSet` carries the frame so a mismatched overlay raises rather than
quietly returning nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from webmap_geo.aggregate.features import FeatureSet, empty_like, read_feature_set
from webmap_geo.aggregate.ops import (
    STAT_OPS,
    Stat,
    aggregate_points,
    buffer,
    centroid,
    clip,
    concave_hull,
    convex_hull,
    difference,
    dissolve,
    erase,
    hexbin,
    intersect,
    props_to_json,
    spatial_join,
    summarize_within,
    union_layers,
    voronoi,
)
from webmap_geo.exceptions import DegenerateInput

#: How many operands each operation takes. Checked before dispatch so a caller
#: that supplied one layer to an overlay gets told which layer is missing,
#: rather than an IndexError from somewhere inside the implementation.
ARITY: dict[str, int] = {
    "buffer": 1,
    "centroid": 1,
    "convex_hull": 1,
    "concave_hull": 1,
    "dissolve": 1,
    "aggregate_points": 1,
    "hexbin": 1,
    "voronoi": 1,
    "clip": 2,
    "erase": 2,
    "difference": 2,
    "intersect": 2,
    "union": 2,
    "spatial_join": 2,
    "summarize_within": 2,
}

_SINGLE: dict[str, Callable[..., FeatureSet]] = {
    "buffer": buffer,
    "centroid": centroid,
    "convex_hull": convex_hull,
    "concave_hull": concave_hull,
    "dissolve": dissolve,
    "aggregate_points": aggregate_points,
    "hexbin": hexbin,
}

_PAIR: dict[str, Callable[..., FeatureSet]] = {
    "clip": clip,
    "erase": erase,
    "difference": difference,
    "intersect": intersect,
    "union": union_layers,
    "spatial_join": spatial_join,
    "summarize_within": summarize_within,
}


def operations() -> list[str]:
    """Every operation this catalog dispatches, for a tool description."""
    return sorted(ARITY)


def aggregate(op: str, inputs: list[FeatureSet], **params: Any) -> FeatureSet:
    """Dispatch a spatial aggregation.

    Arrays arrive already in their analysis frame — this package never
    reprojects (`adr/0003`). The frame travels on the `FeatureSet`, which is
    what makes §8's analysis-CRS rule checkable rather than aspirational: a
    `distance` is in `frame.units`, and the frame is echoed into the lineage
    record, so a buffer that ran in degrees is visible afterwards instead of
    merely wrong.
    """
    if op not in ARITY:
        raise DegenerateInput(
            f"'{op}' is not a spatial aggregation. Available: {', '.join(operations())}."
        )
    if op == "voronoi":
        # One layer, or two when the second is the clip boundary. The only
        # operation whose second operand is optional, so it is the only one
        # exempt from the arity check.
        if not 1 <= len(inputs) <= 2:
            raise DegenerateInput(
                f"'voronoi' takes the point layer, and optionally a boundary to "
                f"clip the cells to; got {len(inputs)} layers."
            )
        clip_to = inputs[1] if len(inputs) == 2 else params.pop("clip_to", None)
        return voronoi(inputs[0], clip_to=clip_to, **params)

    expected = ARITY[op]
    if len(inputs) != expected:
        noun = "layer" if expected == 1 else "layers"
        raise DegenerateInput(
            f"'{op}' takes {expected} input {noun}; got {len(inputs)}. "
            + (
                "Overlay operations need a second layer to overlay against."
                if expected == 2
                else "This operation transforms one layer in place."
            )
        )

    if expected == 1:
        return _SINGLE[op](inputs[0], **params)
    return _PAIR[op](inputs[0], inputs[1], **params)


__all__ = [
    "ARITY",
    "STAT_OPS",
    "FeatureSet",
    "Stat",
    "aggregate",
    "aggregate_points",
    "buffer",
    "centroid",
    "clip",
    "concave_hull",
    "convex_hull",
    "difference",
    "dissolve",
    "empty_like",
    "erase",
    "hexbin",
    "intersect",
    "operations",
    "props_to_json",
    "read_feature_set",
    "spatial_join",
    "summarize_within",
    "union_layers",
    "voronoi",
]
