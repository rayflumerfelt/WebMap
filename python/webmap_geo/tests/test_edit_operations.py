"""The editing operations catalog. `09-editing.md` §11.

Each of these has a rule the spec states and a failure the rule prevents, and
those are what the tests are about. A partial cut that silently does nothing; a
dissolve that loses an operator name without saying so; a smoothed boundary that
quietly stops matching its neighbour. None of the three shows up as an error.
"""

from __future__ import annotations

import pytest
from shapely.geometry import LineString, MultiPolygon, Polygon

from webmap_geo.edit.combine import (
    AttributePolicy,
    attribute_preview,
    combine,
    dissolve,
    explode,
    resolve_attributes,
)
from webmap_geo.edit.shape import (
    TopologyWarning,
    reshape,
    shared_vertices,
    simplification_loss,
    simplify,
    smooth,
    warn_about,
)
from webmap_geo.edit.split import (
    cut_from_points,
    provenance,
    split_features,
    split_geometry,
)
from webmap_geo.exceptions import DegenerateInput

#: A square lease, 1000 ft on a side, in the frame's own units.
SQUARE = Polygon([(0, 0), (1000, 0), (1000, 1000), (0, 1000)])
#: Its neighbour, sharing the eastern boundary exactly.
EAST = Polygon([(1000, 0), (2000, 0), (2000, 1000), (1000, 1000)])


class TestSplit:
    def test_a_cut_across_a_lease_makes_two(self) -> None:
        cut = LineString([(-100, 500), (1100, 500)])

        result = split_geometry(SQUARE, cut)

        assert len(result.parts) == 2
        assert sum(part.area for part in result.parts) == pytest.approx(SQUARE.area)

    def test_a_partial_cut_is_refused_with_a_reason(self) -> None:
        """The failure this prevents: the user draws most of the way across,
        presses Apply, and the map does not change. Nothing is wrong with the
        data and nothing tells them why."""
        cut = LineString([(-100, 500), (500, 500)])

        with pytest.raises(DegenerateInput, match="ends inside the feature"):
            split_geometry(SQUARE, cut)

    def test_a_cut_with_both_ends_inside_says_both(self) -> None:
        cut = LineString([(200, 500), (800, 500)])

        with pytest.raises(DegenerateInput, match="both ends are"):
            split_geometry(SQUARE, cut)

    def test_a_line_splits_at_every_crossing(self) -> None:
        """A fault trace crossing a horizon three times is three cuts and four
        pieces, not one cut and two."""
        horizon = LineString([(0, 0), (3000, 0)])
        cut = LineString(
            [(500, -10), (500, 10), (1500, 10), (1500, -10), (2500, -10), (2500, 10)]
        )

        result = split_geometry(horizon, cut)

        assert len(result.parts) == 4

    def test_one_cut_can_make_more_than_two_parts(self) -> None:
        """Not an edge case: cutting a crescent is the ordinary result."""
        crescent = Polygon(
            [(0, 0), (1000, 0), (1000, 1000), (0, 1000)],
            [[(200, 200), (800, 200), (800, 800), (200, 800)]],
        )
        cut = LineString([(-100, 500), (1100, 500)])

        result = split_geometry(crescent, cut)

        assert len(result.parts) >= 2

    def test_an_invalid_geometry_is_refused_before_it_spreads(self) -> None:
        bowtie = Polygon([(0, 0), (1000, 1000), (1000, 0), (0, 1000)])

        with pytest.raises(DegenerateInput, match="Validate"):
            split_geometry(bowtie, LineString([(-100, 500), (1100, 500)]))

    def test_a_feature_the_cut_misses_comes_back_whole(self) -> None:
        """So the caller can write one command for everything the user
        selected, without deciding per feature whether anything happened.

        The far lease, not the adjacent one: a cut ending at x=1100 reaches
        *into* the eastern neighbour, which is the partial cut refused above.
        """
        far = Polygon([(5000, 5000), (6000, 5000), (6000, 6000), (5000, 6000)])
        cut = LineString([(-100, 500), (1100, 500)])

        results = split_features([("a", SQUARE, {}), ("b", far, {})], cut)

        assert [len(parts) for _, parts, _ in results] == [2, 1]

    def test_provenance_says_where_a_part_came_from(self) -> None:
        """The first time somebody asks why one lease is now three, the answer
        is in the data rather than in whoever remembers doing it."""
        assert provenance({"operator": "Smith"}, "lease-7") == {
            "operator": "Smith",
            "split_from_id": "lease-7",
        }


class TestCutLine:
    def test_drops_a_double_clicked_vertex(self) -> None:
        """A zero-length segment makes the line invalid for `ops.split`, with
        an error that says nothing about double-clicking."""
        cut = cut_from_points([(0, 0), (0, 0), (100, 100)])

        assert list(cut.coords) == [(0.0, 0.0), (100.0, 100.0)]

    def test_refuses_a_line_of_one_point(self) -> None:
        with pytest.raises(DegenerateInput, match="at least two distinct points"):
            cut_from_points([(0, 0), (0, 0)])


class TestCombine:
    def test_wraps_without_moving_anything(self) -> None:
        """Two leases that did not touch still do not touch — this changes how
        many records there are, not where anything is."""
        far = Polygon([(5000, 5000), (6000, 5000), (6000, 6000), (5000, 6000)])

        combined = combine([SQUARE, far])

        assert combined.geom_type == "MultiPolygon"
        assert combined.area == pytest.approx(SQUARE.area + far.area)

    def test_refuses_a_mixed_selection(self) -> None:
        """A multipolygon holding a line is not a thing, and the alternative is
        a GeometryCollection, which most of the pipeline cannot draw."""
        with pytest.raises(DegenerateInput, match="different kinds"):
            combine([SQUARE, LineString([(0, 0), (1, 1)])])

    def test_refuses_one_feature(self) -> None:
        with pytest.raises(DegenerateInput, match="at least two"):
            combine([SQUARE])

    def test_flattens_an_already_multi_part_input(self) -> None:
        """Combining a multipolygon with a polygon gives one multipolygon of
        three parts, not a nested thing no format can store."""
        multi = MultiPolygon([SQUARE, EAST])
        far = Polygon([(5000, 5000), (6000, 5000), (6000, 6000), (5000, 6000)])

        combined = combine([multi, far])

        assert isinstance(combined, MultiPolygon)
        assert len(combined.geoms) == 3


class TestExplode:
    def test_splits_a_multi_part_feature(self) -> None:
        assert len(explode(MultiPolygon([SQUARE, EAST]))) == 2

    def test_a_single_part_feature_comes_back_as_one(self) -> None:
        """Refusing the whole operation because one feature in a mixed
        selection was already single-part would be the software arguing with a
        reasonable request."""
        assert explode(SQUARE) == [SQUARE]


class TestDissolve:
    def test_removes_the_boundary_between_neighbours(self) -> None:
        """The difference from Combine, and the reason §9.1 forbids calling
        both of them Merge."""
        united, _ = dissolve([SQUARE, EAST], [{}, {}], AttributePolicy.LARGEST)

        assert united.geom_type == "Polygon"
        assert united.area == pytest.approx(SQUARE.area + EAST.area)

    def test_refuses_lines_and_says_what_to_use(self) -> None:
        with pytest.raises(DegenerateInput, match="Combine makes one multi-part"):
            dissolve(
                [LineString([(0, 0), (1, 1)]), LineString([(1, 1), (2, 2)])],
                [{}, {}],
                AttributePolicy.LARGEST,
            )

    def test_sum_numeric_sums_the_acreage_and_keeps_the_biggest_name(self) -> None:
        big = Polygon([(0, 0), (2000, 0), (2000, 2000), (0, 2000)])
        props = [
            {"operator": "Smith", "acres": 23.0},
            {"operator": "Jones", "acres": 92.0},
        ]

        resolved = resolve_attributes([SQUARE, big], props, AttributePolicy.SUM_NUMERIC)

        assert resolved["acres"] == pytest.approx(115.0)
        assert resolved["operator"] == "Jones"

    def test_largest_takes_everything_from_one_feature(self) -> None:
        big = Polygon([(0, 0), (2000, 0), (2000, 2000), (0, 2000)])
        props = [{"operator": "Smith", "acres": 23.0}, {"operator": "Jones", "acres": 92.0}]

        resolved = resolve_attributes([SQUARE, big], props, AttributePolicy.LARGEST)

        assert resolved == {"operator": "Jones", "acres": 92.0}

    def test_common_drops_what_disagrees(self) -> None:
        """The only policy that never invents a value: a field the inputs do
        not agree on is not carried at all."""
        props = [
            {"operator": "Smith", "formation": "Wolfcamp A"},
            {"operator": "Jones", "formation": "Wolfcamp A"},
        ]

        resolved = resolve_attributes([SQUARE, EAST], props, AttributePolicy.COMMON)

        assert resolved == {"formation": "Wolfcamp A"}

    def test_a_boolean_is_not_summed(self) -> None:
        """`True + True == 2` in Python, and a `is_producing` column that comes
        back as 2 is worse than one that comes back wrong."""
        props = [{"is_producing": True}, {"is_producing": True}]

        resolved = resolve_attributes([SQUARE, EAST], props, AttributePolicy.SUM_NUMERIC)

        assert resolved["is_producing"] is True

    def test_mismatched_rows_are_refused(self) -> None:
        with pytest.raises(DegenerateInput, match="must line up"):
            resolve_attributes([SQUARE, EAST], [{}], AttributePolicy.LARGEST)

    def test_the_preview_shows_all_three_answers(self) -> None:
        """The question "what happens to my attributes" is answered by seeing
        the three results side by side, not by reading three descriptions."""
        props = [{"operator": "Smith", "acres": 23.0}, {"operator": "Jones", "acres": 92.0}]

        preview = attribute_preview(props, [SQUARE, EAST])

        assert set(preview) == {policy.value for policy in AttributePolicy}
        assert preview["sum_numeric"]["acres"] == pytest.approx(115.0)


class TestSmooth:
    def test_rounds_the_corners(self) -> None:
        line = LineString([(0, 0), (1000, 0), (1000, 1000)])

        smoothed = smooth(line, 0.5)

        assert len(smoothed.coords) > len(line.coords)

    def test_zero_smoothing_changes_nothing(self) -> None:
        line = LineString([(0, 0), (1000, 0), (1000, 1000)])

        assert smooth(line, 0.0) == line

    def test_a_polygon_keeps_its_holes(self) -> None:
        """A doughnut lease smoothed into a solid one is a lease that gained
        the acreage of its hole."""
        doughnut = Polygon(
            [(0, 0), (1000, 0), (1000, 1000), (0, 1000)],
            [[(300, 300), (700, 300), (700, 700), (300, 700)]],
        )

        smoothed = smooth(doughnut, 0.5)

        assert isinstance(smoothed, Polygon)
        assert len(smoothed.interiors) == 1

    def test_refuses_smoothing_past_the_cap(self) -> None:
        """`05` §7's cap: above it a smoothed line drifts measurably off the
        geometry it claims to be."""
        with pytest.raises(DegenerateInput, match="drifts measurably"):
            smooth(LineString([(0, 0), (1, 1), (2, 0)]), 99.0)


class TestSimplify:
    def test_drops_vertices_within_the_tolerance(self) -> None:
        wobbly = LineString([(0, 0), (500, 1), (1000, 0), (1500, 1), (2000, 0)])

        simplified = simplify(wobbly, 10.0)

        assert len(simplified.coords) == 2

    def test_preserves_topology(self) -> None:
        """Without it Shapely produces a self-intersecting polygon from a
        convoluted one, and the next save fails pointing at the user's geometry
        rather than at the simplification that did it."""
        convoluted = Polygon([(0, 0), (1000, 0), (1000, 1000), (500, 10), (0, 1000)])

        assert simplify(convoluted, 50.0).is_valid

    def test_an_enormous_tolerance_leaves_a_valid_shape(self) -> None:
        """`preserve_topology=True` never returns empty — measured at a
        thousand times the feature's size. There is no empty case to guard, and
        an earlier revision guarded one anyway."""
        simplified = simplify(SQUARE, 100_000.0)

        assert not simplified.is_empty
        assert simplified.is_valid

    def test_loss_reports_what_the_tolerance_took(self) -> None:
        """The number worth showing beside §11.5's live preview: a lease that
        lost 30% of its area is not a simplified lease, and a vertex count does
        not say so."""
        assert simplification_loss(SQUARE, simplify(SQUARE, 1.0)) == pytest.approx(0.0)
        assert simplification_loss(SQUARE, simplify(SQUARE, 100_000.0)) > 0.4

    def test_loss_of_an_empty_geometry_is_nothing(self) -> None:
        assert simplification_loss(Polygon(), Polygon()) == 0.0

    def test_refuses_a_zero_tolerance_and_names_the_units(self) -> None:
        with pytest.raises(DegenerateInput, match="layer's own units"):
            simplify(SQUARE, 0.0)


class TestTopologyWarning:
    def test_finds_the_vertices_a_neighbour_shares(self) -> None:
        """These are the coordinates that stop matching if this feature moves
        and the neighbour does not."""
        shared = shared_vertices(SQUARE, [EAST])

        assert sorted(shared) == [(1000.0, 0.0), (1000.0, 1000.0)]

    def test_says_nothing_about_a_feature_that_touches_nothing(self) -> None:
        far = Polygon([(5000, 5000), (6000, 5000), (6000, 6000), (5000, 6000)])

        assert warn_about(SQUARE, [far]) is None

    def test_counts_both_the_vertices_and_the_neighbours(self) -> None:
        south = Polygon([(0, -1000), (1000, -1000), (1000, 0), (0, 0)])

        warning = warn_about(SQUARE, [EAST, south])

        assert isinstance(warning, TopologyWarning)
        assert warning.neighbour_count == 2
        assert "sliver" in warning.message

    def test_a_vertex_shared_with_two_neighbours_counts_once(self) -> None:
        """The caller is counting how many vertices will break, and (1000, 0)
        is one vertex however many features hold it."""
        south = Polygon([(0, -1000), (1000, -1000), (1000, 0), (0, 0)])

        shared = shared_vertices(SQUARE, [EAST, south])

        assert len(shared) == len(set(shared))


class TestReshape:
    def test_replaces_the_boundary_between_two_crossings(self) -> None:
        bulge = LineString([(1100, 200), (700, 500), (1100, 800)])

        reshaped = reshape(SQUARE, bulge)

        assert reshaped.area < SQUARE.area
        assert reshaped.is_valid

    def test_refuses_one_crossing(self) -> None:
        with pytest.raises(DegenerateInput, match="crosses the boundary 1 time"):
            reshape(SQUARE, LineString([(500, 500), (1500, 500)]))

    def test_refuses_more_than_two_crossings(self) -> None:
        """Three crossings leave two candidate answers, and choosing one
        silently is a coin toss the user does not know was flipped."""
        zigzag = LineString([(1100, 100), (900, 300), (1100, 500), (900, 700), (1100, 900)])

        with pytest.raises(DegenerateInput, match="cross exactly"):
            reshape(SQUARE, zigzag)

    def test_refuses_a_multipolygon_and_says_what_to_do(self) -> None:
        with pytest.raises(DegenerateInput, match="Explode it first"):
            reshape(MultiPolygon([SQUARE, EAST]), LineString([(0, 0), (1, 1)]))
