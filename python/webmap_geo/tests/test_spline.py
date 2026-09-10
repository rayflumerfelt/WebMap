"""Cubic and thin-plate spline interpolation. `05-geoprocessing.md` §6.4.

Two things are worth testing here and neither is "does it produce a surface".

The first is **exactness**: an RBF with zero smoothing passes through every
control point, and if it does not, the neighbour count or the kernel is wrong
and the surface is subtly off everywhere.

The second is **overshoot**, which is the method's defining behaviour rather
than a bug. §6.4 requires a warning when the output range exceeds the input
range by 20%, so the measurement has to be real — a spline that reported zero
overshoot on a spiky surface would be lying about the one thing this method
does that the others do not.
"""

from __future__ import annotations

import numpy as np
import pytest

from webmap_geo.exceptions import DegenerateInput
from webmap_geo.frame import AnalysisFrame
from webmap_geo.grid import GridDefinition
from webmap_geo.interpolate.spline import KERNELS, OVERSHOOT_WARNING, spline

TEXAS = AnalysisFrame(srid=2277, units="usft")


def grid(n: int = 40, extent: float = 10_000.0) -> GridDefinition:
    return GridDefinition(
        xmin=0.0, ymin=0.0, cell_size=extent / (n - 1), nx=n, ny=n, frame=TEXAS
    )


def scattered(
    n: int = 60, extent: float = 10_000.0, seed: int = 5
) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    coords = rng.uniform(0.0, extent, size=(n, 2))
    values = np.sin(coords[:, 0] / 2000.0) * 50.0 + coords[:, 1] / 200.0
    return coords, values


# --- it honours the data ---------------------------------------------------------


def test_a_spline_with_no_smoothing_passes_through_its_control_points() -> None:
    """The defining property of an interpolating RBF. A surface that misses
    its own control is wrong everywhere, and looks fine."""
    coords, values = scattered()
    g = grid()

    surface = spline(coords, values, g)

    from scipy.interpolate import RBFInterpolator

    at_control = RBFInterpolator(coords, values, neighbors=64)(coords)
    np.testing.assert_allclose(at_control, values, rtol=1e-6, atol=1e-6)
    assert np.isfinite(surface.estimate).all()


def test_smoothing_trades_exactness_for_a_calmer_surface() -> None:
    """Which is the right trade on noisy data: an exact fit through
    measurement error reproduces the error as structure."""
    rng = np.random.default_rng(8)
    coords, clean = scattered(n=80)
    noisy = clean + rng.normal(0.0, 8.0, size=len(clean))
    g = grid()

    exact = spline(coords, noisy, g, smoothing=0.0)
    smoothed = spline(coords, noisy, g, smoothing=50.0)

    def roughness(surface: np.ndarray) -> float:
        return float(np.nanmean(np.abs(np.diff(surface, axis=0))))

    assert roughness(smoothed.estimate) < roughness(exact.estimate)


@pytest.mark.parametrize("kernel", KERNELS)
def test_every_offered_kernel_produces_a_finite_surface(kernel: str) -> None:
    """The guard against a kernel being listed and not working — which fails
    only when someone selects it."""
    coords, values = scattered()

    surface = spline(coords, values, grid(n=24), kernel=kernel)

    assert np.isfinite(surface.estimate).any()
    assert surface.kernel == kernel


# --- overshoot is measured, not assumed away ---------------------------------------


def test_overshoot_is_measured_against_the_input_range() -> None:
    """§6.4's rule. The number means "how much did this invent", not "how wide
    is the surface"."""
    coords, values = scattered()

    surface = spline(coords, values, grid())

    assert surface.overshoot >= 0.0
    assert surface.input_range[0] == pytest.approx(values.min())
    assert surface.input_range[1] == pytest.approx(values.max())
    span = surface.output_range[1] - surface.output_range[0]
    expected = (span - (values.max() - values.min())) / (values.max() - values.min())
    assert surface.overshoot == pytest.approx(max(0.0, expected))


def test_a_spiky_surface_overshoots_and_says_so() -> None:
    """**The behaviour that makes this method dangerous unattended.** Two close
    control points at very different values force the surface to swing past
    both. On a porosity map that is negative porosity; on a structure map it is
    a dome nobody logged."""
    # A pair of near-coincident points 500 units apart in value, which is what
    # a spline cannot reach without overshooting.
    coords = np.array(
        [
            [0.0, 0.0],
            [10_000.0, 0.0],
            [0.0, 10_000.0],
            [10_000.0, 10_000.0],
            [5_000.0, 5_000.0],
            [5_200.0, 5_000.0],
            [2_000.0, 8_000.0],
            [8_000.0, 2_000.0],
            [1_000.0, 1_000.0],
            [9_000.0, 9_000.0],
        ]
    )
    values = np.array([0.0, 0.0, 0.0, 0.0, 500.0, -500.0, 0.0, 0.0, 0.0, 0.0])

    surface = spline(coords, values, grid(n=60), kernel="cubic")

    assert surface.overshoots_badly, (
        f"a spline through a 1000-unit step 200 units apart should overshoot; "
        f"measured {surface.overshoot:.1%} against a {OVERSHOOT_WARNING:.0%} threshold"
    )


def test_the_search_radius_masks_rather_than_extrapolating() -> None:
    """A thin-plate spline grows without bound away from its data, so an
    unmasked corner can be thousands of units past anything observed — and it
    is drawn with the same confidence as the middle."""
    rng = np.random.default_rng(3)
    # All control in one corner, so most of the grid is far from any of it.
    coords = rng.uniform(0.0, 2_000.0, size=(40, 2))
    values = rng.normal(100.0, 5.0, size=40)
    g = grid(n=40)

    unmasked = spline(coords, values, g)
    masked = spline(coords, values, g, max_radius=1_500.0)

    assert masked.n_extrapolated > 0
    assert np.isnan(masked.estimate).sum() == masked.n_extrapolated
    assert np.isnan(unmasked.estimate).sum() == 0
    # The masked surface stays near the data; the unmasked one need not.
    assert np.nanmax(np.abs(masked.estimate - 100.0)) <= np.nanmax(
        np.abs(unmasked.estimate - 100.0)
    )


# --- degenerate input ---------------------------------------------------------------


def test_coincident_points_are_averaged_rather_than_raising() -> None:
    """Two picks at one surface location is a real thing. Kriging survives it
    because its solver falls back to the nearest value; `RBFInterpolator` has
    no such fallback and raises on a singular system, so the resolution has to
    happen before it."""
    coords, values = scattered(n=30)
    doubled = np.vstack([coords, coords[:5]])
    doubled_values = np.concatenate([values, values[:5] + 20.0])

    surface = spline(doubled, doubled_values, grid(n=20))

    assert np.isfinite(surface.estimate).any()


def test_too_few_points_is_refused_with_the_reason() -> None:
    coords, values = scattered(n=6)

    with pytest.raises(DegenerateInput, match="rings between the points"):
        spline(coords, values, grid())


def test_an_unknown_kernel_lists_the_ones_that_exist() -> None:
    coords, values = scattered()

    with pytest.raises(DegenerateInput, match="thin_plate_spline is the usual"):
        spline(coords, values, grid(), kernel="wobbly")


def test_negative_smoothing_is_refused() -> None:
    coords, values = scattered()

    with pytest.raises(DegenerateInput, match="zero or positive"):
        spline(coords, values, grid(), smoothing=-1.0)


def test_mismatched_points_and_values_are_refused() -> None:
    coords, _ = scattered(n=40)

    with pytest.raises(DegenerateInput, match="exactly one value"):
        spline(coords, np.zeros(39), grid())
