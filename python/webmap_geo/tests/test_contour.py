"""Contouring. `05-geoprocessing.md` §7.

`12-roadmap.md` Phase 4: "contour intervals are round numbers a geologist would
choose." That is the acceptance criterion, and it is a statement about reading
rather than about arithmetic — which is why the tests here are mostly about
what appears on the map.
"""

from __future__ import annotations

import numpy as np
import pytest

from webmap_geo.contour.lines import (
    MAX_SMOOTHING,
    auto_levels,
    contour_grid,
    index_levels,
)
from webmap_geo.exceptions import DegenerateInput
from webmap_geo.frame import AnalysisFrame
from webmap_geo.interpolate.grid import GridDefinition

TEXAS = AnalysisFrame(srid=2277, units="usft")


def grid(nx: int = 41, ny: int = 41, cell: float = 100.0) -> GridDefinition:
    return GridDefinition(xmin=0.0, ymin=0.0, cell_size=cell, nx=nx, ny=ny, frame=TEXAS)


def dome(g: GridDefinition, peak: float = 200.0, base: float = 100.0) -> np.ndarray:
    """A single closed high — the shape a four-way closure makes."""
    xs, ys = np.meshgrid(g.x_coordinates(), g.y_coordinates())
    cx = (g.xmin + g.xmax) / 2
    cy = (g.ymin + g.ymax) / 2
    radius = np.hypot(xs - cx, ys - cy)
    scale = max(g.xmax - g.xmin, 1.0) / 2
    return base + (peak - base) * np.exp(-((radius / (scale * 0.5)) ** 2))


# --- level choice ------------------------------------------------------------


def test_the_specified_porosity_example() -> None:
    """`05` §7, verbatim: a range of 4.1 to 21.8 gives an interval of 1.0 —
    eighteen contours — not 1.18 for exactly fifteen."""
    levels = auto_levels(4.1, 21.8, target_count=15)

    interval = float(np.diff(levels)[0])
    assert interval == pytest.approx(1.0)
    # 5 through 21 — the spec says "18 contours", counting the range's ends;
    # the levels themselves are the seventeen round numbers strictly inside it.
    assert len(levels) == 17
    assert levels[0] == 5.0
    assert levels[-1] == 21.0


@pytest.mark.parametrize(
    ("vmin", "vmax"),
    [
        (8_237.0, 9_614.0),  # TVDSS in feet
        (0.0, 100.0),
        (0.02, 0.31),  # a fraction rather than a percentage
        (-3_500.0, -2_800.0),  # subsea, negative down
    ],
)
def test_every_interval_is_a_round_number(vmin: float, vmax: float) -> None:
    """**The Phase 4 criterion.** A geologist reads the interval off the legend
    and holds it in their head; 137.4 ft is not something anyone holds."""
    levels = auto_levels(vmin, vmax)

    interval = float(np.diff(levels)[0])
    mantissa = interval / 10 ** np.floor(np.log10(interval))
    assert round(mantissa, 6) in (1.0, 2.0, 2.5, 5.0), f"interval {interval:g} is not round"


def test_levels_land_on_multiples_of_the_interval() -> None:
    """Not on the data's minimum. Starting at 4.1 and stepping by 1 would label
    4.1, 5.1, 6.1 — round intervals between unround values, the worst of both."""
    levels = auto_levels(4.1, 21.8)

    assert all(abs(level - round(level)) < 1e-9 for level in levels), levels


def test_levels_carry_no_floating_point_dust() -> None:
    """A contour labelled 0.30000000000000004 defeats the entire purpose of
    choosing a round interval."""
    levels = auto_levels(0.0, 1.0)

    assert all(repr(level) == repr(round(level, 6)) for level in levels), levels


def test_no_contour_sits_on_the_surface_extremes() -> None:
    """A contour at the maximum is a point or a hairline around the peak, which
    draws as noise rather than as information."""
    levels = auto_levels(100.0, 200.0)

    assert levels.min() > 100.0
    assert levels.max() < 200.0


def test_a_flat_surface_says_it_has_no_contours() -> None:
    with pytest.raises(DegenerateInput, match="flat surface has no contours"):
        auto_levels(100.0, 100.0)


def test_more_contours_are_requested_than_a_round_interval_allows() -> None:
    """The round number wins over the requested count: the count is a
    preference, the interval is something a reader does arithmetic with."""
    levels = auto_levels(0.0, 10.0, target_count=7)

    interval = float(np.diff(levels)[0])
    assert interval in (1.0, 2.0, 2.5)


# --- extraction --------------------------------------------------------------


def test_a_dome_produces_closed_contours() -> None:
    """A four-way closure. If these come back open, the closure a geologist is
    looking for is not on the map."""
    g = grid()

    lines = contour_grid(dome(g), g)

    assert lines, "a dome produced no contours"
    assert any(line.closed for line in lines), "no closed contour around a dome"


def test_contour_values_match_the_surface_they_trace() -> None:
    """A contour labelled 150 must run where the surface is 150. This is the
    property everything else depends on, and the one a smoothing bug breaks."""
    g = grid()
    surface = dome(g)

    lines = contour_grid(surface, g, levels=np.array([150.0]))

    assert lines
    for line in lines:
        # Sample the surface along the contour by nearest cell.
        coords = np.asarray(line.geometry.coords)
        cols = np.clip(((coords[:, 0] - g.xmin) / g.cell_size).round().astype(int), 0, g.nx - 1)
        rows = np.clip(((g.ymax - coords[:, 1]) / g.cell_size).round().astype(int), 0, g.ny - 1)
        sampled = surface[rows, cols]
        assert np.abs(sampled - 150.0).max() < 3.0, "a contour is not on its own value"


def test_every_fifth_contour_is_an_index_contour() -> None:
    """The convention on every published structure map, and the reason a dense
    one is readable at all."""
    g = grid()
    levels = np.arange(105.0, 200.0, 5.0)

    lines = contour_grid(dome(g), g, levels=levels)

    indexed = index_levels(lines)
    assert indexed, "no index contours were marked"
    # Every fifth *level*, so the spacing between index values is five
    # intervals.
    if len(indexed) > 1:
        assert np.allclose(np.diff(indexed), 25.0)


def test_nan_cells_are_not_contoured_across() -> None:
    """**Fault-blanked and extrapolated areas.** A contour drawn across a hole
    is a line through ground where there is no data, and it looks exactly like
    a line through ground where there is."""
    g = grid()
    surface = dome(g)
    # Blank the western half, as an extrapolation mask would.
    surface[:, : g.nx // 2] = np.nan

    lines = contour_grid(surface, g)

    for line in lines:
        xs = np.asarray(line.geometry.coords)[:, 0]
        assert xs.min() >= g.xmin + (g.nx // 2 - 2) * g.cell_size, (
            "a contour was drawn into the blanked half"
        )


def test_short_fragments_are_dropped() -> None:
    """A noisy surface produces a scatter of tiny closed contours that read as
    structure and are quantisation."""
    rng = np.random.default_rng(20260909)
    g = grid()
    noisy = dome(g) + rng.normal(0.0, 3.0, size=(g.ny, g.nx))

    keep_all = contour_grid(noisy, g, min_length=0.0)
    filtered = contour_grid(noisy, g)

    assert len(filtered) < len(keep_all), "no fragments were dropped"
    assert all(line.length >= 3 * g.cell_size for line in filtered)


def test_a_transposed_surface_is_refused() -> None:
    """It would produce contours at right angles to the structure — a map that
    is entirely wrong and entirely plausible."""
    g = grid(nx=41, ny=21)

    with pytest.raises(DegenerateInput, match="right angles"):
        contour_grid(np.zeros((g.nx, g.ny)), g)


def test_an_all_nan_surface_says_why() -> None:
    g = grid()

    with pytest.raises(DegenerateInput, match="outside the search radius"):
        contour_grid(np.full((g.ny, g.nx), np.nan), g)


# --- smoothing ---------------------------------------------------------------


def test_smoothing_reduces_angularity() -> None:
    """Raw contours follow cell boundaries and look like staircases."""
    g = grid()
    surface = dome(g)

    def angularity(lines: list) -> float:
        """The sharpest corner, not the total turning.

        Chaikin adds vertices, so total turning grows even as every individual
        turn gets gentler — a metric that would report smoothing as making
        things worse. The sharpest corner is what "angular" means to a reader.
        """
        sharpest = 0.0
        for line in lines:
            coords = np.asarray(line.geometry.coords)
            if len(coords) < 3:
                continue
            steps = np.diff(coords, axis=0)
            angles = np.arctan2(steps[:, 1], steps[:, 0])
            turns = np.abs((np.diff(angles) + np.pi) % (2 * np.pi) - np.pi)
            sharpest = max(sharpest, float(turns.max()))
        return sharpest

    raw = contour_grid(surface, g, levels=np.array([150.0]))
    smooth = contour_grid(surface, g, levels=np.array([150.0]), smoothing=0.4)

    assert angularity(smooth) < angularity(raw)


def test_a_smoothed_contour_stays_on_its_value() -> None:
    """**The reason smoothing is capped.** A contour labelled 8,600 ft that
    runs 20 ft from where the grid says 8,600 is a lie on a map somebody will
    measure."""
    g = grid()
    surface = dome(g)

    lines = contour_grid(surface, g, levels=np.array([150.0]), smoothing=MAX_SMOOTHING)

    for line in lines:
        coords = np.asarray(line.geometry.coords)
        cols = np.clip(((coords[:, 0] - g.xmin) / g.cell_size).round().astype(int), 0, g.nx - 1)
        rows = np.clip(((g.ymax - coords[:, 1]) / g.cell_size).round().astype(int), 0, g.ny - 1)
        assert np.abs(surface[rows, cols] - 150.0).max() < 5.0


def test_smoothing_beyond_the_cap_is_refused() -> None:
    g = grid()

    with pytest.raises(DegenerateInput, match="drifts measurably"):
        contour_grid(dome(g), g, smoothing=0.9)
