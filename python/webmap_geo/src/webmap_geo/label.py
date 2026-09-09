"""Label anchors for polygons. `05-geoprocessing.md` §7.2.

**MapLibre can place a polygon label; the problem is how.** `symbol_layout.ts`
calls `findPoleOfInaccessibility` on the *tile-clipped* geometry, once per tile
and once per ring group, at a precision of two pixels. So:

- a polygon crossing a tile boundary is a different shape in each tile and gets
  a different anchor in each — the label moves, or appears twice, while panning;
- the anchor is recomputed at every zoom against a differently-clipped polygon,
  so it drifts while zooming;
- a multipolygon lease gets one label per part, slivers included.

All three read as a rendering fault rather than as a placement policy. An
anchor computed here is computed once, against the whole geometry, and is a
dataset in its own right — inspectable, correctable by hand, and exported with
the map.

Anchoring reads and writes geometry, which is what puts it in `webmap_geo`
rather than in `style-model` (`adr/0004`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from shapely.algorithms.polylabel import polylabel
from shapely.geometry import MultiPolygon, Point, Polygon
from shapely.geometry.base import BaseGeometry

from webmap_geo.exceptions import DegenerateInput
from webmap_geo.frame import AnalysisFrame

#: Polylabel's precision, as a fraction of the polygon's characteristic width
#: (`sqrt(area)`). Relative rather than absolute because a lease and a basin
#: differ by four orders of magnitude in size, and a tolerance that is
#: reasonable for one either costs seconds or returns a corner for the other.
#:
#: One percent of a width is well under a pixel at any zoom where the polygon
#: is big enough to hold a label at all.
TOLERANCE_RATIO = 0.01

#: How the anchor was placed. Carried out because "why is this label off
#: centre" is a question somebody asks, and because a UI offering "recompute"
#: needs to know which rule produced what it is replacing.
CENTROID = "centroid"
POLE = "pole"


@dataclass(frozen=True)
class LabelAnchor:
    """Where one feature's label goes."""

    point: Point
    method: str
    #: Distance from the anchor to the nearest edge, in the frame's units —
    #: how much room the label has. A label wider than twice this overflows its
    #: polygon, which is what lets a caller thin labels by feature size rather
    #: than by guessing a zoom.
    clearance: float


def label_anchors(
    geometries: Sequence[BaseGeometry],
    frame: AnalysisFrame,
    *,
    tolerance_ratio: float = TOLERANCE_RATIO,
) -> list[LabelAnchor | None]:
    """One anchor per input geometry, in input order.

    **The result is aligned with the input, including its gaps.** A geometry
    with no polygonal area yields `None` rather than being skipped, because the
    caller zips these back onto features by position and a shorter list
    silently shifts every label after the first empty one onto the wrong
    feature.

    A multipolygon gets **one** anchor, on its largest part. One feature is one
    label; labelling every part is what MapLibre already does, and it is what
    puts a lease name on each of its slivers.

    The centroid is used where it falls inside the polygon, and the pole of
    inaccessibility — the centre of the largest inscribed circle — where it
    does not. A crescent-shaped lease or a township with a lake in it has its
    centroid outside itself, and a label there sits on open ground.
    """
    if tolerance_ratio <= 0:
        raise DegenerateInput(
            f"tolerance_ratio must be positive; got {tolerance_ratio:g}. It is a "
            f"fraction of the polygon's width, so 0.01 means one percent."
        )

    anchors: list[LabelAnchor | None] = []
    for position, geometry in enumerate(geometries):
        anchors.append(_anchor(geometry, position, frame, tolerance_ratio))
    return anchors


def _anchor(
    geometry: BaseGeometry,
    position: int,
    frame: AnalysisFrame,
    tolerance_ratio: float,
) -> LabelAnchor | None:
    part = _largest_part(geometry, position, frame)
    if part is None:
        return None

    centroid = part.centroid
    if part.contains(centroid):
        return LabelAnchor(
            point=centroid,
            method=CENTROID,
            clearance=_clearance(part, centroid),
        )

    # `polylabel` needs a positive tolerance and iterates until its cell queue
    # is finer than it. Derived from the part actually being labelled rather
    # than from the layer, so a sliver beside a township does not get a
    # tolerance coarser than the sliver.
    tolerance = max((part.area**0.5) * tolerance_ratio, _SMALLEST_TOLERANCE)
    pole = polylabel(part, tolerance=tolerance)
    return LabelAnchor(point=pole, method=POLE, clearance=_clearance(part, pole))


def _clearance(part: Polygon, point: Point) -> float:
    """Distance to the nearest edge, holes included.

    `boundary`, not `exterior`: a township with a lake in the middle of it has
    its centroid a long way from the outside and a few feet from the water, and
    an anchor that reports the first has told the caller there is room for a
    label that will sit in the lake.
    """
    return float(part.boundary.distance(point))


#: A floor under the derived tolerance. `polylabel` does not terminate on a
#: tolerance of zero, and a degenerate polygon can have an area that
#: underflows to it.
_SMALLEST_TOLERANCE = 1e-9


def _largest_part(
    geometry: BaseGeometry, position: int, frame: AnalysisFrame
) -> Polygon | None:
    """The polygon a single label belongs on, or `None` if there is not one."""
    if geometry is None or geometry.is_empty:
        return None

    if isinstance(geometry, Polygon):
        candidates = [geometry]
    elif isinstance(geometry, MultiPolygon):
        candidates = list(geometry.geoms)
    elif geometry.geom_type in ("Point", "MultiPoint", "LineString", "MultiLineString"):
        raise DegenerateInput(
            f"Feature {position} is a {geometry.geom_type}, and label anchors are "
            f"for polygons. Points already have a position, and a line's label "
            f"belongs on the line — use MapLibre's `line-center` placement for "
            f"those rather than anchoring them here."
        )
    else:
        # A GeometryCollection, or a mixed layer. Take whatever polygons it
        # holds rather than refusing: a collection with one polygon in it is an
        # ordinary result of a clip.
        candidates = [
            part for part in getattr(geometry, "geoms", []) if isinstance(part, Polygon)
        ]

    usable = [part for part in candidates if not part.is_empty and part.area > 0]
    if not usable:
        return None
    if len(usable) == 1:
        return usable[0]

    largest = max(usable, key=lambda part: part.area)
    if largest.area <= 0:
        raise DegenerateInput(
            f"Feature {position} has no area to label in {frame.describe()}. Every "
            f"part collapsed to a line or a point."
        )
    return largest


__all__ = ["CENTROID", "POLE", "TOLERANCE_RATIO", "LabelAnchor", "label_anchors"]
