"""Filled contour bands. `05-geoprocessing.md` §7.

A band is a *measurement*, not a picture: its area answers "how much of this
lease is above the spill point". So the bar here is that the areas are right
against a surface whose true areas are known analytically, not that the output
looks plausible.

The other half is agreement with the lines. A band edge and the contour drawn
over it are the same level, and if they disagree by a fraction of a cell the
map has a coloured halo along every contour that nobody can account for.
"""

from __future__ import annotations

import math
from itertools import pairwise

import numpy as np
import pytest
import shapely
from shapely.geometry import MultiPolygon, Point

from webmap_geo.contour import ContourBand, auto_levels, contour_bands, contour_grid
from webmap_geo.exceptions import DegenerateInput
from webmap_geo.frame import AnalysisFrame
from webmap_geo.grid import GridDefinition

FRAME = AnalysisFrame(srid=2277, units="usft")


def cone_grid(half_width: float = 1000.0, n: int = 201) -> tuple[np.ndarray, GridDefinition]:
    """z = distance from the centre. Every level is a circle of known radius,
    so the area below a level is exactly pi*r^2 — a closed form to test against
    rather than a golden file to regenerate."""
    grid = GridDefinition(
        xmin=-half_width,
        ymin=-half_width,
        cell_size=2 * half_width / (n - 1),
        nx=n,
        ny=n,
        frame=FRAME,
    )
    x = grid.x_coordinates()
    y = grid.y_coordinates()
    xx, yy = np.meshgrid(x, y)
    return np.sqrt(xx**2 + yy**2), grid


def max_departure(line: object, bands: list[ContourBand]) -> float:
    """How far the drawn contour ever strays from the edge of the fill.

    **Not `hausdorff_distance`.** That is symmetric, and the band boundary
    includes every *other* level plus the edge of the domain — so it reports
    the distance to the far side of the map and says nothing about whether
    these two curves coincide. What matters here is one-directional: no point
    on the line departs from the fill edge.
    """
    boundary = MultiPolygon([part for band in bands for part in band.geometry.geoms]).boundary
    vertices = shapely.points(np.asarray(line.coords))  # type: ignore[attr-defined]
    return float(shapely.distance(vertices, boundary).max())


def band_at(bands: list[ContourBand], lower: float) -> ContourBand:
    for band in bands:
        if math.isclose(band.lower, lower, rel_tol=1e-9):
            return band
    raise AssertionError(f"No band starting at {lower}; got {[b.lower for b in bands]}")


# --- the areas are right -------------------------------------------------------


def test_a_band_measures_the_area_between_its_levels() -> None:
    """The reason bands exist at all. On a cone the annulus between r=200 and
    r=400 is pi*(400^2 - 200^2) exactly.

    Tolerance is 0.5% of the true area. Marching squares linearises the surface
    inside each cell, so a circle of radius 200 drawn on a 10 ft grid is a
    polygon with a bounded chord error — this is a discretisation limit, not
    slack for a bug to hide in.
    """
    surface, grid = cone_grid()

    bands = contour_bands(surface, grid, levels=np.array([200.0, 400.0]))
    annulus = band_at(bands, 200.0)

    expected = math.pi * (400.0**2 - 200.0**2)
    assert annulus.area == pytest.approx(expected, rel=0.005)


def test_the_lowest_band_is_the_disc_inside_the_first_level() -> None:
    surface, grid = cone_grid()

    bands = contour_bands(surface, grid, levels=np.array([200.0, 400.0]))
    innermost = bands[0]

    assert innermost.area == pytest.approx(math.pi * 200.0**2, rel=0.005)
    assert innermost.is_open_ended, "the band below the lowest contour is open-ended"


def test_a_band_around_a_summit_is_an_annulus_with_a_hole() -> None:
    """Not a disc. A band drawn as a solid disc covers the levels above it and
    hides the summit — the single most obvious way filled contouring goes
    wrong, and it does not error."""
    surface, grid = cone_grid()

    annulus = band_at(contour_bands(surface, grid, levels=np.array([200.0, 400.0])), 200.0)

    assert not annulus.geometry.contains(Point(0.0, 0.0)), "the band swallowed the summit"
    assert sum(len(part.interiors) for part in annulus.geometry.geoms) == 1


# --- bands and lines are the same curve ----------------------------------------


@pytest.mark.parametrize("smoothing", [0.0, 0.25, 0.5])
def test_a_band_edge_lands_on_the_contour_drawn_over_it(smoothing: float) -> None:
    """`08` §5.2: one level list drives the lines and the fill. If the two
    disagree the map has a coloured fringe along every contour that nobody can
    account for.

    **Zero, not a tolerance.** Both come from the same generator at the same
    level and are smoothed by the same function, so they are the same curve
    rather than two curves that agree closely — and asserting a tolerance here
    would let them drift apart by a fraction of a cell without failing. Run at
    every smoothing level, because smoothing is where they could diverge:
    that is what `contour.smooth` being shared prevents.
    """
    surface, grid = cone_grid()
    levels = np.array([200.0, 400.0])

    lines = contour_grid(surface, grid, levels=levels, smoothing=smoothing)
    bands = contour_bands(surface, grid, levels=levels, smoothing=smoothing)

    drawn = [line for line in lines if math.isclose(line.value, 400.0)]
    assert drawn, "no contour at 400 to compare against"
    assert max_departure(drawn[0].geometry, bands) == 0.0


# --- holes, refusals and edges --------------------------------------------------


def test_a_blanked_area_is_a_hole_rather_than_filled_ground() -> None:
    """NaN is not a value. Filling across a fault-blanked compartment colours
    ground as though it had been interpolated, which is the failure the whole
    extrapolation-reporting effort exists to prevent."""
    surface, grid = cone_grid()
    blanked = surface.copy()
    # A square hole well inside the innermost band, so any fill over it is the
    # bug rather than a neighbouring band.
    centre = grid.nx // 2
    blanked[centre - 12 : centre + 12, centre - 12 : centre + 12] = np.nan

    bands = contour_bands(blanked, grid, levels=np.array([400.0]))
    covering = [b for b in bands if b.geometry.contains(Point(0.0, 0.0))]

    assert not covering, "a blanked area was filled"


def test_bands_tile_the_surface_without_overlapping() -> None:
    """Adjacent bands share an edge and nothing more. Overlap would double-count
    every area total drawn from them."""
    surface, grid = cone_grid()

    bands = contour_bands(surface, grid, levels=np.array([200.0, 400.0, 600.0]))

    for lower, upper in pairwise(bands):
        shared = lower.geometry.intersection(upper.geometry)
        assert shared.area < grid.cell_size**2, (
            f"bands {lower.lower}-{lower.upper} and {upper.lower}-{upper.upper} overlap"
        )


def test_levels_outside_the_surface_do_not_produce_empty_features() -> None:
    """A feature with no geometry breaks every writer downstream, and a level
    above everything in the grid is an ordinary thing to ask for — it is what a
    shared level list from a *different* grid looks like."""
    surface, grid = cone_grid()

    bands = contour_bands(surface, grid, levels=np.array([200.0, 99_000.0]))

    assert all(not band.geometry.is_empty for band in bands)
    assert all(band.area > 0 for band in bands)


def test_an_entirely_blank_surface_says_why_there_is_nothing_to_fill() -> None:
    _, grid = cone_grid(n=21)
    blank = np.full((grid.ny, grid.nx), np.nan)

    with pytest.raises(DegenerateInput, match="search radius or blanked"):
        contour_bands(blank, grid)


def test_a_transposed_surface_is_refused_rather_than_filled_sideways() -> None:
    """It would produce a plausible-looking map rotated 90 degrees, which is
    the class of error `CLAUDE.md` §7.3 says to fail loudly on."""
    grid = GridDefinition(xmin=0, ymin=0, cell_size=10, nx=30, ny=20, frame=FRAME)
    surface = np.zeros((grid.nx, grid.ny))

    with pytest.raises(DegenerateInput, match="right angles to the"):
        contour_bands(surface, grid)


def test_smoothing_above_the_cap_is_refused() -> None:
    surface, grid = cone_grid(n=21)

    with pytest.raises(DegenerateInput, match="no longer sits under the contour"):
        contour_bands(surface, grid, smoothing=0.9)


def test_auto_levels_are_used_when_none_are_given() -> None:
    """The same round intervals the lines get. Bands on unround boundaries
    under contours on round ones is the misalignment in a different form.

    Compared against `auto_levels` itself rather than against a hard-coded
    interval: what has to hold is that the two agree, and pinning a number here
    would only re-test `auto_levels`' choice — which has its own tests, and
    which for this surface is 250, not the 200 a first guess suggests.
    """
    surface, grid = cone_grid()

    bands = contour_bands(surface, grid, target_count=6)

    expected = auto_levels(float(surface.min()), float(surface.max()), 6)
    assert [band.lower for band in bands[1:]] == pytest.approx(list(expected))
