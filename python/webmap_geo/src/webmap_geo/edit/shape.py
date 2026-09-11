"""Smooth, simplify and reshape. `09-editing.md` §11.3, §11.5.

**Smoothing reuses `contour.smooth.chaikin`** rather than growing a second
implementation. §11.5 says so and gives the reason: a shared boundary smoothed
by two different rules is the same class of bug as a fill that disagrees with
the line drawn over it. The cap that module applies — smoothing that would drift
a contour off the value it claims — applies here for the same reason, since a
lease boundary smoothed until it no longer follows the survey is a boundary
somebody will measure.

**Both operations break topological coincidence**, and §11.5 is explicit that
silently desyncing a shared boundary is the failure §7 exists to prevent. The
detection lives here — `shared_vertices` says which vertices a neighbour also
holds — and the decision of what to do about it belongs to the caller, because
"propagate" and "warn and confirm" are both correct and only the user knows
which they meant.

**Reshape replaces the boundary between exactly two crossings.** More or fewer
and it is refused with a sentence, because a reshape line that crosses three
times has two candidate answers and picking one would be a coin toss the user
does not know was flipped.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import shapely
from numpy.typing import NDArray
from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import split as shapely_split

from webmap_geo.contour.smooth import MAX_SMOOTHING, chaikin
from webmap_geo.exceptions import DegenerateInput

#: Coordinate agreement for calling two vertices the same, in frame units.
#: An order of magnitude tighter than any snap tolerance, as `09` §7.2 requires:
#: this answers "were these deliberately made coincident", not "are these close".
COINCIDENCE_TOLERANCE = 0.01


def smooth(geometry: BaseGeometry, smoothing: float) -> BaseGeometry:
    """Round the corners off a line or a polygon's rings.

    `smoothing` runs 0 to `MAX_SMOOTHING`, the same scale contours use, so a
    number that means one thing on a contour means the same thing on a lease.
    """
    if not 0.0 <= smoothing <= MAX_SMOOTHING:
        raise DegenerateInput(
            f"Smoothing is {smoothing}; it runs from 0 to {MAX_SMOOTHING}. Above "
            f"that a smoothed line drifts measurably off the geometry it claims "
            f"to be (`05` §7)."
        )
    if smoothing == 0.0:
        return geometry
    return _map_rings(
        geometry, lambda coords, closed: chaikin(coords, smoothing, closed=closed)
    )


def simplify(geometry: BaseGeometry, tolerance: float) -> BaseGeometry:
    """Douglas-Peucker, in the frame's own units.

    `preserve_topology` is on and is not a preference: without it Shapely will
    happily produce a self-intersecting polygon from a convoluted one, and a
    lease that crosses itself fails validation on the next save with an error
    pointing at the user's geometry rather than at the simplification that did
    it.
    """
    if tolerance <= 0:
        raise DegenerateInput(
            f"Simplify tolerance is {tolerance}; it must be positive and is in "
            f"the layer's own units — feet on a State Plane layer. It is the "
            f"furthest a vertex may move."
        )
    # `preserve_topology=True` never returns empty: measured, at a tolerance a
    # thousand times the feature's size, a polygon comes back as its minimal
    # valid form and a line as its two endpoints. So there is no empty case to
    # handle here — an earlier revision guarded one, and the guard was
    # unreachable code carrying a message nobody could ever have seen.
    return shapely.simplify(geometry, tolerance, preserve_topology=True)


def simplification_loss(original: BaseGeometry, simplified: BaseGeometry) -> float:
    """How much of the feature the tolerance took, as a fraction.

    Area for a polygon, length for a line. §11.5 asks for a live preview as the
    parameters change, and this is the number worth showing beside it: a lease
    that lost 30% of its area is not a simplified lease, and the vertex count
    alone does not say so.
    """
    if original.is_empty:
        return 0.0
    if original.geom_type in {"Polygon", "MultiPolygon"}:
        before, after = original.area, simplified.area
    else:
        before, after = original.length, simplified.length
    if before == 0.0:
        return 0.0
    return abs(before - after) / before


def reshape(polygon: BaseGeometry, line: LineString) -> BaseGeometry:
    """Replace the stretch of boundary a drawn line cuts off.

    §11.3: the line must cross the boundary at exactly two points. Three
    crossings leave two candidate answers, and choosing one silently is a coin
    toss the user does not know was flipped.
    """
    if not isinstance(polygon, Polygon):
        raise DegenerateInput(
            f"Reshape works on a single polygon; this is a "
            f"{polygon.geom_type}. Explode it first, then reshape the part you "
            f"mean."
        )

    boundary = polygon.exterior
    crossings = _crossing_points(boundary, line)
    if len(crossings) != 2:
        raise DegenerateInput(
            f"The reshape line crosses the boundary {len(crossings)} "
            f"time{'s' if len(crossings) != 1 else ''}; it has to cross exactly "
            f"twice, so that there is one stretch of boundary to replace. Draw "
            f"it from outside the feature, across the part you want to move, "
            f"and back out."
        )

    # Both halves are valid polygons; the one to keep is the one whose area is
    # closest to the original — the user is reshaping an edge, not turning the
    # feature inside out.
    pieces = shapely_split(polygon, line)
    candidates = [piece for piece in pieces.geoms if piece.geom_type == "Polygon"]
    if len(candidates) < 2:
        raise DegenerateInput(
            "The reshape line crossed the boundary twice but did not divide the "
            "feature, which usually means it doubles back along the edge. Draw a "
            "simpler line."
        )
    return max(candidates, key=lambda piece: piece.area)


def _crossing_points(boundary: BaseGeometry, line: LineString) -> list[Point]:
    intersection = boundary.intersection(line)
    if intersection.is_empty:
        return []
    if isinstance(intersection, Point):
        return [intersection]
    if isinstance(intersection, shapely.MultiPoint):
        return [point for point in intersection.geoms if isinstance(point, Point)]
    # A line that runs *along* the boundary rather than across it intersects in
    # a LineString. Its endpoints are the crossings, and there are two of them,
    # but the stretch between is ambiguous — treated as more than two so the
    # message above sends the user to draw a simpler line.
    return [Point(coord) for coord in shapely.get_coordinates(intersection)]


def shared_vertices(
    geometry: BaseGeometry, neighbours: list[BaseGeometry]
) -> list[tuple[float, float]]:
    """Vertices this feature holds in common with a neighbour.

    What §11.5 needs before smoothing or simplifying: these are the coordinates
    that stop matching if this feature moves and the neighbour does not, and a
    boundary that silently stops being shared is the sliver §7 exists to
    prevent.
    """
    mine = {_key(point) for point in shapely.get_coordinates(geometry)}
    shared = []
    for neighbour in neighbours:
        for point in shapely.get_coordinates(neighbour):
            if _key(point) in mine:
                shared.append((float(point[0]), float(point[1])))
    # Deduplicated, order preserved: a vertex shared with two neighbours is one
    # vertex, and the caller is counting how many will break.
    seen = set()
    unique = []
    for point in shared:
        if _key(np.array(point)) not in seen:
            seen.add(_key(np.array(point)))
            unique.append(point)
    return unique


def _key(point: NDArray[np.float64]) -> tuple[float, float]:
    quantum = COINCIDENCE_TOLERANCE
    return (
        round(float(point[0]) / quantum) * quantum,
        round(float(point[1]) / quantum) * quantum,
    )


@dataclass(frozen=True)
class TopologyWarning:
    """What a shape change will do to its neighbours."""

    shared_count: int
    neighbour_count: int

    @property
    def message(self) -> str:
        return (
            f"This changes {self.shared_count} vertex"
            f"{'es' if self.shared_count != 1 else ''} shared with "
            f"{self.neighbour_count} neighbouring feature"
            f"{'s' if self.neighbour_count != 1 else ''}. With topological "
            f"editing off, the shared boundary stops being shared and a sliver "
            f"opens along it."
        )


def warn_about(
    geometry: BaseGeometry, neighbours: list[BaseGeometry]
) -> TopologyWarning | None:
    """The warning to show before smoothing or simplifying, or nothing."""
    shared = shared_vertices(geometry, neighbours)
    if not shared:
        return None
    touching = sum(1 for neighbour in neighbours if shared_vertices(geometry, [neighbour]))
    return TopologyWarning(shared_count=len(shared), neighbour_count=touching)


#: A coordinate transform over one ring: its points and whether it closes.
RingTransform = Callable[[NDArray[np.float64], bool], NDArray[np.float64]]


def _map_rings(geometry: BaseGeometry, transform: RingTransform) -> BaseGeometry:
    """Apply a coordinate transform to every ring, rebuilding the same type."""
    kind = geometry.geom_type

    if isinstance(geometry, LineString):
        return LineString(transform(np.asarray(geometry.coords, dtype=np.float64), False))
    if isinstance(geometry, shapely.MultiLineString):
        return shapely.MultiLineString(
            [
                LineString(transform(np.asarray(part.coords, dtype=np.float64), False))
                for part in geometry.geoms
            ]
        )
    if isinstance(geometry, Polygon):
        return _ring_polygon(geometry, transform)
    if isinstance(geometry, shapely.MultiPolygon):
        return shapely.MultiPolygon([_ring_polygon(part, transform) for part in geometry.geoms])
    raise DegenerateInput(
        f"A {kind} has no rings to reshape. Smooth and Simplify apply to lines and polygons."
    )


def _ring_polygon(polygon: Polygon, transform: RingTransform) -> Polygon:
    shell = transform(np.asarray(polygon.exterior.coords, dtype=np.float64), True)
    holes = [
        transform(np.asarray(ring.coords, dtype=np.float64), True) for ring in polygon.interiors
    ]
    return Polygon(shell, holes)


__all__ = [
    "COINCIDENCE_TOLERANCE",
    "TopologyWarning",
    "reshape",
    "shared_vertices",
    "simplification_loss",
    "simplify",
    "smooth",
    "warn_about",
]
