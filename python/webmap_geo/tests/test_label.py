"""Label anchors. `05-geoprocessing.md` §7.2.

The bar is that a label never lands outside the thing it names. That failure is
not subtle on screen — a lease name floating in open ground — but it is
invisible to any test that only checks an anchor was produced, which is why
every case here asserts containment rather than existence.
"""

from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiPolygon,
    Point,
    Polygon,
)

from webmap_geo.exceptions import DegenerateInput
from webmap_geo.frame import AnalysisFrame
from webmap_geo.label import CENTROID, POLE, LabelAnchor, label_anchors

TEXAS = AnalysisFrame(srid=2277, units="usft")


def square(size: float = 100.0) -> Polygon:
    return Polygon([(0, 0), (size, 0), (size, size), (0, size)])


def crescent() -> Polygon:
    """A C-shape. Its centroid falls in the bite, outside the polygon."""
    cut = square().difference(Polygon([(20, -10), (80, -10), (80, 80), (20, 80)]))
    assert isinstance(cut, Polygon), "the bite split the square in two"
    return cut


def anchors_of(*geometries: object) -> list[LabelAnchor | None]:
    return label_anchors(list(geometries), TEXAS)  # type: ignore[arg-type]


def only(anchors: list[LabelAnchor | None]) -> LabelAnchor:
    assert len(anchors) == 1
    anchor = anchors[0]
    assert anchor is not None, "no anchor was produced"
    return anchor


# --- the anchor is inside the polygon ------------------------------------------


def test_a_convex_polygon_is_labelled_at_its_centroid() -> None:
    """The conventional choice, and the visual centre for anything convex.
    Reaching for the pole of inaccessibility here would move the label off the
    place a cartographer would put it."""
    anchor = only(anchors_of(square()))

    assert anchor.method == CENTROID
    assert anchor.point.equals_exact(Point(50, 50), tolerance=1e-9)


def test_a_crescent_is_labelled_inside_itself_not_at_its_centroid() -> None:
    """The case the fallback exists for. A C-shaped lease, or a township with a
    bay cut out of it, has its centroid in open ground — and a label there
    names the wrong thing to anyone reading the map."""
    shape = crescent()
    assert not shape.contains(shape.centroid), "this fixture is not concave enough"

    anchor = only(anchors_of(shape))

    assert anchor.method == POLE
    assert shape.contains(anchor.point), "the label landed outside the polygon"


def test_a_polygon_with_a_hole_over_its_centroid_is_labelled_beside_it() -> None:
    """A township with a lake in the middle. The centroid is inside the outer
    ring and inside the lake, so a containment check against the exterior alone
    passes and the label sits on the water."""
    lake = Polygon([(35, 35), (65, 35), (65, 65), (35, 65)])
    township = Polygon(square().exterior.coords, [lake.exterior.coords])

    anchor = only(anchors_of(township))

    assert township.contains(anchor.point)
    assert not lake.contains(anchor.point), "the label sits in the lake"


@pytest.mark.parametrize("seed", range(12))
def test_a_random_polygon_is_never_labelled_outside_itself(seed: int) -> None:
    """The property that matters, over shapes nobody chose. Random star
    polygons with deep notches are the cheapest way to generate the concave
    cases a hand-written fixture list would miss."""
    rng = np.random.default_rng(seed)
    angles = np.sort(rng.uniform(0, 2 * np.pi, 14))
    radii = rng.uniform(10.0, 100.0, 14)
    shape = Polygon(np.column_stack([radii * np.cos(angles), radii * np.sin(angles)]))
    if not shape.is_valid or shape.area <= 0:
        pytest.skip("degenerate random polygon")

    anchor = only(anchors_of(shape))

    assert shape.contains(anchor.point)


# --- one feature, one label -----------------------------------------------------


def test_a_multipolygon_gets_one_anchor_on_its_largest_part() -> None:
    """One feature is one label. Labelling every part is what MapLibre already
    does, and it is what puts a lease name on each of its slivers."""
    big = square(100.0)
    sliver = Polygon([(500, 500), (505, 500), (505, 502), (500, 502)])

    anchor = only(anchors_of(MultiPolygon([sliver, big])))

    assert big.contains(anchor.point), "the label went to the sliver"


def test_a_collection_is_labelled_on_the_polygons_it_holds() -> None:
    """A GeometryCollection with a polygon in it is an ordinary result of a
    clip, and refusing it would make anchoring fail after an ordinary edit."""
    collection = GeometryCollection([LineString([(0, 0), (10, 10)]), square()])

    anchor = only(anchors_of(collection))

    assert square().contains(anchor.point)


# --- alignment with the input ---------------------------------------------------


def test_an_empty_geometry_holds_its_place_in_the_result() -> None:
    """**The alignment rule.** The caller zips these back onto features by
    position, so skipping one shifts every label after it onto the wrong
    feature — a map where the names are right and all in the wrong places,
    which reads as a data problem rather than as this."""
    anchors = anchors_of(square(), Polygon(), square(50.0))

    assert len(anchors) == 3
    assert anchors[1] is None
    assert anchors[0] is not None and anchors[2] is not None


def test_no_anchors_for_no_features() -> None:
    assert label_anchors([], TEXAS) == []


# --- clearance -------------------------------------------------------------------


def test_clearance_is_the_room_the_label_has() -> None:
    """Half the width of a 100 ft square, from its centre to any edge."""
    anchor = only(anchors_of(square(100.0)))

    assert anchor.clearance == pytest.approx(50.0)


def test_clearance_counts_a_hole_rather_than_only_the_outside() -> None:
    """`boundary`, not `exterior`.

    The hole sits just off centre, so the centroid stays on solid ground and
    this exercises the centroid branch — but it lands 5 ft from the water and
    50 ft from the outside. Measuring against the exterior alone reports 49.9
    and tells the caller there is room for a label that will sit in the lake.

    A concentric ring would not catch it: there the pole of inaccessibility is
    equidistant from both rings, and the wrong measurement gives the right
    answer.
    """
    lake = Polygon([(55, 45), (65, 45), (65, 55), (55, 55)])
    township = Polygon(square().exterior.coords, [lake.exterior.coords])

    anchor = only(anchors_of(township))

    assert anchor.method == CENTROID
    assert anchor.clearance == pytest.approx(5.1, abs=0.1), (
        f"clearance {anchor.clearance:g} ignores the hole it is standing beside"
    )


# --- refusals ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "geometry",
    [Point(1, 2), LineString([(0, 0), (1, 1)])],
    ids=["point", "line"],
)
def test_a_non_polygon_layer_says_what_to_do_instead(geometry: object) -> None:
    """`CLAUDE.md` §8. Anchoring a line layer is a caller mistake with a real
    answer — MapLibre places line labels itself — so the message names it."""
    with pytest.raises(DegenerateInput, match="line-center"):
        anchors_of(geometry)


def test_a_zero_tolerance_is_refused_rather_than_hanging() -> None:
    """`polylabel` iterates until its cell queue is finer than the tolerance,
    so zero does not terminate."""
    with pytest.raises(DegenerateInput, match="must be positive"):
        label_anchors([crescent()], TEXAS, tolerance_ratio=0.0)
