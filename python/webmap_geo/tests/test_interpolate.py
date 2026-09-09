"""The interpolation entry point and its diagnostics. `05-geoprocessing.md` §6.5.

A gridded surface is the most confident-looking artefact this system produces:
smooth, coloured, contoured, and identical in appearance whether it came from
two thousand wells or six. The diagnostics are what make that confidence
answerable — "a grid with 40% of its area extrapolated should say so" — so most
of what is tested here is whether the surface tells the truth about itself.
"""

from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import LineString

from webmap_geo.exceptions import DegenerateInput
from webmap_geo.faults.network import Constraint, ConstraintKind
from webmap_geo.frame import AnalysisFrame
from webmap_geo.grid import GridDefinition
from webmap_geo.interpolate.dispatch import Method, interpolate
from webmap_geo.variogram.model import FittedVariogram

SEED = 20260909
TEXAS = AnalysisFrame(srid=2277, units="usft")


def grid(nx: int = 21, ny: int = 21, cell: float = 100.0) -> GridDefinition:
    return GridDefinition(xmin=0.0, ymin=0.0, cell_size=cell, nx=nx, ny=ny, frame=TEXAS)


def scattered(rng: np.random.Generator, n: int = 60, extent: float = 2_000.0):
    points = rng.uniform(0.0, extent, size=(n, 2))
    values = 100.0 + 20.0 * np.sin(points[:, 0] / 600.0) + 0.01 * points[:, 1]
    return points, values


# --- dispatch ----------------------------------------------------------------


@pytest.mark.parametrize(
    "method",
    [Method.ORDINARY_KRIGING, Method.MINIMUM_CURVATURE, Method.IDW, Method.NEAREST],
)
def test_every_method_produces_a_surface_of_the_right_shape(method: Method) -> None:
    rng = np.random.default_rng(SEED)
    points, values = scattered(rng)
    g = grid()

    result = interpolate(points, values, g, method=method, rng=rng)

    assert result.surface.shape == (g.ny, g.nx)
    assert result.method is method


def test_only_kriging_reports_a_variance() -> None:
    """**A None is more honest than a zero.** Elsewhere the uncertainty is not
    quantified, and a zero variance would read as certainty."""
    rng = np.random.default_rng(SEED)
    points, values = scattered(rng)
    g = grid()

    kriged = interpolate(points, values, g, method=Method.ORDINARY_KRIGING, rng=rng)
    curvature = interpolate(points, values, g, method=Method.MINIMUM_CURVATURE, rng=rng)

    assert kriged.variance is not None
    assert curvature.variance is None


def test_a_variogram_is_fitted_when_none_is_given() -> None:
    """The path Claude takes: it cannot fit a curve, and kriging without a
    variogram is kriging with made-up parameters."""
    rng = np.random.default_rng(SEED)
    points, values = scattered(rng, n=200, extent=6_000.0)
    g = grid(nx=31, ny=31, cell=200.0)

    result = interpolate(points, values, g, rng=rng)

    assert result.lineage["variogram"]["model"] in {
        "spherical",
        "exponential",
        "gaussian",
        "power",
    }
    assert result.lineage["variogram"]["range"] > 0


# --- lineage -----------------------------------------------------------------


def test_the_lineage_is_sufficient_to_reproduce_the_grid() -> None:
    """**The Phase 4 criterion.** Method, every parameter, the fitted
    variogram, the frame, and the package version — a grid re-run under a
    different webmap_geo is not the same grid, and saying so is cheaper than
    discovering it."""
    rng = np.random.default_rng(SEED)
    points, values = scattered(rng, n=200, extent=6_000.0)
    g = grid(nx=31, ny=31, cell=200.0)

    lineage = interpolate(points, values, g, rng=rng).lineage

    assert lineage["method"] == "ordinary_kriging"
    assert lineage["webmap_geo_version"]
    assert lineage["frame"] == {"srid": 2277, "units": "usft"}
    assert lineage["grid"]["cell_size"] == 200.0
    assert lineage["n_control_points"] == 200
    assert "variogram" in lineage
    assert lineage["n_neighbors"] > 0


def test_the_same_inputs_produce_the_same_grid() -> None:
    """`CLAUDE.md` §3.3, and the other half of reproducibility: a lineage
    record is only useful if re-running from it lands in the same place."""
    rng = np.random.default_rng(SEED)
    points, values = scattered(rng, n=150, extent=4_000.0)
    g = grid(nx=21, ny=21, cell=200.0)

    first = interpolate(points, values, g, rng=np.random.default_rng(5))
    second = interpolate(points, values, g, rng=np.random.default_rng(5))

    assert np.array_equal(
        np.nan_to_num(first.surface, nan=-9999.0),
        np.nan_to_num(second.surface, nan=-9999.0),
    )


# --- diagnostics -------------------------------------------------------------


def test_an_extrapolated_grid_says_so_prominently() -> None:
    """**"A grid with 40% of its area extrapolated should say so."**

    It is drawn with the same colours and the same contour interval as the
    well-controlled part, and nothing on the map distinguishes them.
    """
    rng = np.random.default_rng(SEED)
    # Control clustered in one corner of a much larger grid.
    points = rng.uniform(0.0, 400.0, size=(30, 2))
    values = rng.normal(100.0, 5.0, size=30)
    g = grid(nx=41, ny=41, cell=100.0)

    # An explicit variogram, because thirty points inside a 400-unit square
    # cannot support a fitted one — auto-fitting refuses them, correctly, and
    # this test is about the extrapolation report rather than about fitting.
    result = interpolate(
        points,
        values,
        g,
        method=Method.ORDINARY_KRIGING,
        variogram=FittedVariogram(model="spherical", nugget=0.0, sill=25.0, range_=500.0),
        max_radius=500.0,
        rng=rng,
    )

    assert result.diagnostics["extrapolated_fraction"] > 0.4
    assert result.is_mostly_extrapolated
    assert any("extrapolated" in w for w in result.warnings)
    assert any("do not read structure" in w for w in result.warnings)


def test_a_well_controlled_grid_carries_no_extrapolation_warning() -> None:
    rng = np.random.default_rng(SEED)
    points, values = scattered(rng, n=120)
    g = grid()

    result = interpolate(points, values, g, method=Method.MINIMUM_CURVATURE, rng=rng)

    assert result.diagnostics["extrapolated_fraction"] == 0.0
    assert not any("extrapolated" in w for w in result.warnings)


def test_the_input_and_output_ranges_are_both_reported() -> None:
    """Overshoot is only visible as a comparison, so both have to be there."""
    rng = np.random.default_rng(SEED)
    points, values = scattered(rng)

    result = interpolate(points, values, grid(), method=Method.MINIMUM_CURVATURE, rng=rng)

    assert result.diagnostics["input_range"] == [
        pytest.approx(float(values.min())),
        pytest.approx(float(values.max())),
    ]
    assert result.diagnostics["output_range"] is not None


def test_cross_validation_appears_in_the_diagnostics() -> None:
    """The single most useful number a gridding job reports: how far the
    surface is from the data it was built on, in the data's own units."""
    rng = np.random.default_rng(SEED)
    points, values = scattered(rng, n=200, extent=6_000.0)

    result = interpolate(points, values, grid(nx=31, ny=31, cell=200.0), rng=rng)

    validation = result.diagnostics["cross_validation"]
    assert validation["rmse"] > 0
    assert validation["field_std"] > 0


def test_a_surface_no_better_than_the_mean_says_so() -> None:
    """An RMSE near the field's own standard deviation means the property has
    no spatial structure at this well spacing — worth knowing before the map
    goes on a slide."""
    rng = np.random.default_rng(SEED)
    points = rng.uniform(0.0, 6_000.0, size=(200, 2))
    # Pure noise: no spatial structure at all.
    values = rng.normal(100.0, 10.0, size=200)

    result = interpolate(points, values, grid(nx=31, ny=31, cell=200.0), rng=rng)

    assert any("barely better than the mean" in w for w in result.warnings)


# --- constraints -------------------------------------------------------------


def fault(name: str, *coords: tuple[float, float]) -> Constraint:
    return Constraint(geometry=LineString(coords), kind=ConstraintKind.FAULT, name=name)


def test_compartment_control_counts_are_reported() -> None:
    """`05` §6.5. A compartment with two wells and one with two hundred are
    drawn identically, and nothing on the map distinguishes them."""
    rng = np.random.default_rng(SEED)
    g = grid()
    constraints = [fault("N-S", (1_000.0, -100.0), (1_000.0, 2_100.0))]
    # All the control on the western side.
    points = rng.uniform(100.0, 800.0, size=(30, 2))
    values = rng.normal(100.0, 5.0, size=30)

    result = interpolate(
        points, values, g, method=Method.MINIMUM_CURVATURE, constraints=constraints, rng=rng
    )

    assert result.diagnostics["n_compartments"] == 2
    assert len(result.diagnostics["empty_compartments"]) == 1
    assert any("no control points at all" in w for w in result.warnings)


def test_kriging_says_plainly_that_it_ignores_faults() -> None:
    """**Silently ignoring them would be the worst outcome.**

    Barrier-aware kriging needs path distance on a constrained mesh (§6.2),
    which is not built. A surface that smears throw across a sealing fault
    while the map draws the fault on top of it is wrong in a way that looks
    authoritative.
    """
    rng = np.random.default_rng(SEED)
    points, values = scattered(rng)
    constraints = [fault("N-S", (1_000.0, -100.0), (1_000.0, 2_100.0))]

    result = interpolate(
        points, values, grid(), method=Method.ORDINARY_KRIGING, constraints=constraints, rng=rng
    )

    assert any("does not yet honour" in w for w in result.warnings)
    assert any("minimum_curvature" in w for w in result.warnings)


def test_minimum_curvature_honours_a_fault_through_the_entry_point() -> None:
    """End to end, through the API the rest of the system calls."""
    g = grid()
    constraints = [fault("sealing", (1_000.0, -100.0), (1_000.0, 2_100.0))]

    west = np.column_stack([np.full(6, 400.0), np.linspace(200.0, 1_800.0, 6)])
    east = np.column_stack([np.full(6, 1_600.0), np.linspace(200.0, 1_800.0, 6)])
    points = np.vstack([west, east])
    values = np.concatenate([np.full(6, 100.0), np.full(6, 200.0)])

    faulted = interpolate(
        points, values, g, method=Method.MINIMUM_CURVATURE, constraints=constraints
    )
    plain = interpolate(points, values, g, method=Method.MINIMUM_CURVATURE)

    row = 10
    assert abs(faulted.surface[row, 11] - faulted.surface[row, 9]) > 3 * abs(
        plain.surface[row, 11] - plain.surface[row, 9]
    )


# --- method-specific honesty -------------------------------------------------


def test_idw_warns_about_bull_s_eyes() -> None:
    """`05` §6.4: "offer it, do not default to it." Every control point becomes
    a local extremum, which reads as structure."""
    rng = np.random.default_rng(SEED)
    points, values = scattered(rng)

    result = interpolate(points, values, grid(), method=Method.IDW, rng=rng)

    assert any("bull's-eyes" in w for w in result.warnings)


def test_idw_reproduces_a_value_it_lands_on() -> None:
    """An exact hit would divide by zero. The value there is simply that
    point's, and getting it wrong produces an inf that spreads."""
    points = np.array([[500.0, 500.0], [1_500.0, 500.0], [1_000.0, 1_500.0]])
    values = np.array([10.0, 20.0, 30.0])
    g = GridDefinition(xmin=500.0, ymin=500.0, cell_size=500.0, nx=3, ny=3, frame=TEXAS)

    result = interpolate(points, values, g, method=Method.IDW)

    assert np.isfinite(result.surface).all()
    # Row 2 is the southern edge (y=500), column 0 is x=500.
    assert result.surface[2, 0] == pytest.approx(10.0)


def test_nearest_calls_itself_a_diagnostic() -> None:
    rng = np.random.default_rng(SEED)
    points, values = scattered(rng)

    result = interpolate(points, values, grid(), method=Method.NEAREST, rng=rng)

    assert any("not a surface" in w for w in result.warnings)


# --- refusals ----------------------------------------------------------------


def test_too_few_points_names_the_likely_causes() -> None:
    with pytest.raises(DegenerateInput, match="nulls"):
        interpolate(np.zeros((2, 2)), np.zeros(2), grid())


def test_all_nan_values_are_refused_before_gridding() -> None:
    rng = np.random.default_rng(SEED)
    points = rng.uniform(0.0, 2_000.0, size=(50, 2))

    with pytest.raises(DegenerateInput, match="CRS mismatch"):
        interpolate(points, np.full(50, np.nan), grid())


def test_an_unknown_method_is_refused() -> None:
    rng = np.random.default_rng(SEED)
    points, values = scattered(rng)

    with pytest.raises(ValueError, match="natural_neighbour"):
        interpolate(points, values, grid(), method="natural_neighbour")


def test_describe_reads_as_a_caption() -> None:
    """`04-mcp-server.md` §6.1 puts this in render metadata so a caption can
    state the method without re-deriving it from the image."""
    rng = np.random.default_rng(SEED)
    points, values = scattered(rng, n=200, extent=6_000.0)

    described = interpolate(points, values, grid(nx=31, ny=31, cell=200.0), rng=rng).describe()

    assert "ordinary kriging" in described
    assert "variogram" in described
    assert "cells" in described
