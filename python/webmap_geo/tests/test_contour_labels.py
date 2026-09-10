"""Contour labels and their gaps. `adr/0015`.

The three rules worth protecting are the ones that make a gapped contour read
like a map instead of like a rendering fault: a short line is left whole, a
closed ring does not gain a seam, and a label never reads upside down.
"""

from __future__ import annotations

import math

import pytest
from shapely.geometry import LineString

from webmap_geo.contour.labels import (
    MIN_LENGTH_IN_GAPS,
    gap_length,
    label_contour,
)


def horizontal(length: float) -> LineString:
    return LineString([(0.0, 0.0), (length, 0.0)])


def total_length(pieces: list[LineString]) -> float:
    return sum(piece.length for piece in pieces)


class TestGapLength:
    def test_scales_with_the_text(self) -> None:
        short = gap_length("-800", metres_per_pixel=10.0)
        long = gap_length("-12,800", metres_per_pixel=10.0)

        assert long > short

    def test_scales_with_the_reference_scale(self) -> None:
        """A gap is a distance on the ground, so it doubles when each pixel
        covers twice as much of it."""
        near = gap_length("-12,800", metres_per_pixel=5.0)
        far = gap_length("-12,800", metres_per_pixel=10.0)

        assert far == pytest.approx(near * 2.0)

    def test_refuses_to_invent_a_scale(self) -> None:
        """`adr/0015`: a label is a fixed number of pixels and a gap a fixed
        number of feet, so the two agree at exactly one scale — and only the
        caller knows which."""
        with pytest.raises(ValueError, match="no sensible default"):
            gap_length("-12,800", metres_per_pixel=0.0)


class TestShortLines:
    def test_a_short_contour_is_left_whole(self) -> None:
        """Cutting a gap out of it leaves two stubs, which is worse than an
        unlabelled contour."""
        line = horizontal(MIN_LENGTH_IN_GAPS * 100.0 - 1.0)

        result = label_contour(line, -12800.0, gap=100.0, spacing=500.0)

        assert result.labels == []
        assert len(result.pieces) == 1
        assert result.pieces[0].equals(line)

    def test_a_long_contour_is_cut(self) -> None:
        line = horizontal(1000.0)

        result = label_contour(line, -12800.0, gap=100.0, spacing=500.0)

        assert len(result.labels) >= 1
        assert total_length(result.pieces) == pytest.approx(1000.0 - 100.0 * len(result.labels))


class TestPlacement:
    def test_one_label_sits_in_the_middle(self) -> None:
        """Half a spacing in, so a contour long enough for exactly one label
        does not get it at an end."""
        line = horizontal(1000.0)

        result = label_contour(line, -12800.0, gap=100.0, spacing=1000.0)

        assert len(result.labels) == 1
        assert result.labels[0].point.x == pytest.approx(500.0)

    def test_labels_repeat_along_a_long_contour(self) -> None:
        line = horizontal(5000.0)

        result = label_contour(line, -12800.0, gap=100.0, spacing=1000.0)

        xs = [label.point.x for label in result.labels]
        assert xs == pytest.approx([500.0, 1500.0, 2500.0, 3500.0, 4500.0])

    def test_no_gap_is_cut_at_an_end(self) -> None:
        """A gap at the very end is not a break in the line, it is a shortened
        line — and it reads as a contour that stops early."""
        line = horizontal(1000.0)

        result = label_contour(line, -12800.0, gap=100.0, spacing=200.0)

        first, last = result.pieces[0], result.pieces[-1]
        assert first.coords[0] == (0.0, 0.0)
        assert last.coords[-1] == (1000.0, 0.0)

    def test_the_label_sits_in_its_gap(self) -> None:
        """The whole point: the anchor is the middle of the break, so nothing
        is drawn under the text."""
        line = horizontal(1000.0)

        result = label_contour(line, -12800.0, gap=100.0, spacing=1000.0)

        anchor = result.labels[0].point
        for piece in result.pieces:
            assert piece.distance(anchor) >= 50.0 - 1e-6


class TestBearing:
    def test_an_east_west_contour_is_not_rotated(self) -> None:
        result = label_contour(horizontal(1000.0), -12800.0, gap=100.0, spacing=1000.0)

        assert result.labels[0].bearing == pytest.approx(0.0)

    def test_a_north_south_contour_reads_bottom_to_top(self) -> None:
        """-90 rather than +90: on a screen whose y grows downward, that is the
        rotation that leaves the text running up the line rather than down it."""
        line = LineString([(0.0, 0.0), (0.0, 1000.0)])

        result = label_contour(line, -12800.0, gap=100.0, spacing=1000.0)

        assert result.labels[0].bearing == pytest.approx(-90.0)

    def test_direction_of_digitising_does_not_change_the_label(self) -> None:
        """Two adjacent contours traced in opposite directions must not read in
        opposite directions — that looks like a bug in the renderer."""
        north = label_contour(
            LineString([(0.0, 0.0), (0.0, 1000.0)]), -12800.0, gap=100.0, spacing=1000.0
        )
        south = label_contour(
            LineString([(0.0, 1000.0), (0.0, 0.0)]), -12800.0, gap=100.0, spacing=1000.0
        )

        assert north.labels[0].bearing == pytest.approx(south.labels[0].bearing)

    def test_a_diagonal_follows_the_line(self) -> None:
        line = LineString([(0.0, 0.0), (1000.0, 1000.0)])

        result = label_contour(line, -12800.0, gap=100.0, spacing=1000.0)

        # Up and to the right on the ground is up and to the right on screen,
        # which is a rotation of -45 clockwise.
        assert result.labels[0].bearing == pytest.approx(-45.0)

    def test_near_vertical_contours_all_read_the_same_way(self) -> None:
        """Two contours a fifth of a degree apart came out at +89.9 and -89.9 —
        both readable, one reading up the line and the other down it, on the
        same map. That reads as a renderer fault."""
        leaning_left = LineString([(0.0, 0.0), (-3.0, 1000.0)])
        leaning_right = LineString([(0.0, 0.0), (3.0, 1000.0)])

        left = label_contour(leaning_left, -12800.0, gap=100.0, spacing=1000.0)
        right = label_contour(leaning_right, -12800.0, gap=100.0, spacing=1000.0)

        # Within a few degrees of each other, not 180 apart. The tilt is
        # invisible; the reading direction is not.
        assert abs(left.labels[0].bearing - right.labels[0].bearing) < 20.0
        assert left.labels[0].bearing < 0.0
        assert right.labels[0].bearing < 0.0

    def test_a_gentle_slope_still_follows_its_line(self) -> None:
        """The snap is a band around vertical, not a rule that every label is
        vertical."""
        line = LineString([(0.0, 0.0), (1000.0, 300.0)])

        result = label_contour(line, -12800.0, gap=100.0, spacing=1000.0)

        assert result.labels[0].bearing == pytest.approx(-16.699, abs=0.01)

    def test_a_wiggle_does_not_spin_the_label(self) -> None:
        """A contour from a noisy grid wiggles vertex to vertex. A bearing
        taken from one short segment makes the label wander while the line
        does not."""
        coords = [(x, 5.0 if index % 2 else -5.0) for index, x in enumerate(range(0, 1001, 10))]
        line = LineString(coords)

        result = label_contour(line, -12800.0, gap=200.0, spacing=1000.0)

        assert abs(result.labels[0].bearing) < 15.0


class TestClosedContours:
    def circle(self, radius: float = 500.0, points: int = 180) -> LineString:
        return LineString(
            [
                (
                    radius * math.cos(2 * math.pi * index / points),
                    radius * math.sin(2 * math.pi * index / points),
                )
                for index in range(points + 1)
            ]
        )

    def test_a_ring_cut_once_comes_back_as_one_line(self) -> None:
        """Not two pieces with a seam where the ring happened to start —
        `adr/0015`. The ring's start vertex is an artefact of tracing and a
        break there is a break nobody chose."""
        ring = self.circle()

        result = label_contour(ring, -12800.0, gap=100.0, spacing=ring.length + 1.0)

        assert len(result.labels) == 1
        assert len(result.pieces) == 1

    def test_the_stitched_piece_is_continuous(self) -> None:
        """Its length is the ring's, less the gap. A piece assembled wrongly
        would show as a chord across the middle."""
        ring = self.circle()

        result = label_contour(ring, -12800.0, gap=100.0, spacing=ring.length + 1.0)

        assert result.pieces[0].length == pytest.approx(ring.length - 100.0, rel=1e-3)

    def test_a_ring_takes_labels_all_the_way_round(self) -> None:
        ring = self.circle()

        result = label_contour(ring, -12800.0, gap=60.0, spacing=ring.length / 4.0)

        assert len(result.labels) == 4
        assert total_length(result.pieces) == pytest.approx(ring.length - 240.0, rel=1e-3)


class TestArguments:
    def test_a_zero_gap_is_refused(self) -> None:
        with pytest.raises(ValueError, match="gap must be positive"):
            label_contour(horizontal(1000.0), -12800.0, gap=0.0, spacing=100.0)

    def test_a_zero_spacing_is_refused(self) -> None:
        with pytest.raises(ValueError, match="spacing must be positive"):
            label_contour(horizontal(1000.0), -12800.0, gap=10.0, spacing=0.0)
