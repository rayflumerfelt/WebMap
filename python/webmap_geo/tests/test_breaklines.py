"""Breakline-aware interpolation. `05-geoprocessing.md` §6.1, `CLAUDE.md` §13.

A **breakline** is a soft constraint: value continuous across it, gradient
discontinuous, and it carries its own Z. A **fault** is hard: value
discontinuous. Treating one as the other is not a small error — a breakline
handled as a fault tears a surface that should bend, and a fault handled as a
breakline smears throw that should be a step. Neither failure looks wrong on
the map, which is why these are behavioural tests on the surface rather than
assertions about which rows the operator emitted.

The test field is a **terrace**: flat at 100 west of x = 500, then dipping away
east of it. That is the shape a breakline exists to record, and the shape a
smoothing interpolator rounds off.
"""

from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import LineString

from webmap_geo.exceptions import DegenerateInput
from webmap_geo.faults import (
    Constraint,
    ConstraintKind,
    blocked_edges,
    breakline_control,
    soft_edges,
)
from webmap_geo.frame import AnalysisFrame
from webmap_geo.grid import GridDefinition
from webmap_geo.interpolate.dispatch import Method, interpolate

FRAME = AnalysisFrame(srid=2277, units="ft")
BREAK_X = 500.0


def a_grid(cell_size: float = 25.0) -> GridDefinition:
    return GridDefinition(xmin=0.0, ymin=0.0, cell_size=cell_size, nx=41, ny=41, frame=FRAME)


def terrace(x: np.ndarray) -> np.ndarray:
    """Flat at 100 to the west of the break, dipping 0.1/ft to the east.

    Continuous at `BREAK_X` — the value does not jump — and the slope changes
    from 0 to 0.1 across it. Exactly a breakline, and not a fault.
    """
    return np.where(x <= BREAK_X, 100.0, 100.0 - 0.1 * (x - BREAK_X))


def control(rng: np.random.Generator, n: int = 220) -> tuple[np.ndarray, np.ndarray]:
    """Scattered control on the terrace, with a gap straddling the break.

    The gap matters. With control right up against the line from both sides,
    every method reproduces the kink and the test proves nothing about the
    constraint. The gap is what forces the interpolator to decide for itself
    whether the surface bends there.
    """
    xy = rng.uniform([0.0, 0.0], [1000.0, 1000.0], size=(n * 2, 2))
    away = np.abs(xy[:, 0] - BREAK_X) > 120.0
    xy = xy[away][:n]
    return xy, terrace(xy[:, 0])


def a_breakline() -> Constraint:
    """The break itself, with its own elevations — a constant 100."""
    return Constraint(
        name="terrace edge",
        kind=ConstraintKind.BREAKLINE,
        geometry=LineString([(BREAK_X, -50.0), (BREAK_X, 1050.0)]),
        z_values=np.array([100.0, 100.0]),
    )


def a_fault() -> Constraint:
    return Constraint(
        name="sealing fault",
        kind=ConstraintKind.FAULT,
        geometry=LineString([(BREAK_X, -50.0), (BREAK_X, 1050.0)]),
    )


# --- the masks ------------------------------------------------------------------


def test_a_breakline_marks_soft_edges_and_blocks_none() -> None:
    """The two functions read the same geometry and answer different questions.

    A breakline in `blocked_edges` would sever the surface; a fault in
    `soft_edges` would leave it smooth across a sealing fault. Each must ignore
    the other's kind.
    """
    grid = a_grid()
    breakline = [a_breakline()]

    hard_v, hard_h = blocked_edges(breakline, grid)
    _, soft_h = soft_edges(breakline, grid)

    assert not hard_v.any() and not hard_h.any()
    assert soft_h.any(), "a north-south breakline must mark east-west links"


def test_a_fault_marks_blocked_edges_and_no_soft_ones() -> None:
    grid = a_grid()
    fault = [a_fault()]

    _, hard_h = blocked_edges(fault, grid)
    soft_v, soft_h = soft_edges(fault, grid)

    assert hard_h.any()
    assert not soft_v.any() and not soft_h.any()


def test_the_two_kinds_mark_the_same_links() -> None:
    """Same geometry, same links — the difference is entirely in what the
    solver then does with them, not in where they are."""
    grid = a_grid()
    _, hard_h = blocked_edges([a_fault()], grid)
    _, soft_h = soft_edges([a_breakline()], grid)

    assert np.array_equal(hard_h, soft_h)


# --- densification --------------------------------------------------------------


def test_a_breakline_is_densified_to_about_one_point_per_cell() -> None:
    """A trace digitised with two vertices 44 cells apart constrains 2 cells in
    44 without this, and the surface ignores it in between — which reads as the
    breakline not working, and is really the breakline not being sampled."""
    grid = a_grid()
    xy, z = breakline_control([a_breakline()], grid)

    length = 1100.0
    assert len(xy) == pytest.approx(length / grid.cell_size, abs=2)
    assert np.allclose(z, 100.0)
    assert np.allclose(xy[:, 0], BREAK_X)


def test_z_is_interpolated_by_distance_not_by_vertex_index() -> None:
    """A digitised trace is not evenly spaced. Interpolating on index would
    bunch the elevations wherever someone happened to click densely."""
    grid = a_grid()
    # Three vertices: the middle one sits at 10% of the length, not 50%.
    constraint = Constraint(
        name="ramp",
        kind=ConstraintKind.BREAKLINE,
        geometry=LineString([(100.0, 100.0), (200.0, 100.0), (1100.0, 100.0)]),
        z_values=np.array([0.0, 10.0, 100.0]),
    )
    xy, z = breakline_control([constraint], grid)

    # Z is a linear ramp in x either side of the middle vertex; at x = 600 the
    # value is 10 + 90 * (400/900) = 50.
    at_600 = np.interp(600.0, xy[:, 0], z)
    assert at_600 == pytest.approx(50.0, abs=1.0)


def test_a_fault_contributes_no_control_points() -> None:
    """A fault has no Z of its own — throw is a property of the surfaces either
    side, not of the trace."""
    xy, z = breakline_control([a_fault()], a_grid())
    assert len(xy) == 0 and len(z) == 0


def test_a_breakline_without_z_cannot_be_constructed() -> None:
    """The backstop in `breakline_control` should be unreachable, because
    `Constraint` refuses first."""
    with pytest.raises(DegenerateInput, match="own elevations"):
        Constraint(
            name="no z",
            kind=ConstraintKind.BREAKLINE,
            geometry=LineString([(0.0, 0.0), (1.0, 1.0)]),
        )


# --- what the surface does ------------------------------------------------------


def _surface(constraints: list[Constraint] | None) -> np.ndarray:
    rng = np.random.default_rng(20260910)
    xy, z = control(rng)
    result = interpolate(
        xy,
        z,
        a_grid(),
        method=Method.MINIMUM_CURVATURE,
        constraints=constraints,
        rng=rng,
    )
    return np.asarray(result.surface, dtype=float)


def _kink_sharpness(surface: np.ndarray, grid: GridDefinition) -> float:
    """How abruptly the along-x slope changes at the break, averaged over rows.

    The second difference in x at the break column. A smooth surface rounds the
    corner over several cells and this is small; a surface that honours the
    breakline turns it in one cell and this is large.
    """
    xs = grid.x_coordinates()
    col = int(np.argmin(np.abs(xs - BREAK_X)))
    triple = surface[:, col - 1 : col + 2]
    finite = np.isfinite(triple).all(axis=1)
    second = triple[finite, 0] - 2.0 * triple[finite, 1] + triple[finite, 2]
    return float(np.mean(np.abs(second)))


def test_a_breakline_sharpens_the_kink_it_marks() -> None:
    """**The behavioural claim.** With the breakline the surface turns the
    corner at the line; without it the corner is rounded off across the control
    gap, which is the feature the breakline was drawn to record.
    """
    grid = a_grid()
    without = _kink_sharpness(_surface(None), grid)
    with_break = _kink_sharpness(_surface([a_breakline()]), grid)

    assert with_break > without * 1.5, (
        f"breakline sharpness {with_break:.4g} vs unconstrained {without:.4g} — "
        f"the soft edges or the densified control are not reaching the solver"
    )


def test_a_breakline_does_not_tear_the_surface() -> None:
    """The other half of the definition, and the half a fault gets wrong.

    Value stays continuous across a breakline. The first differences are kept
    across a soft edge for exactly this reason, so the two sides cannot drift
    apart into a step.
    """
    grid = a_grid()
    surface = _surface([a_breakline()])
    xs = grid.x_coordinates()
    col = int(np.argmin(np.abs(xs - BREAK_X)))

    step = np.abs(surface[:, col] - surface[:, col + 1])
    step = step[np.isfinite(step)]
    # The true surface drops 0.1 per foot east of the break, so one cell of
    # honest dip is 2.5 ft. Anything much beyond that is a tear.
    assert step.max() < 6.0, f"largest jump across the breakline is {step.max():.3g} ft"


def test_a_fault_on_the_same_line_does_tear_it() -> None:
    """The control for the test above. If a fault produced the same surface as
    a breakline, neither test would be measuring the constraint.

    The two sides are solved independently across a sealing fault, so the west
    side — flat control at 100 with nothing east of it to pull on — sits at a
    different level from the east side's dip.
    """
    faulted = _surface([a_fault()])
    broken = _surface([a_breakline()])

    assert not np.allclose(
        faulted[np.isfinite(faulted)].sum(), broken[np.isfinite(broken)].sum()
    )


def test_a_breakline_is_reported_in_the_lineage() -> None:
    """`CLAUDE.md` §3.3: a derived grid records how it was made. "There was a
    breakline and it contributed 44 points" is not recoverable afterwards."""
    rng = np.random.default_rng(20260910)
    xy, z = control(rng)
    result = interpolate(
        xy, z, a_grid(), method=Method.MINIMUM_CURVATURE, constraints=[a_breakline()], rng=rng
    )

    assert result.lineage["n_soft_constraints"] == 1
    assert result.lineage["n_breakline_points"] > 20
    # The user supplied these, and the count must not silently include the
    # points the breakline contributed.
    assert result.lineage["n_control_points"] == len(xy)


def test_a_method_that_smooths_across_a_breakline_says_so() -> None:
    """The half-honoured case is the confusing one: the Z *is* used, so the
    surface follows the line and looks right, and only the kink is missing."""
    rng = np.random.default_rng(20260910)
    xy, z = control(rng)
    result = interpolate(
        xy, z, a_grid(), method=Method.IDW, constraints=[a_breakline()], rng=rng
    )

    assert any("breakline" in warning for warning in result.warnings)
    assert any("round the break off" in warning for warning in result.warnings)


def test_minimum_curvature_does_not_warn_about_breaklines() -> None:
    """It honours them, so the warning would be noise — and a warning nobody
    can act on is how real ones stop being read."""
    rng = np.random.default_rng(20260910)
    xy, z = control(rng)
    result = interpolate(
        xy, z, a_grid(), method=Method.MINIMUM_CURVATURE, constraints=[a_breakline()], rng=rng
    )

    assert not any("round the break off" in warning for warning in result.warnings)
