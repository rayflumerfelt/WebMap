"""Fault network validation, cleaning, and rasterisation. `05-geoprocessing.md` §4.

`12-roadmap.md` Phase 4: "fault network validation catches all defects in the
hostile fault fixture, each with a location." The hostile fixture is built here
rather than loaded, so every defect in it is deliberate and named.

The design rule under test throughout is §4's: **never silently repair a fault
network.** Validation reports; cleaning is separate and produces a changelog.
The distinction between "this fault tips out here" and "this fault trace is
incomplete" is geological judgment, and a preprocessing step that closes a
compartment changes which wells are believed to be in communication.
"""

from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import LineString

from webmap_geo.exceptions import DegenerateInput
from webmap_geo.faults.network import (
    Constraint,
    ConstraintKind,
    clean_network,
    validate_network,
)
from webmap_geo.faults.raster import (
    blocked_edges,
    compartments,
    control_per_compartment,
)
from webmap_geo.frame import AnalysisFrame
from webmap_geo.grid import GridDefinition
from webmap_geo.interpolate.minimum_curvature import minimum_curvature

TEXAS = AnalysisFrame(srid=2277, units="usft")
TOLERANCE = 50.0


def fault(name: str, *coords: tuple[float, float]) -> Constraint:
    return Constraint(geometry=LineString(coords), kind=ConstraintKind.FAULT, name=name)


# --- constraint kinds --------------------------------------------------------


def test_a_breakline_without_z_is_refused() -> None:
    """**The two constraint types are different physics** (`CLAUDE.md` §13).

    A breakline is continuous in value and discontinuous in gradient, and
    carries its own elevations. One without Z is either a fault or incomplete
    data — and treating it as a fault would cut a surface that should not be
    cut.
    """
    with pytest.raises(DegenerateInput, match="either a fault"):
        Constraint(
            geometry=LineString([(0, 0), (100, 100)]),
            kind=ConstraintKind.BREAKLINE,
            name="terrace edge",
        )


def test_a_breakline_needs_one_z_per_vertex() -> None:
    with pytest.raises(DegenerateInput, match="Every vertex needs an elevation"):
        Constraint(
            geometry=LineString([(0, 0), (100, 100), (200, 50)]),
            kind=ConstraintKind.BREAKLINE,
            name="terrace",
            z_values=np.array([100.0, 110.0]),
        )


def test_only_a_fault_is_hard() -> None:
    hard = fault("normal", (0, 0), (100, 0))
    soft = Constraint(
        geometry=LineString([(0, 0), (100, 0)]),
        kind=ConstraintKind.BREAKLINE,
        name="terrace",
        z_values=np.array([100.0, 105.0]),
    )

    assert hard.is_hard
    assert not soft.is_hard


# --- validation --------------------------------------------------------------


def test_a_clean_network_reports_clean() -> None:
    """Two faults meeting end-to-end at a node is an ordinary Y junction, not a
    defect. Reporting it would bury the real problems under the normal ones."""
    network = [
        fault("A", (0, 0), (1_000, 0)),
        fault("B", (1_000, 0), (1_500, 800)),
    ]

    report = validate_network(network, TOLERANCE)

    assert report.is_clean, report.describe()
    assert "clean" in report.describe()


def test_a_crossing_without_a_shared_node_is_caught_with_its_location() -> None:
    """**The defect that breaks triangulation.** Two segments crossing without
    a shared node produce overlapping triangles, and the mesh then says two
    compartments are connected where the map shows a fault."""
    network = [
        fault("east-west", (0, 500), (1_000, 500)),
        fault("north-south", (500, 0), (500, 1_000)),
    ]

    report = validate_network(network, TOLERANCE)

    assert len(report.crossing_pairs) == 1
    at = report.crossing_pairs[0]["at"]
    assert at == pytest.approx((500.0, 500.0))
    assert "500" in report.describe()


def test_a_dangling_end_is_caught_with_its_gap() -> None:
    """A near miss. The gap is reported so a geologist can judge whether it is
    a tip or an incomplete trace."""
    network = [
        fault("main", (0, 0), (1_000, 0)),
        # Ends 20 units short of `main` — inside tolerance, so a near miss.
        fault("splay", (500, 20), (800, 400)),
    ]

    report = validate_network(network, TOLERANCE)

    assert len(report.dangles) == 1
    assert report.dangles[0]["gap"] == pytest.approx(20.0)
    assert report.dangles[0]["end"] == "start"


def test_a_fault_tipping_out_is_not_reported_as_a_dangle() -> None:
    """**The distinction §4 protects.** An end far from everything is a fault
    tipping out, which is ordinary geology. Reporting it would fill the report
    with things that are correct and make the real defects invisible."""
    network = [
        fault("main", (0, 0), (1_000, 0)),
        fault("far", (500, 5_000), (800, 5_400)),
    ]

    report = validate_network(network, TOLERANCE)

    assert report.dangles == []


def test_a_zero_length_trace_is_caught() -> None:
    network = [
        fault("real", (0, 0), (1_000, 0)),
        Constraint(
            geometry=LineString([(500, 500), (500, 500)]),
            kind=ConstraintKind.FAULT,
            name="degenerate",
        ),
    ]

    report = validate_network(network, TOLERANCE)

    assert len(report.zero_length) == 1
    assert report.zero_length[0]["name"] == "degenerate"


def test_a_duplicate_trace_is_caught() -> None:
    """Digitised twice. Harmless on a map and fatal in a triangulation."""
    network = [
        fault("A", (0, 0), (1_000, 0)),
        fault("A again", (0, 0), (1_000, 0)),
    ]

    report = validate_network(network, TOLERANCE)

    assert len(report.duplicates) == 1


def test_a_self_intersection_is_caught_with_its_location() -> None:
    """A trace that loops back through itself. Triangulation cannot represent
    it, and the crossing point is where a geologist has to look."""
    network = [fault("bowtie", (0, 0), (1_000, 1_000), (1_000, 0), (0, 1_000))]

    report = validate_network(network, TOLERANCE)

    assert len(report.self_intersections) == 1
    assert report.self_intersections[0]["at"] is not None


def test_the_hostile_network_catches_every_defect_with_a_location() -> None:
    """**The Phase 4 criterion**, on a fixture built to contain one of each.

    Every entry must carry a location: a report saying "5 problems" sends
    someone hunting through a hundred traces, and one saying where sends them
    to the spot.
    """
    hostile = [
        fault("crossing-A", (0, 500), (1_000, 500)),
        fault("crossing-B", (500, 0), (500, 1_000)),
        fault("dangling", (200, 530), (200, 900)),
        fault("duplicate-1", (2_000, 0), (2_000, 1_000)),
        fault("duplicate-2", (2_000, 0), (2_000, 1_000)),
        Constraint(
            geometry=LineString([(3_000, 3_000), (3_000, 3_000)]),
            kind=ConstraintKind.FAULT,
            name="zero-length",
        ),
        fault("bowtie", (4_000, 0), (5_000, 1_000), (5_000, 0), (4_000, 1_000)),
    ]

    report = validate_network(hostile, TOLERANCE)

    assert report.crossing_pairs, "the crossing was missed"
    assert report.dangles, "the dangle was missed"
    assert report.duplicates, "the duplicate was missed"
    assert report.zero_length, "the zero-length trace was missed"
    assert report.self_intersections, "the self-intersection was missed"

    for group in (
        report.crossing_pairs,
        report.dangles,
        report.duplicates,
        report.self_intersections,
    ):
        for item in group:
            assert item.get("at") is not None, (
                f"a defect was reported without a location: {item}"
            )


def test_a_non_positive_tolerance_says_what_a_good_one_is() -> None:
    with pytest.raises(DegenerateInput, match="median control-point spacing"):
        validate_network([fault("A", (0, 0), (1, 1))], 0.0)


# --- cleaning ----------------------------------------------------------------


def test_cleaning_produces_a_changelog_of_everything_it_changed() -> None:
    """**§4's design rule.** The rule against silent repair is only honoured if
    a geologist can read what happened."""
    network = [
        fault("A", (0, 500), (1_000, 500)),
        fault("B", (500, 0), (500, 1_000)),
        Constraint(
            geometry=LineString([(9_000, 9_000), (9_000, 9_000)]),
            kind=ConstraintKind.FAULT,
            name="degenerate",
        ),
    ]
    report = validate_network(network, TOLERANCE)

    _, changelog = clean_network(network, report, TOLERANCE)

    assert changelog, "cleaning changed things and said nothing"
    assert any("zero-length" in entry for entry in changelog)
    assert any("Noded" in entry for entry in changelog)


def test_cleaning_a_clean_network_changes_nothing() -> None:
    network = [fault("A", (0, 0), (1_000, 0)), fault("B", (1_000, 0), (1_500, 800))]
    report = validate_network(network, TOLERANCE)

    cleaned, changelog = clean_network(network, report, TOLERANCE)

    assert changelog == []
    assert len(cleaned) == 2


def test_cleaning_makes_a_crossing_network_valid() -> None:
    """The point of the exercise: a network that could not be triangulated
    becomes one that can."""
    network = [
        fault("east-west", (0, 500), (1_000, 500)),
        fault("north-south", (500, 0), (500, 1_000)),
    ]
    report = validate_network(network, TOLERANCE)

    cleaned, _ = clean_network(network, report, TOLERANCE)
    after = validate_network(cleaned, TOLERANCE)

    assert after.crossing_pairs == [], after.describe()


def test_a_dangle_beyond_tolerance_is_left_open() -> None:
    """**Never silently repair.** An unresolved dangle means the compartment is
    open there, which may be geologically correct — and closing it changes
    which wells are believed to be in communication."""
    network = [
        fault("main", (0, 0), (1_000, 0)),
        fault("far", (500, 900), (800, 1_400)),
    ]
    report = validate_network(network, TOLERANCE)

    cleaned, changelog = clean_network(network, report, TOLERANCE)

    assert not any("Snapped" in entry for entry in changelog)
    assert cleaned[1].geometry.coords[0] == (500.0, 900.0)


def test_a_near_dangle_is_snapped_and_said_so() -> None:
    network = [
        fault("main", (0, 0), (1_000, 0)),
        fault("splay", (500, 20), (800, 400)),
    ]
    report = validate_network(network, TOLERANCE)

    cleaned, changelog = clean_network(network, report, TOLERANCE)

    assert any("Snapped" in entry for entry in changelog)
    assert cleaned[1].geometry.distance(cleaned[0].geometry) == pytest.approx(0.0, abs=1e-6)


# --- rasterisation -----------------------------------------------------------


def grid(nx: int = 21, ny: int = 21, cell: float = 100.0) -> GridDefinition:
    return GridDefinition(xmin=0.0, ymin=0.0, cell_size=cell, nx=nx, ny=ny, frame=TEXAS)


def test_a_north_south_fault_blocks_east_west_links() -> None:
    """**The naming trap.** A north-south fault blocks east-west links.
    Getting it backwards makes the surface step across the fault's strike
    instead of along it, which looks like structure and is an indexing error.
    """
    g = grid()
    trace = [fault("N-S", (1_000.0, -100.0), (1_000.0, 2_100.0))]

    vertical, horizontal = blocked_edges(trace, g)

    assert horizontal.any(), "a north-south fault blocked no east-west links"
    assert not vertical.any(), "a north-south fault blocked a north-south link"
    # Column 1000 is index 10; the blocked link is the one either side of it.
    assert horizontal[:, 9].any() or horizontal[:, 10].any()


def test_an_east_west_fault_blocks_north_south_links() -> None:
    g = grid()
    trace = [fault("E-W", (-100.0, 1_000.0), (2_100.0, 1_000.0))]

    vertical, horizontal = blocked_edges(trace, g)

    assert vertical.any()
    assert not horizontal.any()


def test_a_breakline_blocks_nothing() -> None:
    """A breakline is continuous in value (`CLAUDE.md` §13). Blocking an edge
    for one would cut a surface that should not be cut."""
    g = grid()
    soft = [
        Constraint(
            geometry=LineString([(1_000.0, -100.0), (1_000.0, 2_100.0)]),
            kind=ConstraintKind.BREAKLINE,
            name="terrace",
            z_values=np.array([100.0, 120.0]),
        )
    ]

    vertical, horizontal = blocked_edges(soft, g)

    assert not vertical.any()
    assert not horizontal.any()


def test_a_fault_separates_the_grid_into_two_compartments() -> None:
    """A compartment is a region bounded by faults (`CLAUDE.md` §13). A fault
    across the whole grid makes two."""
    g = grid()
    trace = [fault("N-S", (1_000.0, -100.0), (1_000.0, 2_100.0))]

    labels, count = compartments(blocked_edges(trace, g), g)

    assert count == 2
    assert labels[10, 0] != labels[10, 20]


def test_a_fault_tipping_out_leaves_one_compartment() -> None:
    """**The geology this is for.** A fault that does not cut the whole area
    leaves the surface connected around its tip — which is exactly why a
    barrier-aware method is needed rather than a hard split."""
    g = grid()
    trace = [fault("partial", (1_000.0, -100.0), (1_000.0, 1_000.0))]

    _, count = compartments(blocked_edges(trace, g), g)

    assert count == 1, "a fault that tips out should not close a compartment"


def test_a_diagonal_fault_cannot_be_leaked_through() -> None:
    """A fault crossing a cell corner separates all four cells. Leaving one
    link open would let the surface leak diagonally through the fault — a
    single-cell gap that is invisible on a map and wrong everywhere past it."""
    g = grid()
    trace = [fault("diagonal", (-100.0, -100.0), (2_100.0, 2_100.0))]

    labels, count = compartments(blocked_edges(trace, g), g)

    assert count >= 2, "a diagonal fault was leaked through"
    # The corners *off* the diagonal. The northeast and southwest corners lie
    # on the fault itself, so they are on the same side of it by the on-centre
    # convention — comparing those would test the convention, not the leak.
    northwest = labels[0, 0]
    southeast = labels[20, 20]
    assert northwest != southeast, "the surface leaked across a diagonal fault"


def test_control_counts_name_a_compartment_with_no_wells() -> None:
    """`05` §6.5's diagnostic. A compartment with two wells and one with two
    hundred are drawn with the same colours and the same contour interval, and
    nothing on the map distinguishes them."""
    g = grid()
    trace = [fault("N-S", (1_000.0, -100.0), (1_000.0, 2_100.0))]
    labels, _ = compartments(blocked_edges(trace, g), g)

    # All the control on the western side.
    points = np.array([[200.0, 400.0], [300.0, 900.0], [500.0, 1_500.0]])
    tally = control_per_compartment(points, labels, g)

    assert len(tally) == 2
    assert sorted(tally.values()) == [0, 3], "an empty compartment was not reported"


def test_a_rasterised_fault_makes_the_solver_step() -> None:
    """**Sub-phase 4, end to end.** Traces in, a stepped surface out.

    This is the join between the geology and the arithmetic: the validation,
    the rasterisation and the solver all have to agree about where the fault
    is, and an off-by-one anywhere puts it half a cell from where it was drawn.
    """
    g = grid()
    trace = [fault("sealing", (1_000.0, -100.0), (1_000.0, 2_100.0))]

    west = np.column_stack([np.full(6, 400.0), np.linspace(200.0, 1_800.0, 6)])
    east = np.column_stack([np.full(6, 1_600.0), np.linspace(200.0, 1_800.0, 6)])
    points = np.vstack([west, east])
    values = np.concatenate([np.full(6, 100.0), np.full(6, 200.0)])

    unfaulted = minimum_curvature(points, values, g)
    faulted = minimum_curvature(points, values, g, blocked_edges=blocked_edges(trace, g))

    # Sample either side of the fault, as the Phase 4 criterion asks.
    row = 10
    unfaulted_step = abs(unfaulted.estimate[row, 11] - unfaulted.estimate[row, 9])
    faulted_step = abs(faulted.estimate[row, 11] - faulted.estimate[row, 9])

    assert faulted_step > unfaulted_step * 3, (
        f"the surface did not honour the fault "
        f"(faulted step {faulted_step:.1f} vs unfaulted {unfaulted_step:.1f})"
    )
    # And each side sits near its own control, rather than compromising.
    assert faulted.estimate[row, 4] == pytest.approx(100.0, abs=8.0)
    assert faulted.estimate[row, 16] == pytest.approx(200.0, abs=8.0)
