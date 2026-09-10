"""Universal kriging. `05-geoprocessing.md` §6.2.

The load-bearing test is the reduction: **with a drift of order 0 the system
*is* the ordinary-kriging system**, so the two must agree to floating-point
tolerance on identical input. If they ever diverge, one of them has a bug in
the covariance assembly and there is no way to tell which by inspection —
both produce a smooth, plausible surface.

The second is recovery: a synthetic surface with a known planar trend and a
known covariance must come back closer under a linear drift than under a
constant mean. That is the entire reason this method exists, and it is
measurable rather than a matter of taste.
"""

from __future__ import annotations

import numpy as np
import pytest

from webmap_geo.exceptions import DegenerateInput
from webmap_geo.frame import AnalysisFrame
from webmap_geo.grid import GridDefinition
from webmap_geo.interpolate.kriging import ordinary_kriging
from webmap_geo.interpolate.universal import (
    drift_basis,
    n_drift_terms,
    universal_kriging,
)
from webmap_geo.variogram.model import FittedVariogram

TEXAS = AnalysisFrame(srid=2277, units="usft")


def grid(n: int = 24, extent: float = 10_000.0) -> GridDefinition:
    return GridDefinition(
        xmin=0.0, ymin=0.0, cell_size=extent / (n - 1), nx=n, ny=n, frame=TEXAS
    )


def variogram(range_: float = 4000.0) -> FittedVariogram:
    return FittedVariogram(
        model="spherical", nugget=0.0, sill=100.0, range_=range_, n_pairs_used=1000
    )


def scattered(
    n: int = 120, extent: float = 10_000.0, seed: int = 4
) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    coords = rng.uniform(0.0, extent, size=(n, 2))
    return coords, rng.normal(0.0, 10.0, size=n)


# --- the reduction ---------------------------------------------------------------


def test_a_drift_of_order_zero_is_exactly_ordinary_kriging() -> None:
    """**The test that keeps both implementations honest.**

    With `drift_order=0` the drift basis is a single column of ones, which is
    precisely ordinary kriging's unbiasedness constraint. The two solve the
    same system, so they must agree to floating point — not "closely", which
    would let a covariance bug hide behind a plausible-looking surface.
    """
    coords, values = scattered()
    g = grid()
    model = variogram()

    ok = ordinary_kriging(coords, values, g, model, n_neighbors=16)
    uk = universal_kriging(coords, values, g, model, drift_order=0, n_neighbors=16)

    both = np.isfinite(ok.estimate) & np.isfinite(uk.estimate)
    assert both.sum() > 100, "not enough estimated cells to compare"
    np.testing.assert_allclose(uk.estimate[both], ok.estimate[both], rtol=1e-9, atol=1e-9)
    np.testing.assert_allclose(uk.variance[both], ok.variance[both], rtol=1e-7, atol=1e-7)


def test_the_two_agree_on_which_cells_have_no_control() -> None:
    """Both leave a node NaN when nothing is within the search radius. A
    method that filled them instead would look better and be inventing."""
    coords, values = scattered(n=40)
    g = grid(n=30)
    model = variogram(range_=1500.0)

    ok = ordinary_kriging(coords, values, g, model, n_neighbors=16)
    uk = universal_kriging(coords, values, g, model, drift_order=0, n_neighbors=16)

    np.testing.assert_array_equal(np.isnan(ok.estimate), np.isnan(uk.estimate))


# --- what the drift buys ----------------------------------------------------------


def test_a_planar_trend_is_recovered_better_with_a_linear_drift() -> None:
    """**The reason the method exists**, measured rather than asserted.

    A surface with a strong planar trend violates ordinary kriging's constant-
    mean assumption. Sampled sparsely and estimated at held-out locations, a
    linear drift should be materially closer. Both use the same variogram and
    the same neighbourhood, so the drift is the only difference.
    """
    rng = np.random.default_rng(11)
    extent = 10_000.0
    coords = rng.uniform(0.0, extent, size=(80, 2))

    # 40 ft of dip across the map, against a sill of 100 — a trend that
    # dominates, which is exactly the case ordinary kriging mishandles.
    def truth(xy: np.ndarray) -> np.ndarray:
        return 0.004 * xy[:, 0] + 0.002 * xy[:, 1]

    values = truth(coords) + rng.normal(0.0, 1.0, size=len(coords))

    held_out = rng.uniform(0.0, extent, size=(200, 2))
    g = GridDefinition(xmin=0.0, ymin=0.0, cell_size=250.0, nx=41, ny=41, frame=TEXAS)
    model = FittedVariogram(
        model="spherical", nugget=1.0, sill=100.0, range_=6000.0, n_pairs_used=500
    )

    ok = ordinary_kriging(coords, values, g, model, n_neighbors=12)
    uk = universal_kriging(coords, values, g, model, drift_order=1, n_neighbors=12)

    centres = g.cell_centres()
    expected = truth(centres).reshape(g.ny, g.nx)
    usable = np.isfinite(ok.estimate) & np.isfinite(uk.estimate)

    ok_error = float(np.sqrt(np.mean((ok.estimate[usable] - expected[usable]) ** 2)))
    uk_error = float(np.sqrt(np.mean((uk.estimate[usable] - expected[usable]) ** 2)))

    assert uk_error < ok_error, (
        f"a linear drift did not help on a planar trend: UK {uk_error:.2f} vs OK {ok_error:.2f}"
    )
    assert len(held_out) == 200  # the sample the tolerance was chosen against


def test_estimating_a_trend_costs_certainty() -> None:
    """Universal kriging variance is never below the ordinary-kriging variance
    at the same node: the drift terms enter the variance with a positive sign,
    because a mean that is estimated rather than assumed is less certain.

    A UK variance *below* OK's would mean a sign error in the variance
    assembly — which is invisible on the estimate map and wrong on every
    confidence map drawn from it.
    """
    coords, values = scattered(n=90)
    g = grid(n=20)
    model = variogram()

    ok = ordinary_kriging(coords, values, g, model, n_neighbors=16)
    uk = universal_kriging(coords, values, g, model, drift_order=1, n_neighbors=16)

    both = np.isfinite(ok.variance) & np.isfinite(uk.variance)
    assert both.sum() > 50
    # A small tolerance for rounding; the property is one-sided.
    assert np.all(uk.variance[both] >= ok.variance[both] - 1e-6)


# --- the basis --------------------------------------------------------------------


@pytest.mark.parametrize(("order", "terms"), [(0, 1), (1, 3), (2, 6)])
def test_the_basis_has_the_number_of_terms_it_claims(order: int, terms: int) -> None:
    coords = np.array([[0.0, 0.0], [1.0, 2.0], [3.0, 4.0]])

    basis = drift_basis(coords, order)

    assert basis.shape == (3, terms)
    assert n_drift_terms(order) == terms
    # The constant comes first, which the drift constraint depends on.
    np.testing.assert_array_equal(basis[:, 0], np.ones(3))


def test_the_basis_reproduces_a_plane_exactly_at_order_one() -> None:
    """If it did not, the drift constraint would be enforcing the wrong
    functions and the trend would come out tilted."""
    rng = np.random.default_rng(2)
    coords = rng.uniform(-5.0, 5.0, size=(20, 2))
    plane = 3.0 + 2.0 * coords[:, 0] - 1.5 * coords[:, 1]

    basis = drift_basis(coords, 1)
    fitted, *_ = np.linalg.lstsq(basis, plane, rcond=None)

    np.testing.assert_allclose(fitted, [3.0, 2.0, -1.5], atol=1e-10)


# --- refusals ----------------------------------------------------------------------


def test_an_unsupported_drift_order_says_what_each_one_means() -> None:
    coords, values = scattered()

    with pytest.raises(DegenerateInput, match="0 is a constant"):
        universal_kriging(coords, values, grid(), variogram(), drift_order=3)


def test_too_few_points_for_the_drift_is_refused_rather_than_extrapolated() -> None:
    """With fewer points than the drift needs, the polynomial passes exactly
    through them and the result is extrapolation wearing kriging's clothes."""
    coords, values = scattered(n=8)

    with pytest.raises(DegenerateInput, match="polynomial extrapolation"):
        universal_kriging(coords, values, grid(), variogram(), drift_order=2)


def test_a_neighbourhood_smaller_than_the_drift_is_refused() -> None:
    """Six drift terms and five neighbours is an underdetermined system at
    every node — it would return the nearest value everywhere and look like a
    Voronoi diagram."""
    coords, values = scattered(n=100)

    with pytest.raises(DegenerateInput, match="Raise it to at least"):
        universal_kriging(coords, values, grid(), variogram(), drift_order=2, n_neighbors=5)


def test_mismatched_points_and_values_are_refused() -> None:
    coords, _ = scattered(n=50)

    with pytest.raises(DegenerateInput, match="exactly one value"):
        universal_kriging(coords, np.zeros(49), grid(), variogram())
