"""The spatial aggregation catalog. `05-geoprocessing.md` §8.

Areas and counts are checked against closed forms — a buffer of a point is
pi*r^2, a dissolve of two touching squares is their combined area with no
double count. Output that merely looks plausible is the failure mode these
operations have, because every one of them returns *something*.

The other half is the pairs people confuse: clip against intersect, dissolve
against combine, join against lookup. Each of those produces a different
feature count from the same inputs, and getting the wrong one is silent.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest
import shapely
from shapely.geometry import LineString, Point, Polygon

from webmap_geo.aggregate import FeatureSet, Stat, aggregate, operations, ops
from webmap_geo.exceptions import DegenerateInput, FrameMismatch
from webmap_geo.frame import AnalysisFrame

TEXAS = AnalysisFrame(srid=2277, units="usft")
OTHER = AnalysisFrame(srid=32613, units="m")


def fs(
    geoms: list[Any],
    props: list[dict[str, Any]] | None = None,
    frame: AnalysisFrame = TEXAS,
) -> FeatureSet:
    return FeatureSet(
        geometry=np.array(geoms, dtype=object),
        props=props if props is not None else [{} for _ in geoms],
        frame=frame,
    )


def square(x: float, y: float, size: float = 10.0) -> Polygon:
    return shapely.box(x, y, x + size, y + size)


# --- per-feature transforms ---------------------------------------------------


def test_buffering_a_point_gives_the_area_of_a_circle() -> None:
    """Tolerance is the polygon approximation, not slack. 8 quadrant segments
    is a 32-gon, whose area is `0.5 * n * sin(2*pi/n)` of the circle's —
    0.9936, so it under-measures by 0.64%. Anything looser would hide a wrong
    distance; anything tighter fails on arithmetic."""
    out = ops.buffer(fs([Point(0, 0)]), distance=100.0)

    assert out.geometry[0].area == pytest.approx(math.pi * 100.0**2, rel=0.007)
    assert out.geometry[0].area < math.pi * 100.0**2, "an inscribed polygon cannot exceed it"


def test_a_negative_buffer_that_erodes_a_feature_away_drops_it() -> None:
    """Rather than returning an empty geometry, which every writer downstream
    rejects and which reads as corruption rather than as a small polygon."""
    out = ops.buffer(fs([square(0, 0, 10), square(100, 100, 1000)]), distance=-5.0)

    assert len(out) == 1, "the 10 ft square should have eroded to nothing"
    assert out.geometry[0].area > 0


def test_a_zero_buffer_is_refused_rather_than_returning_the_input() -> None:
    with pytest.raises(DegenerateInput, match="returns the input unchanged"):
        ops.buffer(fs([Point(0, 0)]), distance=0)


def test_point_on_surface_lands_inside_a_crescent_where_the_centroid_does_not() -> None:
    """The distinction the option exists for. A centroid is right for a
    distribution and wrong for a label (`05` §7.2)."""
    crescent = square(0, 0, 100).difference(shapely.box(20, -10, 80, 80))
    assert not crescent.contains(crescent.centroid), "fixture is not concave enough"

    inside = ops.centroid(fs([crescent]), on_surface=True)
    outside = ops.centroid(fs([crescent]), on_surface=False)

    assert crescent.contains(inside.geometry[0])
    assert not crescent.contains(outside.geometry[0])


def test_a_concave_hull_is_tighter_than_the_convex_one() -> None:
    """The whole reason to ask for one. Shapely's, because ST_ConcaveHull is
    absent from duckdb 1.5.5 spatial despite `05` §8's table naming it."""
    # Two separated clusters: the convex hull bridges the gap between them and
    # a concave hull does not. Points arranged on a boundary — a circle, or a
    # U — cannot show the difference, because every one of them is already on
    # the convex hull; both fixtures came back with identical area.
    rng = np.random.default_rng(1)
    cloud = np.vstack(
        [rng.normal([0, 0], 8, size=(120, 2)), rng.normal([200, 0], 8, size=(120, 2))]
    )
    points = [Point(x, y) for x, y in cloud]

    convex = ops.convex_hull(fs(points))
    concave = ops.concave_hull(fs(points), ratio=0.05)

    assert concave.geometry[0].area < 0.7 * convex.geometry[0].area


def test_a_concave_hull_ratio_outside_zero_to_one_is_refused() -> None:
    with pytest.raises(DegenerateInput, match="between 0 and 1"):
        ops.concave_hull(fs([Point(0, 0), Point(1, 1), Point(0, 1)]), ratio=1.5)


# --- overlay ------------------------------------------------------------------


def test_clip_keeps_the_left_layers_attributes_and_count() -> None:
    """Clip is a cookie-cutter: the mask decides *where*, never *what*."""
    leases = fs([square(0, 0, 100)], [{"lease": "Smith"}])
    unit = fs([shapely.box(50, 50, 200, 200)], [{"unit": "A"}])

    out = ops.clip(leases, unit)

    assert len(out) == 1
    assert out.props[0] == {"lease": "Smith"}, "clip must not transfer mask attributes"
    assert out.geometry[0].area == pytest.approx(50 * 50)


def test_intersect_carries_both_attribute_sets_where_clip_carries_one() -> None:
    """The most common overlay mistake, and it is silent — both return a
    polygon of the same shape."""
    leases = fs([square(0, 0, 100)], [{"lease": "Smith"}])
    unit = fs([shapely.box(50, 50, 200, 200)], [{"unit": "A"}])

    clipped = ops.clip(leases, unit)
    crossed = ops.intersect(leases, unit)

    assert clipped.geometry[0].equals(crossed.geometry[0])
    assert crossed.props[0] == {"lease": "Smith", "unit": "A"}


def test_a_colliding_attribute_name_is_kept_under_a_prefix() -> None:
    """Two layers both carrying `name` is the normal case. Overwriting is how
    an overlay silently loses half its attributes."""
    left = fs([square(0, 0, 100)], [{"name": "left"}])
    right = fs([square(0, 0, 100)], [{"name": "right"}])

    out = ops.intersect(left, right)

    assert out.props[0] == {"name": "left", "right_name": "right"}


def test_erase_is_the_complement_of_clip() -> None:
    whole = fs([square(0, 0, 100)])
    mask = fs([shapely.box(50, 50, 200, 200)])

    kept = ops.clip(whole, mask)
    removed = ops.erase(whole, mask)

    assert kept.geometry[0].area + removed.geometry[0].area == pytest.approx(100 * 100)


def test_union_splits_overlaps_into_their_own_features() -> None:
    """Three groups out: left-only, right-only, and the shared piece carrying
    both attribute sets."""
    left = fs([square(0, 0, 100)], [{"a": 1}])
    right = fs([shapely.box(50, 0, 150, 100)], [{"b": 2}])

    out = ops.union_layers(left, right)

    assert len(out) == 3
    total = sum(g.area for g in out.geometry)
    assert total == pytest.approx(100 * 100 + 100 * 100 - 50 * 100)


def test_overlaying_layers_in_different_frames_is_refused() -> None:
    """**Not an empty result.** Coordinates in different frames do not
    overlap, so the honest-looking answer is "these layers do not touch"."""
    with pytest.raises(FrameMismatch, match="different analysis frames"):
        ops.clip(fs([square(0, 0)]), fs([square(0, 0)], frame=OTHER))


# --- dissolve -----------------------------------------------------------------


def test_dissolve_removes_the_shared_boundary_rather_than_wrapping() -> None:
    """`09` §9.1: Dissolve is a true union and Combine wraps into a multi-part
    feature. They produce different acreage, which is why neither may ever be
    labelled "merge"."""
    out = ops.dissolve(fs([square(0, 0, 10), square(10, 0, 10)]))

    assert len(out) == 1
    assert out.geometry[0].area == pytest.approx(200.0)
    assert out.geometry[0].geom_type == "Polygon", "a true union, not a MultiPolygon"


def test_dissolve_by_attribute_groups_and_reports_its_membership() -> None:
    source = fs(
        [square(0, 0, 10), square(10, 0, 10), square(100, 100, 10)],
        [{"op": "A"}, {"op": "A"}, {"op": "B"}],
    )

    out = ops.dissolve(source, by="op")

    assert len(out) == 2
    by_op = {record["op"]: record for record in out.props}
    assert by_op["A"]["source_features"] == 2
    assert by_op["B"]["source_features"] == 1


# --- joins and summaries -------------------------------------------------------


def test_a_spatial_join_repeats_a_feature_once_per_match() -> None:
    """What a join means, and what surprises people expecting a lookup."""
    well = fs([Point(5, 5)], [{"api": "42-001"}])
    zones = fs(
        [square(0, 0, 100), shapely.box(-10, -10, 50, 50)],
        [{"zone": "north"}, {"zone": "west"}],
    )

    out = ops.spatial_join(well, zones)

    assert len(out) == 2
    assert {record["zone"] for record in out.props} == {"north", "west"}


def test_an_inner_join_drops_unmatched_features_and_a_left_join_keeps_them() -> None:
    wells = fs([Point(5, 5), Point(9999, 9999)], [{"api": "a"}, {"api": "b"}])
    zones = fs([square(0, 0, 100)], [{"zone": "north"}])

    assert len(ops.spatial_join(wells, zones, how="inner")) == 1
    kept = ops.spatial_join(wells, zones, how="left")
    assert len(kept) == 2
    assert "zone" not in kept.props[1]


def test_summarize_within_keeps_a_zone_that_contains_nothing() -> None:
    """An empty zone is a result. Dropping it turns "no wells here" into "no
    such lease", which is a different statement."""
    zones = fs([square(0, 0, 100), square(500, 500, 100)], [{"id": 1}, {"id": 2}])
    wells = fs([Point(5, 5), Point(9, 9)], [{"ip": 100.0}, {"ip": 300.0}])

    out = ops.summarize_within(
        zones,
        wells,
        stats=[Stat(op="count", name="wells"), Stat(op="mean", name="avg_ip", field="ip")],
    )

    assert len(out) == 2
    assert out.props[0]["wells"] == 2
    assert out.props[0]["avg_ip"] == pytest.approx(200.0)
    assert out.props[1]["wells"] == 0
    assert out.props[1]["avg_ip"] is None


def test_a_non_numeric_value_is_skipped_rather_than_failing_the_operation() -> None:
    """One bad row in a 50,000-feature layer should not lose the other 49,999."""
    zones = fs([square(0, 0, 100)], [{}])
    wells = fs([Point(1, 1), Point(2, 2)], [{"ip": "n/a"}, {"ip": 400.0}])

    out = ops.summarize_within(zones, wells, stats=[Stat(op="mean", name="m", field="ip")])

    assert out.props[0]["m"] == pytest.approx(400.0)


def test_a_statistic_without_a_field_is_refused_unless_it_is_a_count() -> None:
    with pytest.raises(DegenerateInput, match="needs a field"):
        Stat(op="sum", name="total")


# --- binning -------------------------------------------------------------------


def test_binning_emits_only_occupied_cells() -> None:
    """A 10,000-cell grid over forty wells in one corner is 9,960 features
    carrying count=0, which costs more to draw than it explains."""
    points = [Point(1, 1), Point(2, 2), Point(1000, 1000)]

    out = ops.aggregate_points(fs(points), cell_size=100.0)

    assert len(out) == 2
    assert sorted(record["count"] for record in out.props) == [1, 2]


def test_every_point_lands_in_exactly_one_hexagon() -> None:
    """A tessellation that double-counts or drops points is not a tessellation,
    and a density map made from one is wrong everywhere."""
    rng = np.random.default_rng(11)
    points = [Point(x, y) for x, y in rng.uniform(-500, 500, size=(400, 2))]

    out = ops.hexbin(fs(points), cell_size=100.0)

    assert sum(record["count"] for record in out.props) == 400


def test_hexagon_cells_contain_the_points_they_counted() -> None:
    """**The bug this caught.** `_hex_keys` builds a lattice whose
    nearest-neighbour distance is `cell_size`, so each cell is a hexagon of
    *circumradius* `cell_size / sqrt(3)` — but `_hexagon` was first called with
    `cell_size / 2`, the *inradius*, a factor of 1.155 smaller. The cells left
    gaps, and a point in a gap was counted in a cell that did not contain it:
    the map would show a number beside its hexagon rather than inside it.

    Tested as containment rather than intersection, because adjacent hexagons
    share edges and `intersects` counts a boundary point in both.
    """
    rng = np.random.default_rng(3)
    points = [Point(x, y) for x, y in rng.uniform(0, 300, size=(120, 2))]

    out = ops.hexbin(fs(points), cell_size=60.0)

    tree = shapely.STRtree(out.geometry)
    for point in points:
        containing = tree.query(point, predicate="within")
        assert len(containing) == 1, f"{point.wkt} is inside {len(containing)} cells"

    for cell, record in zip(out.geometry, out.props, strict=True):
        inside = [p for p in points if cell.contains(p)]
        assert len(inside) == record["count"], "cell geometry disagrees with its own count"


def test_a_non_positive_cell_size_is_refused_with_the_units() -> None:
    with pytest.raises(DegenerateInput, match="usft"):
        ops.hexbin(fs([Point(0, 0)]), cell_size=0)


# --- voronoi --------------------------------------------------------------------


def test_voronoi_cells_carry_the_attributes_of_the_point_inside_them() -> None:
    """`voronoi_polygons` does not promise cell order matches input order.
    Matching by index would attach every attribute to the wrong cell and the
    map would look entirely plausible."""
    points = [Point(0, 0), Point(100, 0), Point(50, 100), Point(50, 40)]
    source = fs(points, [{"well": name} for name in ("a", "b", "c", "d")])

    out = ops.voronoi(source)

    for cell, record in zip(out.geometry, out.props, strict=True):
        owner = next(p for p, r in zip(points, source.props, strict=True) if r == record)
        assert cell.buffer(1e-9).contains(owner), f"cell for {record} does not contain it"


def test_voronoi_is_clipped_so_outer_cells_are_not_arbitrary() -> None:
    """Unclipped, the outer cells run to the envelope and say more about the
    bounding box than about the data."""
    points = [Point(0, 0), Point(100, 0), Point(50, 100)]
    boundary = fs([shapely.box(-10, -10, 110, 110)])

    out = ops.voronoi(fs(points), clip_to=boundary)

    total = sum(g.area for g in out.geometry)
    assert total == pytest.approx(120 * 120, rel=0.01)


def test_voronoi_needs_at_least_three_points() -> None:
    with pytest.raises(DegenerateInput, match="at least 3 points"):
        ops.voronoi(fs([Point(0, 0), Point(1, 1)]))


# --- dispatch --------------------------------------------------------------------


def test_every_catalogued_operation_dispatches() -> None:
    """The guard against an operation being added to the table and not wired
    up — which fails only when someone asks for it."""
    squares = fs([square(0, 0, 10), square(10, 0, 10)], [{"g": "x"}, {"g": "x"}])
    points = fs([Point(1, 1), Point(2, 2), Point(8, 3)], [{"v": 1.0}] * 3)
    params: dict[str, dict[str, Any]] = {
        "buffer": {"distance": 1.0},
        "concave_hull": {"ratio": 0.5},
        "aggregate_points": {"cell_size": 5.0},
        "hexbin": {"cell_size": 5.0},
        "dissolve": {"by": "g"},
        "summarize_within": {"stats": [Stat(op="count", name="n")]},
    }
    for op in operations():
        first = points if op in ("voronoi", "aggregate_points", "hexbin") else squares
        inputs = (
            [first]
            if op == "voronoi"
            or op
            in (
                "buffer",
                "centroid",
                "convex_hull",
                "concave_hull",
                "dissolve",
                "aggregate_points",
                "hexbin",
            )
            else [squares, fs([shapely.box(5, 0, 25, 10)], [{"h": 2}])]
        )
        result = aggregate(op, inputs, **params.get(op, {}))
        assert isinstance(result, FeatureSet), op
        assert result.frame == TEXAS, f"{op} lost the analysis frame"


def test_an_unknown_operation_lists_the_ones_that_exist() -> None:
    with pytest.raises(DegenerateInput, match="dissolve"):
        aggregate("smoosh", [fs([Point(0, 0)])])


def test_an_overlay_given_one_layer_says_what_is_missing() -> None:
    with pytest.raises(DegenerateInput, match="second layer"):
        aggregate("clip", [fs([square(0, 0)])])


# --- the value object -------------------------------------------------------------


def test_geometry_and_props_must_be_the_same_length() -> None:
    """They are zipped by position, so a mismatch attaches every attribute
    after the first gap to the wrong feature."""
    with pytest.raises(DegenerateInput, match="zipped by position"):
        FeatureSet(geometry=np.array([Point(0, 0)], dtype=object), props=[], frame=TEXAS)


def test_a_feature_set_round_trips_through_arrow() -> None:
    source = fs([Point(1, 2), LineString([(0, 0), (1, 1)])], [{"a": 1}, {"b": "two"}])

    back = FeatureSet.from_arrow(source.to_arrow(), TEXAS)

    assert [g.wkt for g in back.geometry] == [g.wkt for g in source.geometry]
    assert back.props == source.props
