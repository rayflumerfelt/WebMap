"""Contour features: which lines get cut, and what the labels carry. `adr/0015`.

`tests/test_contour_job.py` proves this end to end against a real grid. This
proves the rules exactly, against lines chosen to exercise them — through a
real grid, "an intermediate contour is not cut" is asserted through however
many disjoint segments a level happened to trace, which is a different claim.
"""

from __future__ import annotations

import pytest
from shapely.geometry import LineString

from webmap_core.services.contours import ContourRequest, contour_features, label_text
from webmap_geo.contour.lines import ContourLine
from webmap_geo.crs import frame_for
from webmap_geo.grid import GridDefinition

TEXAS_CENTRAL = 2277

#: A 100 × 100 grid of 500 ft cells: 50,000 ft across, about a township and a
#: half, which is the scale these numbers were chosen against.
GRID = GridDefinition(
    xmin=1_200_000.0,
    ymin=6_800_000.0,
    cell_size=500.0,
    nx=100,
    ny=100,
    frame=frame_for(TEXAS_CENTRAL),
)


def straight(value: float, *, is_index: bool, length: float = 40_000.0) -> ContourLine:
    return ContourLine(
        geometry=LineString([(1_200_000.0, 6_800_000.0), (1_200_000.0 + length, 6_800_000.0)]),
        value=value,
        is_index=is_index,
        closed=False,
    )


def kinds(props: list[dict[str, object]]) -> list[object]:
    return [entry["kind"] for entry in props]


class TestWhichLinesAreCut:
    def test_an_index_contour_is_cut_and_labelled(self) -> None:
        geometry, props = contour_features(
            [straight(-12800.0, is_index=True)],
            [-12800.0],
            grid=GRID,
            request=ContourRequest(dataset_id=None),  # type: ignore[arg-type]
        )

        assert "label" in kinds(props)
        assert kinds(props).count("contour") > 1, "the line was not cut"
        assert len(geometry) == len(props)

    def test_an_intermediate_contour_is_left_whole(self) -> None:
        """Only index contours are labelled, so only index contours are cut. A
        gap in an unlabelled line is a break with nothing in it."""
        geometry, props = contour_features(
            [straight(-12600.0, is_index=False)],
            [-12800.0, -12600.0],
            grid=GRID,
            request=ContourRequest(dataset_id=None),  # type: ignore[arg-type]
        )

        assert kinds(props) == ["contour"]
        assert len(geometry) == 1
        assert props[0]["label"] is None
        assert props[0]["bearing"] is None

    def test_a_short_index_contour_keeps_its_geometry(self) -> None:
        """Below the threshold there is no room for a label, and cutting one in
        anyway leaves two stubs."""
        geometry, props = contour_features(
            [straight(-12800.0, is_index=True, length=200.0)],
            [-12800.0],
            grid=GRID,
            request=ContourRequest(dataset_id=None),  # type: ignore[arg-type]
        )

        assert kinds(props) == ["contour"]
        assert geometry[0].length == pytest.approx(200.0)


class TestLabelText:
    def test_integral_levels_read_as_integers(self) -> None:
        """ "-12,800.00" on a structure map is four characters of noise on every
        contour."""
        assert label_text(-12800.0, [-12800.0, -12600.0]) == "-12,800"

    def test_fractional_levels_keep_their_decimals(self) -> None:
        assert label_text(1.25, [1.0, 1.25, 1.5]) == "1.25"

    def test_the_label_travels_with_the_geometry(self) -> None:
        """The gap was cut for this exact string (`adr/0015`), so a style that
        formatted the number itself could render one that does not fit."""
        _, props = contour_features(
            [straight(-12800.0, is_index=True)],
            [-12800.0],
            grid=GRID,
            request=ContourRequest(dataset_id=None),  # type: ignore[arg-type]
        )

        labels = [entry for entry in props if entry["kind"] == "label"]
        assert labels
        assert all(entry["label"] == "-12,800" for entry in labels)


class TestScale:
    def test_a_wider_reference_scale_cuts_a_longer_gap(self) -> None:
        """A gap is a distance on the ground, and the caller names the scale it
        is cut for."""
        lengths = []
        for scale in (10.0, 40.0):
            geometry, props = contour_features(
                [straight(-12800.0, is_index=True)],
                [-12800.0],
                grid=GRID,
                request=ContourRequest(
                    dataset_id=None,  # type: ignore[arg-type]
                    label_scale=scale,
                    label_spacing=40_000.0,
                ),
            )
            drawn = sum(
                line.length
                for line, entry in zip(geometry, props, strict=True)
                if entry["kind"] == "contour"
            )
            lengths.append(drawn)

        assert lengths[1] < lengths[0], "the wider scale cut no more line away"

    def test_labels_are_never_packed_tighter_than_their_gaps(self) -> None:
        """A spacing narrower than the gap would leave a contour that is more
        break than line. The service widens it rather than refusing, because
        the default spacing is derived and the caller did not choose it."""
        geometry, props = contour_features(
            [straight(-12800.0, is_index=True)],
            [-12800.0],
            grid=GRID,
            request=ContourRequest(
                dataset_id=None,  # type: ignore[arg-type]
                label_scale=40.0,
                label_spacing=1.0,
            ),
        )

        drawn = [
            line.length
            for line, entry in zip(geometry, props, strict=True)
            if entry["kind"] == "contour"
        ]
        assert sum(drawn) > 0.0, "every scrap of line was cut away"
