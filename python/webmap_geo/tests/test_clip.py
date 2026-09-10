"""Clipping a grid. `08` §5.2.

The clip itself is a mask, and masks are easy. What these tests are really
about is the two numbers `08` §5.2 says a clipped grid must recompute or it
lies — the extrapolation fraction and the display range — and the boundary
conventions that decide which acreage ends up on the map.
"""

from __future__ import annotations

import numpy as np
import pytest
import shapely
from shapely.geometry import Polygon

from webmap_geo.clip import (
    DISPLAY_PERCENTILES,
    clip,
    control_boundary,
    extrapolated_fraction,
)
from webmap_geo.exceptions import DegenerateInput
from webmap_geo.frame import AnalysisFrame
from webmap_geo.grid import GridDefinition

FRAME = AnalysisFrame(srid=2277, units="ft")


def a_grid(nx: int = 40, ny: int = 40, cell_size: float = 25.0) -> GridDefinition:
    return GridDefinition(xmin=0.0, ymin=0.0, cell_size=cell_size, nx=nx, ny=ny, frame=FRAME)


def a_surface(grid: GridDefinition) -> np.ndarray:
    """A plane dipping east, so the display range is easy to reason about."""
    xs = grid.x_coordinates()
    return np.tile(xs, (grid.ny, 1)).astype(float)


def a_square(grid: GridDefinition, fraction: float = 0.5) -> Polygon:
    """A centred square covering `fraction` of the grid's width and height."""
    xmin, ymin, xmax, ymax = grid.bounds
    cx, cy = (xmin + xmax) / 2.0, (ymin + ymax) / 2.0
    half_x = (xmax - xmin) * fraction / 2.0
    half_y = (ymax - ymin) * fraction / 2.0
    return Polygon(
        [
            (cx - half_x, cy - half_y),
            (cx + half_x, cy - half_y),
            (cx + half_x, cy + half_y),
            (cx - half_x, cy + half_y),
        ]
    )


# --- the mask -------------------------------------------------------------------


def test_cells_outside_the_boundary_become_nodata() -> None:
    grid = a_grid()
    result = clip(a_surface(grid), grid, a_square(grid, 0.5))

    assert np.isnan(result.surface).any()
    assert np.isfinite(result.surface).any()
    # A centred half-width square covers about a quarter of the area.
    assert result.clipped_fraction == pytest.approx(0.75, abs=0.05)


def test_invert_excludes_instead_of_including() -> None:
    """A lease to leave out, a no-permit block. The same operation with the
    mask negated, not a second code path — so the two must be complements."""
    grid = a_grid()
    square = a_square(grid, 0.5)

    kept = clip(a_surface(grid), grid, square)
    dropped = clip(a_surface(grid), grid, square, invert=True)

    assert np.isnan(kept.surface) is not np.isnan(dropped.surface)
    assert np.array_equal(np.isnan(kept.surface), np.isfinite(dropped.surface))
    assert kept.clipped_fraction + dropped.clipped_fraction == pytest.approx(1.0)


def test_a_cell_is_in_or_out_by_its_centre() -> None:
    """**By centre, not by overlap.** A cell is one value at one location and a
    partially covered cell has no partial value to give; including it because a
    corner is inside extends the grid half a cell past the lease line, which on
    a 250 ft grid is 125 ft of somebody else's acreage.
    """
    grid = a_grid(nx=4, ny=4, cell_size=100.0)
    # `GridDefinition` bounds are cell *centres*, so the centres are at 0, 100,
    # 200, 300 and each cell spans 50 either side. This boundary reaches 60,
    # which overlaps the cell centred on 100 without containing its centre.
    boundary = Polygon([(-10.0, -10.0), (60.0, -10.0), (60.0, 60.0), (-10.0, 60.0)])
    result = clip(a_surface(grid), grid, boundary)

    surviving = np.isfinite(result.surface)
    assert surviving.sum() == 1, "overlap, not centre containment, was used"


def test_a_self_intersecting_boundary_is_cleaned_rather_than_refused() -> None:
    """Digitised lease outlines self-intersect routinely, and `make_valid` on a
    polygon is well defined. Refusing would make an ordinary layer unusable for
    a defect nobody can see on the map."""
    grid = a_grid()
    bowtie = Polygon([(100.0, 100.0), (900.0, 900.0), (100.0, 900.0), (900.0, 100.0)])
    assert not bowtie.is_valid

    result = clip(a_surface(grid), grid, bowtie)
    assert np.isfinite(result.surface).any()


def test_a_boundary_that_misses_the_grid_says_what_to_check() -> None:
    """The two causes are a CRS mismatch and `invert` the wrong way round, and
    the message names both — a bare 'no cells remain' sends someone looking at
    the polygon."""
    grid = a_grid()
    elsewhere = Polygon(
        [(1e6, 1e6), (1e6 + 100, 1e6), (1e6 + 100, 1e6 + 100), (1e6, 1e6 + 100)]
    )
    with pytest.raises(DegenerateInput, match="coordinate systems"):
        clip(a_surface(grid), grid, elsewhere)


def test_an_empty_boundary_is_refused() -> None:
    grid = a_grid()
    with pytest.raises(DegenerateInput, match="empty"):
        clip(a_surface(grid), grid, Polygon())


def test_a_surface_of_the_wrong_shape_is_refused() -> None:
    """A clip against a different grid would remove cells by index rather than
    by location, which produces a plausible-looking map of the wrong acreage."""
    grid = a_grid()
    with pytest.raises(DegenerateInput, match="same grid"):
        clip(np.zeros((10, 10)), grid, a_square(grid))


# --- the two numbers that must move with it --------------------------------------


def test_the_display_range_follows_the_surviving_cells() -> None:
    """`08` §5.2: or the legend spans values no longer on the map."""
    grid = a_grid()
    surface = a_surface(grid)
    result = clip(surface, grid, a_square(grid, 0.5))

    survivors = result.surface[np.isfinite(result.surface)]
    expected = np.percentile(survivors, DISPLAY_PERCENTILES)

    assert result.display_range is not None
    assert result.display_range == pytest.approx(tuple(expected))
    # And it is genuinely narrower than the unclipped grid's, which is the
    # whole reason for recomputing it.
    assert result.display_range[1] - result.display_range[0] < surface.max() - surface.min()


def test_clipping_away_an_invented_corner_lowers_the_extrapolation_fraction() -> None:
    """**The claim that makes clipping the answer to extrapolation.** All the
    control sits in one corner; the rest of the grid is invention. Clipping to
    the control removes it, and the number has to say so."""
    grid = a_grid(nx=60, ny=60, cell_size=25.0)
    rng = np.random.default_rng(20260910)
    control = rng.uniform([0.0, 0.0], [300.0, 300.0], size=(80, 2))
    surface = a_surface(grid)

    before = extrapolated_fraction(surface, grid, control)
    boundary = control_boundary(control, method="convex_hull")
    after = clip(surface, grid, boundary, control_points=control).extrapolated_fraction

    assert before > 0.7, f"the fixture is not mostly extrapolated ({before:.0%})"
    assert after is not None
    assert after < before / 2.0, f"{after:.0%} after clipping vs {before:.0%} before"


def test_the_denominator_is_the_clipped_map_not_the_original_grid() -> None:
    """**The measurement that fixed the definition.** Counting blanked cells in
    the numerator while keeping the full grid in the denominator made clipping
    away a wholly invented corner *raise* the extrapolation fraction — 95%
    before and 97% after, on this fixture. That is the exact opposite of what
    `08` §5.2 says clipping is for.
    """
    grid = a_grid(nx=60, ny=60, cell_size=25.0)
    rng = np.random.default_rng(20260910)
    control = rng.uniform([0.0, 0.0], [300.0, 300.0], size=(80, 2))
    surface = a_surface(grid)
    clipped = clip(surface, grid, control_boundary(control), control_points=control)

    whole_grid = extrapolated_fraction(clipped.surface, grid, control)
    assert clipped.extrapolated_fraction is not None
    assert whole_grid > 0.9, "the blanked cells should dominate the whole-grid view"
    assert clipped.extrapolated_fraction < whole_grid / 3.0


def test_the_extrapolation_fraction_is_not_recomputed_without_control() -> None:
    """`None`, not the pre-clip figure. Carrying the old number forward would
    be wrong in the direction that flatters the grid."""
    grid = a_grid()
    result = clip(a_surface(grid), grid, a_square(grid))
    assert result.extrapolated_fraction is None


def test_a_nodata_cell_counts_as_extrapolated() -> None:
    """The same convention the gridding diagnostics use: a cell nothing could
    estimate and a cell nothing supports are both cells not to read."""
    grid = a_grid(nx=10, ny=10, cell_size=25.0)
    dense = grid.cell_centres()
    surface = a_surface(grid)
    assert extrapolated_fraction(surface, grid, dense) == pytest.approx(0.0)

    blanked = surface.copy()
    blanked[0, :] = np.nan
    assert extrapolated_fraction(blanked, grid, dense) == pytest.approx(0.1)


def test_the_default_radius_matches_the_gridding_dispatcher() -> None:
    """The constant is the one thing the duplicated helper must agree on: a
    clip that used a different definition of 'supported' would report a
    different extrapolation fraction for an unchanged grid."""
    from webmap_geo.clip import _default_radius
    from webmap_geo.interpolate.dispatch import _search_radius

    rng = np.random.default_rng(7)
    coords = rng.uniform(0.0, 1000.0, size=(200, 2))
    assert _default_radius(coords) == pytest.approx(_search_radius(coords, None))


# --- the control boundary preset --------------------------------------------------


def test_the_convex_hull_contains_every_control_point() -> None:
    rng = np.random.default_rng(11)
    coords = rng.uniform(0.0, 1000.0, size=(60, 2))
    hull = control_boundary(coords, method="convex_hull")

    # `covers`, not `contains`: the points that *made* the hull lie on its
    # boundary, and `contains` excludes a boundary point. The clip uses the
    # same convention — a cell centre on the line is inside.
    assert shapely.intersects_xy(hull, coords[:, 0], coords[:, 1]).all()


def test_the_concave_hull_is_tighter_than_the_convex_one() -> None:
    """On an L-shaped acquisition the convex hull keeps the bay no well has
    touched. That is the case the concave option exists for."""
    rng = np.random.default_rng(13)
    arm_a = rng.uniform([0.0, 0.0], [1000.0, 150.0], size=(120, 2))
    arm_b = rng.uniform([0.0, 0.0], [150.0, 1000.0], size=(120, 2))
    coords = np.vstack([arm_a, arm_b])

    convex = control_boundary(coords, method="convex_hull")
    concave = control_boundary(coords, method="concave_hull")

    assert concave.area < convex.area * 0.8


def test_the_radius_boundary_leaves_holes_where_control_is_sparse() -> None:
    """And the holes are honest: they are exactly the area `05` §6.5 does not
    call supported."""
    coords = np.array(
        [[0.0, 0.0], [1000.0, 0.0], [1000.0, 1000.0], [0.0, 1000.0], [500.0, 500.0]]
    )
    boundary = control_boundary(coords, method="radius", search_radius=200.0)

    assert not shapely.contains_xy(boundary, 250.0, 250.0)
    assert shapely.contains_xy(boundary, 500.0, 500.0)


def test_too_few_points_for_a_boundary_says_how_many() -> None:
    with pytest.raises(DegenerateInput, match="at least 3"):
        control_boundary(np.array([[0.0, 0.0], [1.0, 1.0]]))


def test_an_unknown_boundary_method_lists_the_real_ones() -> None:
    rng = np.random.default_rng(3)
    coords = rng.uniform(0.0, 100.0, size=(10, 2))
    with pytest.raises(DegenerateInput, match="convex_hull"):
        control_boundary(coords, method="alpha_shape")  # type: ignore[arg-type]
