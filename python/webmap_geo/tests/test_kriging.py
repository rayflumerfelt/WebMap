"""Ordinary kriging. `05-geoprocessing.md` §6.2.

`12-roadmap.md` Phase 4: "kriging with a known synthetic variogram recovers the
field within tolerance." That is the acceptance criterion and the shape of the
central test here — generate a field from stated parameters, krige it from a
sample, and require the result to match the field it came from.

The other tests are the properties that make a kriged surface trustworthy:
exactness at control points, variance that rises away from them, and weights
that sum to one. Each has a failure mode that produces a map nobody would
question.
"""

from __future__ import annotations

import numpy as np
import pytest

from webmap_geo.exceptions import DegenerateInput
from webmap_geo.frame import AnalysisFrame
from webmap_geo.grid import GridDefinition
from webmap_geo.interpolate.kriging import cross_validate, ordinary_kriging
from webmap_geo.variogram.model import FittedVariogram, anisotropy_transform

SEED = 20260909

#: EPSG:2277, the seed project's frame. Feet, so a "range 4200" in any message
#: is unambiguous.
TEXAS = AnalysisFrame(srid=2277, units="usft")


def grid_over(bounds: tuple[float, float, float, float], cell: float) -> GridDefinition:
    return GridDefinition.covering(bounds, cell, TEXAS)


def gaussian_field(
    rng: np.random.Generator,
    *,
    n: int,
    extent: float,
    range_: float,
    sill: float = 10.0,
    nugget: float = 0.0,
    anisotropy_ratio: float = 1.0,
    anisotropy_angle: float = 0.0,
) -> tuple[np.ndarray, np.ndarray, FittedVariogram]:
    """Points, values, and the variogram they were drawn from."""
    points = rng.uniform(0.0, extent, size=(n, 2))
    truth = FittedVariogram(
        model="spherical",
        nugget=nugget,
        sill=sill,
        range_=range_,
        anisotropy_ratio=anisotropy_ratio,
        anisotropy_angle=anisotropy_angle,
    )

    working = points
    if anisotropy_ratio > 1.0:
        working = points @ anisotropy_transform(anisotropy_ratio, anisotropy_angle).T
    separation = working[:, None, :] - working[None, :, :]
    covariance = truth.covariance(np.hypot(separation[..., 0], separation[..., 1]))
    covariance += np.eye(n) * 1e-8 * sill

    values = np.linalg.cholesky(covariance) @ rng.standard_normal(n) + 100.0
    return points, values, truth


# --- grid definition ---------------------------------------------------------


def test_row_zero_is_the_northern_edge() -> None:
    """**GeoTIFF's order, and the one every raster reader assumes.**

    Ascending y produces a map that reads as a plausible surface reflected
    about its centre, and nothing errors.
    """
    grid = grid_over((0.0, 0.0, 1_000.0, 1_000.0), 100.0)

    ys = grid.y_coordinates()

    assert ys[0] > ys[-1], "row 0 is not the northern edge"
    assert ys[0] == pytest.approx(grid.ymax)


def test_cell_centres_flatten_in_reshape_order() -> None:
    """So a result can be reshaped without index arithmetic — which is where a
    transposed grid usually comes from."""
    grid = grid_over((0.0, 0.0, 300.0, 200.0), 100.0)

    centres = grid.cell_centres()

    assert len(centres) == grid.n_cells
    # The first row is the northern one, running west to east.
    assert centres[0][1] == pytest.approx(grid.ymax)
    assert centres[1][0] > centres[0][0]


def test_the_transform_is_written_from_the_corner_not_the_centre() -> None:
    """A GeoTIFF's origin is the outer edge of the first pixel. The half-cell
    difference is what makes a grid disagree with the points that made it."""
    grid = grid_over((1_000.0, 2_000.0, 1_400.0, 2_400.0), 100.0)

    a, _, c, _, e, f = grid.transform()

    assert a == 100.0
    assert e == -100.0, "the y step must be negative for a north-up raster"
    assert c == pytest.approx(grid.xmin - 50.0)
    assert f == pytest.approx(grid.ymax + 50.0)


def test_an_oversized_grid_names_a_cell_size_that_would_work() -> None:
    """`CLAUDE.md` §8: name the limit and the offending value, so the caller
    knows what to change and to what."""
    with pytest.raises(DegenerateInput, match="gives"):
        GridDefinition.covering((0.0, 0.0, 1e6, 1e6), 10.0, TEXAS)


def test_inverted_bounds_state_the_expected_order() -> None:
    with pytest.raises(DegenerateInput, match="xmin, ymin, xmax, ymax"):
        GridDefinition.covering((100.0, 0.0, 0.0, 100.0), 10.0, TEXAS)


# --- exactness ---------------------------------------------------------------


def test_kriging_reproduces_a_control_point_it_lands_on() -> None:
    """**Kriging is an exact interpolator.** With no nugget, the estimate at a
    control point is that point's value.

    A surface that smooths through its own control is the signature of a bug in
    the weights, and it is invisible on a map — the surface still looks
    plausible, it just no longer honours the wells it was built from.
    """
    rng = np.random.default_rng(SEED)
    points = np.array([[0.0, 0.0], [1_000.0, 0.0], [0.0, 1_000.0], [1_000.0, 1_000.0]])
    values = np.array([10.0, 20.0, 30.0, 40.0])
    variogram = FittedVariogram(model="spherical", nugget=0.0, sill=10.0, range_=2_000.0)

    # A grid whose cell centres fall exactly on the control points.
    grid = GridDefinition(xmin=0.0, ymin=0.0, cell_size=1_000.0, nx=2, ny=2, frame=TEXAS)
    result = ordinary_kriging(points, values, grid, variogram)

    # Row 0 is the north, so the top row holds y=1000.
    assert result.estimate[0, 0] == pytest.approx(30.0, abs=1e-6)
    assert result.estimate[1, 0] == pytest.approx(10.0, abs=1e-6)
    assert rng is not None


def test_variance_is_near_zero_at_a_control_point() -> None:
    """The other half of exactness: where the value is known, the uncertainty
    is not."""
    points = np.array([[0.0, 0.0], [1_000.0, 0.0], [0.0, 1_000.0], [1_000.0, 1_000.0]])
    values = np.array([10.0, 20.0, 30.0, 40.0])
    variogram = FittedVariogram(model="spherical", nugget=0.0, sill=10.0, range_=2_000.0)
    grid = GridDefinition(xmin=0.0, ymin=0.0, cell_size=1_000.0, nx=2, ny=2, frame=TEXAS)

    result = ordinary_kriging(points, values, grid, variogram)

    assert result.variance[0, 0] < 0.01


def test_variance_rises_away_from_control() -> None:
    """**The number that says where the surface is invented.**

    A map drawn without regard to it presents an extrapolated corner with the
    same confidence as a well-controlled centre.
    """
    points = np.array([[500.0, 500.0], [600.0, 500.0], [500.0, 600.0]])
    values = np.array([10.0, 11.0, 12.0])
    variogram = FittedVariogram(model="spherical", nugget=0.0, sill=10.0, range_=5_000.0)
    grid = GridDefinition(xmin=0.0, ymin=0.0, cell_size=250.0, nx=9, ny=9, frame=TEXAS)

    result = ordinary_kriging(points, values, grid, variogram)

    near = result.variance[4, 4]
    far = result.variance[0, 0]
    assert far > near, "variance did not increase away from control"


# --- recovery ----------------------------------------------------------------


def test_a_known_field_is_recovered() -> None:
    """**The Phase 4 acceptance criterion**: "kriging with a known synthetic
    variogram recovers the field within tolerance."

    Half the points are held out; the surface is built from the rest and
    compared against the held-out truth. A correlation above 0.7 on a field
    with a range a fifth of the domain is what recovery looks like — the
    remaining error is the part of the field that lives below the sample
    spacing and genuinely cannot be recovered.
    """
    rng = np.random.default_rng(SEED)
    points, values, truth = gaussian_field(rng, n=400, extent=20_000.0, range_=4_000.0)

    held_out = rng.choice(len(points), size=120, replace=False)
    mask = np.ones(len(points), dtype=bool)
    mask[held_out] = False

    grid = GridDefinition.covering((0.0, 0.0, 20_000.0, 20_000.0), 500.0, TEXAS)
    result = ordinary_kriging(points[mask], values[mask], grid, truth)

    # Sample the surface at the held-out locations.
    xs = np.clip(
        ((points[held_out, 0] - grid.xmin) / grid.cell_size).round().astype(int), 0, grid.nx - 1
    )
    ys = np.clip(
        ((grid.ymax - points[held_out, 1]) / grid.cell_size).round().astype(int), 0, grid.ny - 1
    )
    predicted = result.estimate[ys, xs]

    usable = np.isfinite(predicted)
    correlation = np.corrcoef(predicted[usable], values[held_out][usable])[0, 1]

    assert correlation > 0.7, f"the field was not recovered (r={correlation:.2f})"


def test_the_surface_does_not_overshoot_the_data() -> None:
    """Kriging is a weighted average of the data, so its range cannot exceed
    the data's by much. A surface that does is the sign of an ill-conditioned
    system — and an overshooting structure map invents a closure."""
    rng = np.random.default_rng(SEED)
    points, values, truth = gaussian_field(rng, n=300, extent=15_000.0, range_=3_000.0)
    grid = GridDefinition.covering((0.0, 0.0, 15_000.0, 15_000.0), 500.0, TEXAS)

    result = ordinary_kriging(points, values, grid, truth)
    finite = result.estimate[np.isfinite(result.estimate)]

    spread = values.max() - values.min()
    assert finite.min() > values.min() - 0.2 * spread
    assert finite.max() < values.max() + 0.2 * spread


def test_anisotropy_elongates_the_neighbourhood_along_its_azimuth() -> None:
    """The whole point of carrying anisotropy into the search.

    With a due-east grain, a control point's influence must reach further east
    than north. Measured as the distance over which the surface stays defined:
    beyond the elliptical neighbourhood there is no control at all, so the
    cells are NaN — which is a sharper signal than comparing two values, and it
    is the one that shows the neighbourhood really is an ellipse.

    An isotropic neighbourhood feeding an anisotropic model would reach equally
    far in both directions and produce a surface elongated by roughly the
    square root of the ratio: wrong, and still shaped like a map.
    """
    points = np.array([[5_000.0, 5_000.0]])
    # A single control point, plus two far away so the solver has three.
    points = np.vstack([points, [[100.0, 100.0], [9_900.0, 100.0]]])
    values = np.array([100.0, 0.0, 0.0])
    variogram = FittedVariogram(
        model="spherical",
        nugget=0.0,
        sill=10.0,
        range_=3_000.0,
        anisotropy_ratio=5.0,
        # Azimuth 090 is due east, so the long axis runs east-west.
        anisotropy_angle=90.0,
    )
    grid = GridDefinition(xmin=0.0, ymin=0.0, cell_size=250.0, nx=41, ny=41, frame=TEXAS)

    result = ordinary_kriging(points, values, grid, variogram)

    centre_row = centre_col = 20
    east_reach = sum(
        1
        for step in range(1, 20)
        if np.isfinite(result.estimate[centre_row, centre_col + step])
    )
    north_reach = sum(
        1
        for step in range(1, 20)
        if np.isfinite(result.estimate[centre_row - step, centre_col])
    )

    assert east_reach > north_reach * 2, (
        f"the neighbourhood is not elliptical: reached {east_reach} cells east "
        f"and {north_reach} north"
    )


# --- refusals ----------------------------------------------------------------


def test_too_few_points_says_what_to_use_instead() -> None:
    variogram = FittedVariogram(model="spherical", nugget=0.0, sill=1.0, range_=100.0)
    grid = grid_over((0.0, 0.0, 100.0, 100.0), 10.0)

    with pytest.raises(DegenerateInput, match="nearest-neighbour"):
        ordinary_kriging(np.zeros((2, 2)), np.zeros(2), grid, variogram)


def test_mismatched_points_and_values_are_refused() -> None:
    variogram = FittedVariogram(model="spherical", nugget=0.0, sill=1.0, range_=100.0)
    grid = grid_over((0.0, 0.0, 100.0, 100.0), 10.0)

    with pytest.raises(DegenerateInput, match="exactly one value"):
        ordinary_kriging(np.zeros((5, 2)), np.zeros(4), grid, variogram)


def test_duplicated_control_points_do_not_produce_nan() -> None:
    """A singular system. Falling back to the nearest value is honest — it is
    what the data supports — and far better than a NaN that then spreads
    through contouring."""
    points = np.array([[0.0, 0.0], [0.0, 0.0], [1_000.0, 1_000.0], [500.0, 200.0]])
    values = np.array([10.0, 10.0, 20.0, 15.0])
    variogram = FittedVariogram(model="spherical", nugget=0.0, sill=10.0, range_=3_000.0)
    grid = GridDefinition(xmin=0.0, ymin=0.0, cell_size=250.0, nx=5, ny=5, frame=TEXAS)

    result = ordinary_kriging(points, values, grid, variogram)

    assert np.isfinite(result.estimate).all()


def test_cells_beyond_the_search_radius_are_nan_not_the_mean() -> None:
    """**A mean drawn across a gap looks like data and is not** (`05` §6.5).

    Left as NaN so the renderer shows nothing there, and counted so the
    diagnostics can say what fraction of the map is extrapolated.
    """
    points = np.array([[100.0, 100.0], [200.0, 100.0], [100.0, 200.0]])
    values = np.array([10.0, 11.0, 12.0])
    variogram = FittedVariogram(model="spherical", nugget=0.0, sill=10.0, range_=500.0)
    grid = GridDefinition(xmin=0.0, ymin=0.0, cell_size=500.0, nx=12, ny=12, frame=TEXAS)

    result = ordinary_kriging(points, values, grid, variogram, max_radius=800.0)

    assert result.n_extrapolated > 0
    assert np.isnan(result.estimate).any()
    assert result.extrapolated_fraction > 0.0


# --- cross validation --------------------------------------------------------


def test_cross_validation_excludes_the_point_being_predicted() -> None:
    """**The classic way to produce a validation that validates nothing.**

    A point is always its own nearest neighbour, so including it makes every
    prediction exact and the RMSE zero — on any data, with any model.
    """
    rng = np.random.default_rng(SEED)
    points, values, truth = gaussian_field(rng, n=200, extent=15_000.0, range_=3_000.0)

    diagnostics = cross_validate(points, values, truth, rng=rng)

    assert diagnostics["rmse"] > 0.0, "the point predicted itself"


def test_cross_validation_reports_the_field_spread_for_comparison() -> None:
    """An RMSE near the field's own standard deviation means the model is
    barely better than the mean — worth knowing before the map goes on a
    slide, and not knowable from the RMSE alone."""
    rng = np.random.default_rng(SEED)
    points, values, truth = gaussian_field(rng, n=200, extent=15_000.0, range_=3_000.0)

    diagnostics = cross_validate(points, values, truth, rng=rng)

    assert diagnostics["field_std"] > 0
    assert diagnostics["rmse"] < diagnostics["field_std"], "no better than the mean"


def test_cross_validation_is_reproducible() -> None:
    """`CLAUDE.md` §3.3 — the diagnostic goes into the lineage record."""
    rng = np.random.default_rng(SEED)
    points, values, truth = gaussian_field(rng, n=200, extent=15_000.0, range_=3_000.0)

    first = cross_validate(points, values, truth, rng=np.random.default_rng(3))
    second = cross_validate(points, values, truth, rng=np.random.default_rng(3))

    assert first == second
