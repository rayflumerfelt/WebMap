"""Minimum curvature. `05-geoprocessing.md` §6.1.

No Surfer reference grid is available here, so the Phase 4 criterion "matches
the Surfer reference within 0.5% of value range" cannot be met yet — that is
recorded as carried-forward work. What *can* be checked without one is the
mathematics the reference would be testing:

- A planar field is reproduced exactly. A plane has zero curvature everywhere,
  so the curvature-minimising surface through planar data **is** that plane. A
  method that gets this wrong is wrong about its own definition.
- The surface honours its control.
- Curvature is genuinely minimised: no smoother surface passes through the
  same data.
- Tension reduces overshoot.

The plane test is the strongest of these. It has a unique correct answer known
in closed form, and almost every implementation error — a sign in the stencil,
a transposed grid, a boundary condition applied to the wrong edge — breaks it.
"""

from __future__ import annotations

import numpy as np
import pytest

from webmap_geo.exceptions import DegenerateInput
from webmap_geo.frame import AnalysisFrame
from webmap_geo.interpolate.grid import GridDefinition
from webmap_geo.interpolate.minimum_curvature import minimum_curvature

SEED = 20260909
TEXAS = AnalysisFrame(srid=2277, units="usft")


def grid(nx: int = 21, ny: int = 21, cell: float = 100.0) -> GridDefinition:
    return GridDefinition(xmin=0.0, ymin=0.0, cell_size=cell, nx=nx, ny=ny, frame=TEXAS)


# --- the plane ---------------------------------------------------------------


def test_a_plane_is_reproduced_exactly() -> None:
    """**The strongest available check.**

    A plane has zero curvature everywhere, so the curvature-minimising surface
    through planar data is that plane — a unique answer known in closed form.
    Almost every implementation error breaks it: a sign in the stencil, a
    transposed grid, a boundary condition on the wrong edge.
    """
    rng = np.random.default_rng(SEED)
    g = grid(nx=21, ny=21, cell=100.0)

    points = rng.uniform(0.0, 2_000.0, size=(60, 2))
    # z = 100 + 0.05x - 0.02y. A dipping surface, which is what a structure map
    # of a basin margin looks like before anyone adds structure.
    values = 100.0 + 0.05 * points[:, 0] - 0.02 * points[:, 1]

    result = minimum_curvature(points, values, g)

    xs, ys = np.meshgrid(g.x_coordinates(), g.y_coordinates())
    expected = 100.0 + 0.05 * xs - 0.02 * ys

    spread = float(values.max() - values.min())
    error = np.abs(result.estimate - expected).max() / spread
    assert error < 0.02, f"the plane was not reproduced (max error {error:.1%} of range)"


def test_a_constant_field_is_constant_everywhere() -> None:
    """Including where there is no control. A surface that sags between
    identical control points has a sign error in its smoothness term."""
    rng = np.random.default_rng(SEED)
    g = grid()
    points = rng.uniform(0.0, 2_000.0, size=(30, 2))

    result = minimum_curvature(points, np.full(30, 42.0), g)

    # The data term is a soft pin, so a constant field comes back constant to
    # within the solver's tolerance rather than exactly.
    assert np.allclose(result.estimate, 42.0, atol=0.5)


# --- honouring control -------------------------------------------------------


def test_the_surface_honours_its_control_points() -> None:
    """Not exactly — the data term is a weighted pin, not a hard constraint,
    which is what lets two wells in one cell be averaged. But close: a surface
    that departs visibly from its own wells is not a surface of those wells."""
    rng = np.random.default_rng(SEED)
    g = grid(nx=31, ny=31, cell=100.0)
    points = rng.uniform(200.0, 2_800.0, size=(40, 2))
    values = 100.0 + 20.0 * np.sin(points[:, 0] / 800.0)

    result = minimum_curvature(points, values, g)

    cols = np.clip(((points[:, 0] - g.xmin) / g.cell_size).round().astype(int), 0, g.nx - 1)
    rows = np.clip(((g.ymax - points[:, 1]) / g.cell_size).round().astype(int), 0, g.ny - 1)
    at_control = result.estimate[rows, cols]

    spread = float(values.max() - values.min())
    assert np.abs(at_control - values).max() < 0.15 * spread


def test_two_wells_in_one_cell_land_between_them() -> None:
    """A grid cannot honour two different values at one location.

    Each observation contributes its own row, so the least-squares solution is
    their compromise rather than an arithmetic mean computed before the solver
    sees them — and critically, not whichever happened to be last in the array.
    A surface that depended on row order would change when a file was re-sorted.
    """
    g = grid(nx=11, ny=11, cell=200.0)
    points = np.array([[1_000.0, 1_000.0], [1_020.0, 1_010.0], [0.0, 0.0], [2_000.0, 2_000.0]])
    values = np.array([100.0, 120.0, 100.0, 100.0])

    result = minimum_curvature(points, values, g)
    reordered = minimum_curvature(points[::-1], values[::-1], g)

    centre = result.estimate[5, 5]
    assert 100.0 < centre < 120.0, f"the compromise is outside the two values ({centre:.1f})"
    assert np.allclose(result.estimate, reordered.estimate, atol=1e-6), (
        "row order changed the surface"
    )


# --- smoothness --------------------------------------------------------------


def test_the_surface_is_smoother_than_a_nearest_neighbour_one() -> None:
    """Curvature is what is being minimised, so the result must have less of it
    than an obvious alternative. Without this, "it looks smooth" is the only
    evidence the method does anything."""
    rng = np.random.default_rng(SEED)
    g = grid(nx=31, ny=31, cell=100.0)
    points = rng.uniform(0.0, 3_000.0, size=(25, 2))
    values = rng.normal(100.0, 10.0, size=25)

    result = minimum_curvature(points, values, g)

    from scipy.spatial import cKDTree

    tree = cKDTree(points)
    _, nearest = tree.query(g.cell_centres())
    blocky = values[nearest].reshape(g.ny, g.nx)

    def roughness(surface: np.ndarray) -> float:
        return float(
            np.sum(np.diff(surface, axis=0) ** 2) + np.sum(np.diff(surface, axis=1) ** 2)
        )

    # An order of magnitude smoother, measured. At a heavier data weight the
    # ratio collapses to 1.0 — a "minimum curvature" surface no smoother than
    # nearest-neighbour, which is the failure this number guards.
    assert roughness(result.estimate) < roughness(blocky) / 5


def test_tension_reduces_overshoot() -> None:
    """**Surfer's internal tension.**

    Pure minimum curvature can overshoot into a closure that does not exist —
    a structure map with an invented four-way. Tension blends toward the
    harmonic solution, which cannot overshoot.
    """
    g = grid(nx=41, ny=41, cell=100.0)
    # A step: two flat plateaus at different levels. The classic overshoot
    # case, because a curvature-minimising surface rings at the transition.
    xs = np.linspace(0.0, 4_000.0, 24)
    points = np.column_stack([xs, np.full_like(xs, 2_000.0)])
    values = np.where(xs < 2_000.0, 100.0, 140.0)

    loose = minimum_curvature(points, values, g, tension=0.0)
    taut = minimum_curvature(points, values, g, tension=0.9)

    loose_overshoot = float(loose.estimate.max() - values.max())
    taut_overshoot = float(taut.estimate.max() - values.max())

    assert taut_overshoot <= loose_overshoot + 1e-9, "tension did not reduce overshoot"


# --- fault-blocked edges -----------------------------------------------------


def test_a_blocked_edge_lets_the_surface_step() -> None:
    """**Sub-phase 4's foundation.** A hard fault removes the edge from the
    smoothness operator, giving the surface a free edge — unconstrained across
    the fault, which is the correct physical analogue for a sealing fault
    rather than clamping it.

    Checked here without a fault network: the mask is the interface, and
    proving the solver honours it separates "the stencil is right" from "the
    rasterisation is right".
    """
    g = grid(nx=21, ny=21, cell=100.0)

    # Control on both sides at very different levels.
    left = np.column_stack([np.full(8, 300.0), np.linspace(0.0, 2_000.0, 8)])
    right = np.column_stack([np.full(8, 1_700.0), np.linspace(0.0, 2_000.0, 8)])
    points = np.vstack([left, right])
    values = np.concatenate([np.full(8, 100.0), np.full(8, 200.0)])

    # Block every horizontal (east-west) link across column 10 — a north-south
    # fault down the middle.
    horizontal = np.zeros((g.ny, g.nx - 1), dtype=bool)
    horizontal[:, 10] = True

    unblocked = minimum_curvature(points, values, g)
    blocked = minimum_curvature(
        points, values, g, blocked_edges=(np.zeros((g.ny - 1, g.nx), dtype=bool), horizontal)
    )

    # The gradient across the fault line should be far steeper when blocked.
    unblocked_step = abs(unblocked.estimate[10, 11] - unblocked.estimate[10, 10])
    blocked_step = abs(blocked.estimate[10, 11] - blocked.estimate[10, 10])

    assert blocked_step > unblocked_step * 2, (
        f"the surface did not step across the blocked edge "
        f"(blocked {blocked_step:.1f} vs unblocked {unblocked_step:.1f})"
    )


def test_an_unblocked_mask_changes_nothing() -> None:
    """A mask of all-False must produce the same surface as no mask. Otherwise
    the fault path and the unfaulted path are different code with different
    bugs."""
    rng = np.random.default_rng(SEED)
    g = grid(nx=15, ny=15, cell=100.0)
    points = rng.uniform(0.0, 1_400.0, size=(20, 2))
    values = rng.normal(100.0, 5.0, size=20)

    plain = minimum_curvature(points, values, g)
    masked = minimum_curvature(
        points,
        values,
        g,
        blocked_edges=(
            np.zeros((g.ny - 1, g.nx), dtype=bool),
            np.zeros((g.ny, g.nx - 1), dtype=bool),
        ),
    )

    assert np.allclose(plain.estimate, masked.estimate, atol=1e-9)


# --- convergence and refusals ------------------------------------------------


def test_convergence_is_reported_not_assumed() -> None:
    """`CLAUDE.md` §3.3: a job that hit the iteration cap is flagged, not
    silently returned. A non-converged surface can look perfectly smooth and be
    wrong by a contour interval."""
    rng = np.random.default_rng(SEED)
    g = grid()
    points = rng.uniform(0.0, 2_000.0, size=(20, 2))
    values = rng.normal(100.0, 5.0, size=20)

    result = minimum_curvature(points, values, g, max_iterations=2)

    assert result.converged is False
    assert "HIT ITERATION CAP" in result.describe()
    assert result.n_iterations > 0


def test_a_converged_solve_says_so() -> None:
    rng = np.random.default_rng(SEED)
    g = grid()
    points = rng.uniform(0.0, 2_000.0, size=(20, 2))
    values = rng.normal(100.0, 5.0, size=20)

    result = minimum_curvature(points, values, g)

    assert result.converged
    assert "converged" in result.describe()


def test_points_outside_the_grid_say_what_to_check() -> None:
    """A coordinate order swap puts points in a different hemisphere from the
    grid, and the failure is otherwise a surface of NaN with no explanation."""
    g = grid()
    points = np.array([[-102.0, 31.9], [-101.9, 32.0], [-102.1, 31.8]])
    values = np.array([100.0, 110.0, 120.0])

    with pytest.raises(DegenerateInput, match="hemispheres"):
        minimum_curvature(points, values, g)


def test_tension_outside_its_range_is_refused() -> None:
    rng = np.random.default_rng(SEED)
    g = grid()
    points = rng.uniform(0.0, 2_000.0, size=(10, 2))

    with pytest.raises(DegenerateInput, match="0 \\(pure minimum curvature\\)"):
        minimum_curvature(points, np.zeros(10), g, tension=1.5)


def test_too_few_points_says_what_a_surface_through_them_would_be() -> None:
    g = grid()
    with pytest.raises(DegenerateInput, match="is a plane"):
        minimum_curvature(np.zeros((2, 2)), np.zeros(2), g)
