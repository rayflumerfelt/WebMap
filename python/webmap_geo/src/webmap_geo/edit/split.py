"""Splitting features with a cut line. `09-editing.md` §11.1.

Geometry in, geometry out, in the analysis frame — `adr/0004` puts this here
rather than in the editing UI, and `adr/0003` means nothing in this module
transforms anything.

**A partial cut is rejected, not silently ignored.** §11.1 is explicit, and the
reason is what a partial cut looks like: the user draws a line most of the way
across a lease, presses Apply, and the map does not change. Nothing is wrong
with the data and nothing tells them why — so they draw it again, slightly
differently, and get the same silence. Refusing with a sentence is the whole
feature.

**One cut may produce more than two parts**, which is not an edge case but the
ordinary result of cutting a crescent or a lease with a hole in it. Anything
here that assumed two would be wrong on the first real fault trace.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import shapely
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import split as shapely_split

from webmap_geo.exceptions import DegenerateInput

#: Distance, in frame units, within which a cut endpoint counts as outside the
#: geometry it crosses. Not zero: a user tracing a lease boundary snaps to it,
#: and a cut that ends exactly on the boundary is a cut that stops there — which
#: is the partial cut this module refuses, arrived at by being *too* accurate.
BOUNDARY_TOLERANCE = 1e-6


@dataclass(frozen=True)
class SplitResult:
    """What one feature became."""

    parts: list[BaseGeometry]

    @property
    def was_split(self) -> bool:
        return len(self.parts) > 1


def split_geometry(geometry: BaseGeometry, cut: LineString) -> SplitResult:
    """Split one geometry along a cut line.

    Polygons must be crossed completely; lines split at every intersection,
    which for a fault trace crossing a horizon three times is three cuts and
    four pieces.
    """
    if cut.is_empty or len(cut.coords) < 2:
        raise DegenerateInput(
            "The cut line has fewer than two points, so there is nothing to cut "
            "along. Click at least twice to draw it."
        )
    if not geometry.is_valid:
        raise DegenerateInput(
            "This feature's geometry is invalid — self-intersecting, or with a "
            "ring that does not close — so a split would produce parts that are "
            "invalid too. Run Validate on the layer first."
        )

    if geometry.geom_type in {"Polygon", "MultiPolygon"}:
        _require_complete_crossing(geometry, cut)

    parts = [part for part in shapely_split(geometry, cut).geoms if not part.is_empty]
    if not parts:
        raise DegenerateInput(
            "The split produced no geometry at all, which means the cut and the "
            "feature do not actually meet. This is a bug if the preview showed "
            "them crossing."
        )
    return SplitResult(parts=list(parts))


def _require_complete_crossing(geometry: BaseGeometry, cut: LineString) -> None:
    """Refuse a cut that does not go all the way across.

    The test is on the *interior*: a cut whose ends are inside the polygon
    leaves a dangling edge that Shapely simply ignores, and the user sees
    nothing happen.
    """
    for polygon in _polygons(geometry):
        crossing = cut.intersection(polygon)
        if crossing.is_empty:
            continue

        start, end = shapely.Point(cut.coords[0]), shapely.Point(cut.coords[-1])
        inside = [
            point
            for point in (start, end)
            if polygon.contains(point) and point.distance(polygon.exterior) > BOUNDARY_TOLERANCE
        ]
        if inside:
            raise DegenerateInput(
                f"The cut line ends inside the feature, so it would not divide "
                f"it — {'both ends are' if len(inside) == 2 else 'one end is'} "
                f"within the boundary. Extend the line past the edge on both "
                f"sides and apply again."
            )


def _polygons(geometry: BaseGeometry) -> list[Polygon]:
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return [part for part in geometry.geoms if isinstance(part, Polygon)]
    return []


def cut_from_points(points: list[tuple[float, float]]) -> LineString:
    """The cutting polyline the user clicked out.

    Consecutive duplicates are dropped — a double-click while drawing adds the
    same vertex twice, and a zero-length segment makes the whole line invalid
    for `ops.split` with an error that says nothing about double-clicking.
    """
    cleaned: list[tuple[float, float]] = []
    for point in points:
        if not cleaned or point != cleaned[-1]:
            cleaned.append(point)
    if len(cleaned) < 2:
        raise DegenerateInput(
            "A cut needs at least two distinct points. Click along the line you "
            "want to cut with, then apply."
        )
    return LineString(cleaned)


def split_features(
    features: list[tuple[str, BaseGeometry, dict[str, Any]]],
    cut: LineString,
) -> list[tuple[str, list[BaseGeometry], dict[str, Any]]]:
    """Split every selected feature, keeping the ones the cut misses.

    A feature the cut does not touch comes back as a single part, so the caller
    can write one command covering everything the user selected without deciding
    per feature whether anything happened. §11.1: one undo entry containing the
    delete and every creation.
    """
    results = []
    for feature_id, geometry, props in features:
        if not geometry.intersects(cut):
            results.append((feature_id, [geometry], props))
            continue
        results.append((feature_id, split_geometry(geometry, cut).parts, props))
    return results


def provenance(props: dict[str, Any], source_id: str) -> dict[str, Any]:
    """Attributes for a part, carrying where it came from.

    §11.1 asks for `split_from_id`, and it earns its place the first time
    somebody asks why one lease is now three: the answer is in the data rather
    than in whoever remembers doing it.
    """
    return {**props, "split_from_id": source_id}


__all__ = [
    "BOUNDARY_TOLERANCE",
    "SplitResult",
    "cut_from_points",
    "provenance",
    "split_features",
    "split_geometry",
]
