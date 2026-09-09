"""Constrained triangulation and path distance. `05-geoprocessing.md` §5, §6.2.

Two properties carry everything else:

- **No triangle spans a fault.** That is what makes the constraint structural
  rather than a matter of every downstream caller remembering to check.
- **Path distance across a sealing fault is not the straight-line distance.**
  A surface built on the straight-line one smears throw across the fault while
  the map draws the fault on top of it — geologically wrong and entirely
  plausible.
"""

from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import LineString

from webmap_geo.exceptions import DegenerateInput
from webmap_geo.faults.network import Constraint, ConstraintKind
from webmap_geo.frame import AnalysisFrame
from webmap_geo.mesh import (
    FAULT_SEGMENT,
    barrier_aware_neighbours,
    build_mesh,
    compartment_of,
    nearest_vertex,
    path_distances,
)

TEXAS = AnalysisFrame(srid=2277, units="usft")
BBOX = (0.0, 0.0, 1000.0, 1000.0)


def scattered(n: int = 60, seed: int = 20260909) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.uniform(50.0, 950.0, size=(n, 2))


def wall(x: float = 500.0) -> Constraint:
    """A north-south fault clean across the domain, sealing it in two."""
    return Constraint(
        geometry=LineString([(x, -10.0), (x, 1010.0)]),
        kind=ConstraintKind.FAULT,
        name="Sealing Fault",
    )


def tipping_fault(x: float = 500.0) -> Constraint:
    """A fault that stops half way, so the two sides connect round its tip."""
    return Constraint(
        geometry=LineString([(x, -10.0), (x, 500.0)]),
        kind=ConstraintKind.FAULT,
        name="Tipping Fault",
    )


# --- the structural guarantee -------------------------------------------------


def test_no_triangle_spans_a_fault() -> None:
    """**The property the mesh exists for.** A triangle crossing a fault would
    let every mesh-based operation interpolate across it, and no amount of
    care downstream could undo that."""
    mesh = build_mesh(scattered(), [wall()], BBOX, TEXAS)

    for a, b, c in mesh.triangles:
        xs = mesh.vertices[[a, b, c]][:, 0]
        # A triangle spanning the fault has vertices strictly either side of
        # x = 500. Vertices *on* the fault belong to both sides and are fine.
        assert not (xs.min() < 500.0 - 1e-9 and xs.max() > 500.0 + 1e-9), (
            "a triangle spans the fault"
        )


def test_the_fault_appears_as_constrained_edges() -> None:
    """Guaranteed present in the output, which is what `p` buys."""
    mesh = build_mesh(scattered(), [wall()], BBOX, TEXAS)

    fault_edges = mesh.segments[mesh.segment_kind == FAULT_SEGMENT]
    assert len(fault_edges), "the fault was not preserved as a constraint"
    for a, b in fault_edges:
        assert mesh.vertices[a][0] == pytest.approx(500.0)
        assert mesh.vertices[b][0] == pytest.approx(500.0)


def test_refinement_splits_a_fault_and_keeps_every_piece_a_fault() -> None:
    """**The bug a naive implementation has.** Quality refinement splits a
    constrained segment, and the pieces come back with new vertex indices. If
    the kind is not carried through the marker channel, a split fault becomes
    a set of unmarked edges and stops blocking anything."""
    mesh = build_mesh(scattered(), [wall()], BBOX, TEXAS, max_area=5_000.0)

    fault_edges = mesh.segments[mesh.segment_kind == FAULT_SEGMENT]
    assert len(fault_edges) > 1, "refinement did not split the fault; test is inert"
    for a, b in fault_edges:
        assert mesh.vertices[a][0] == pytest.approx(500.0)
        assert mesh.vertices[b][0] == pytest.approx(500.0)


def test_a_breakline_is_a_constraint_but_not_a_barrier() -> None:
    """`CLAUDE.md` §13. A breakline is a gradient discontinuity with value
    continuity — it belongs in the triangulation and must not block a path."""
    breakline = Constraint(
        geometry=LineString([(500.0, -10.0), (500.0, 1010.0)]),
        kind=ConstraintKind.BREAKLINE,
        z_values=np.array([100.0, 100.0]),
        name="Channel Margin",
    )
    mesh = build_mesh(scattered(), [breakline], BBOX, TEXAS)

    assert not (mesh.segment_kind == FAULT_SEGMENT).any()
    assert not mesh.split_vertices, "a breakline split a vertex"
    assert compartment_of(mesh).max() == 0, "a breakline split the domain"


def test_max_area_makes_the_mesh_finer() -> None:
    """`05` §5: omitting it produces a mesh too coarse to sample accurately —
    only as fine as the control spacing, which in a sparse area is thousands
    of feet."""
    coarse = build_mesh(scattered(), [wall()], BBOX, TEXAS)
    fine = build_mesh(scattered(), [wall()], BBOX, TEXAS, max_area=500.0)

    # `q30` already refines a good deal on its own — 750 triangles here with no
    # area cap — so the comparison is against that, not against a raw Delaunay.
    assert len(fine.triangles) > 2 * len(coarse.triangles)


def test_a_control_point_coincident_with_a_fault_vertex_is_accepted() -> None:
    """A fault trace starting where a well sits is not unusual, and
    Shewchuk's triangle rejects duplicated input vertices outright rather than
    merging them."""
    points = np.vstack([scattered(20), [[500.0, -10.0]]])

    mesh = build_mesh(points, [wall()], BBOX, TEXAS)

    assert len(mesh.triangles) > 0


def test_values_land_on_the_control_vertices_and_nowhere_else() -> None:
    """Refinement adds Steiner points; attaching a well's value to one a few
    feet away would put data where there is none."""
    points = scattered(30)
    values = np.arange(30, dtype=float)

    mesh = build_mesh(points, [wall()], BBOX, TEXAS, max_area=5_000.0, values=values)

    assert mesh.vertex_z is not None
    known = np.isfinite(mesh.vertex_z)
    assert known.sum() == 30, "a value was lost or duplicated"
    assert sorted(mesh.vertex_z[known]) == sorted(values)


# --- refusals -----------------------------------------------------------------


def test_an_inverted_bbox_is_refused_with_the_expected_order() -> None:
    with pytest.raises(DegenerateInput, match="xmin, ymin, xmax, ymax"):
        build_mesh(scattered(), [], (1000.0, 1000.0, 0.0, 0.0), TEXAS)


def test_a_transposed_point_array_is_refused() -> None:
    with pytest.raises(DegenerateInput, match=r"\(n, 2\)"):
        build_mesh(np.zeros((2, 60)), [], BBOX, TEXAS)


def test_a_non_positive_max_area_says_what_it_is_for() -> None:
    with pytest.raises(DegenerateInput, match="twice the cell"):
        build_mesh(scattered(), [], BBOX, TEXAS, max_area=0.0)


# --- path distance ------------------------------------------------------------


def test_a_sealing_fault_makes_the_far_side_unreachable() -> None:
    """**The whole point.** Two wells 20 ft apart either side of a sealing
    fault are not neighbours, and a variogram that treats them as such
    produces a surface with no throw across a fault the map draws."""
    points = np.array([[480.0, 500.0], [520.0, 500.0]])
    mesh = build_mesh(points, [wall()], BBOX, TEXAS)

    west = nearest_vertex(mesh, points[0])
    east = nearest_vertex(mesh, points[1])

    reached = path_distances(mesh, west, {east})

    assert east not in reached, "a path crossed a sealing fault"


def test_a_fault_tip_lets_a_path_round_it_at_the_right_cost() -> None:
    """Not unreachable — *correctly downweighted*. A well on the far side of a
    tipping fault is a legitimate neighbour, and the distance is the way
    round, not the way through."""
    points = np.array([[480.0, 200.0], [520.0, 200.0]])
    mesh = build_mesh(points, [tipping_fault()], BBOX, TEXAS, max_area=10_000.0)

    west = nearest_vertex(mesh, points[0])
    east = nearest_vertex(mesh, points[1])

    reached = path_distances(mesh, west, {east})

    assert east in reached, "the path round the fault tip was not found"
    straight = float(np.hypot(*(points[1] - points[0])))
    # The fault tip is at y = 500 and the points are at y = 200, so the way
    # round is at least twice the 300 ft up plus the 40 ft across.
    assert reached[east] > 600.0
    assert reached[east] > 10 * straight


def test_path_distance_without_faults_is_close_to_straight_line() -> None:
    """A sanity check on the graph itself. Path distance over a triangulated
    plane is a little longer than the straight line — the path follows edges —
    but not by much on a refined mesh, and a large gap would mean the
    adjacency is wrong rather than that the geometry is."""
    points = np.array([[100.0, 500.0], [900.0, 500.0]])
    mesh = build_mesh(points, [], BBOX, TEXAS, max_area=2_000.0)

    a = nearest_vertex(mesh, points[0])
    b = nearest_vertex(mesh, points[1])

    reached = path_distances(mesh, a, {b})

    assert b in reached
    assert 800.0 <= reached[b] < 800.0 * 1.35


def test_the_search_stops_at_its_radius() -> None:
    """`max_distance` bounds the cost. Without it every query walks the whole
    mesh, which at a million grid nodes is the entire budget."""
    points = scattered(40)
    mesh = build_mesh(points, [], BBOX, TEXAS, max_area=5_000.0)
    source = nearest_vertex(mesh, np.array([500.0, 500.0]))
    targets = {int(nearest_vertex(mesh, point)) for point in points}

    near = path_distances(mesh, source, targets, max_distance=200.0)
    far = path_distances(mesh, source, targets, max_distance=2_000.0)

    assert len(near) < len(far)
    assert all(distance <= 200.0 for distance in near.values())


# --- neighbourhoods -----------------------------------------------------------


def test_neighbours_come_back_nearest_first_by_path_distance() -> None:
    points = scattered(40)
    mesh = build_mesh(points, [], BBOX, TEXAS, max_area=5_000.0)
    control = np.array([nearest_vertex(mesh, point) for point in points])
    source = nearest_vertex(mesh, np.array([500.0, 500.0]))

    found = barrier_aware_neighbours(mesh, control, source, k=8)

    assert len(found) == 8
    assert list(found.distances) == sorted(found.distances)


def test_a_neighbourhood_never_reaches_through_a_sealing_fault() -> None:
    """**The property that makes fault-aware kriging fault-aware.** Every
    neighbour must be on the same side of the fault as the target."""
    west_points = np.column_stack([np.linspace(100.0, 450.0, 20), np.full(20, 500.0)])
    east_points = np.column_stack([np.linspace(550.0, 900.0, 20), np.full(20, 500.0)])
    points = np.vstack([west_points, east_points])
    mesh = build_mesh(points, [wall()], BBOX, TEXAS, max_area=10_000.0)
    control = np.array([nearest_vertex(mesh, point) for point in points])

    source = nearest_vertex(mesh, np.array([450.0, 500.0]))
    found = barrier_aware_neighbours(mesh, control, source, k=20)

    assert len(found) > 0
    for index in found.indices:
        assert mesh.vertices[index][0] <= 500.0 + 1e-6, (
            "a neighbour was found on the far side of a sealing fault"
        )


def test_a_target_with_no_reachable_control_returns_nothing() -> None:
    """Absence, not an infinite distance. Reporting infinity invites a caller
    to include the point with a tiny weight, and the right weight is not small
    but missing."""
    west = np.column_stack([np.linspace(100.0, 400.0, 10), np.full(10, 500.0)])
    mesh = build_mesh(west, [wall()], BBOX, TEXAS, max_area=10_000.0)
    control = np.array([nearest_vertex(mesh, point) for point in west])

    source = nearest_vertex(mesh, np.array([900.0, 500.0]))
    found = barrier_aware_neighbours(mesh, control, source, k=8)

    assert len(found) == 0


# --- compartments --------------------------------------------------------------


def test_a_sealing_fault_produces_two_compartments() -> None:
    mesh = build_mesh(scattered(), [wall()], BBOX, TEXAS)

    labels = compartment_of(mesh)

    assert labels.max() == 1, f"expected 2 compartments, got {labels.max() + 1}"


def test_a_tipping_fault_leaves_one_compartment() -> None:
    """A fault that does not reach the domain edge does not seal anything —
    it lengthens the path round its tip, which is a different thing and shows
    up in the distances rather than in the labels."""
    mesh = build_mesh(scattered(), [tipping_fault()], BBOX, TEXAS)

    assert compartment_of(mesh).max() == 0


def test_two_crossing_faults_produce_four_compartments() -> None:
    """The case a rasterised fault mask historically got wrong by leaking
    through the crossing point."""
    crossing = [
        wall(500.0),
        Constraint(
            geometry=LineString([(-10.0, 500.0), (1010.0, 500.0)]),
            kind=ConstraintKind.FAULT,
            name="Cross Fault",
        ),
    ]
    mesh = build_mesh(scattered(), crossing, BBOX, TEXAS)

    assert compartment_of(mesh).max() == 3
