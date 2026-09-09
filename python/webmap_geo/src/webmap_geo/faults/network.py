"""Fault network validation and cleaning. `05-geoprocessing.md` §4.

Raw fault polylines from a geologist's interpretation are almost never
triangulation-ready. Cleaning is a **required step with clear diagnostics, not
a silent fix-up**.

> **Design rule.** Never silently repair a fault network. The distinction
> between "this fault tips out here" and "this fault trace is incomplete" is
> geological judgment, not a preprocessing decision.

That rule is why validation and cleaning are separate functions and why a
dangle beyond tolerance is reported rather than extended. An unresolved dangle
means the compartment is open there, which may be exactly right — and a
compartment silently closed by a preprocessing step changes which wells are
believed to be in communication, which changes the map, which changes a
decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import numpy as np
from numpy.typing import NDArray
from shapely.geometry import LineString, MultiLineString, Point
from shapely.ops import unary_union

from webmap_geo.exceptions import DegenerateInput


class ConstraintKind(StrEnum):
    """`CLAUDE.md` §13 — the two constraint types, different physics.

    A **fault** is a hard constraint: no interpolation across it, value
    discontinuous. A **breakline** is soft: value continuous across it, gradient
    discontinuous, and it carries its own Z.

    Treating one as the other is not a small error. A breakline handled as a
    fault cuts a surface that should be continuous; a fault handled as a
    breakline smears throw that should be a step.
    """

    FAULT = "fault"
    BREAKLINE = "breakline"


@dataclass(frozen=True)
class Constraint:
    """A single fault trace or breakline, in analysis-CRS coordinates."""

    geometry: LineString
    kind: ConstraintKind
    name: str | None = None
    #: Elevations along the line. Required for a breakline, meaningless for a
    #: fault — a fault's Z is whatever the surface does on each side.
    z_values: NDArray[np.floating] | None = None

    def __post_init__(self) -> None:
        if self.kind is ConstraintKind.BREAKLINE and self.z_values is None:
            raise DegenerateInput(
                f"Breakline '{self.name or 'unnamed'}' has no z_values. "
                f"Breaklines carry their own elevations — a breakline without Z "
                f"is either a fault (use kind='fault') or incomplete data."
            )
        if self.z_values is not None:
            expected = len(self.geometry.coords)
            if len(self.z_values) != expected:
                raise DegenerateInput(
                    f"Breakline '{self.name or 'unnamed'}' has {len(self.z_values)} "
                    f"z values for {expected} vertices. Every vertex needs an "
                    f"elevation, or the ones between are undefined."
                )

    @property
    def is_hard(self) -> bool:
        return self.kind is ConstraintKind.FAULT


@dataclass
class ValidationReport:
    """Everything wrong with a fault network, in terms a geologist can act on.

    Every entry carries a **location**. `12-roadmap.md` Phase 4 requires it:
    "fault network validation catches all defects in the hostile fault fixture,
    each with a location." A report saying "3 crossings" sends someone hunting
    through a hundred traces; one saying where sends them to the spot.
    """

    crossing_pairs: list[dict[str, Any]] = field(default_factory=list)
    dangles: list[dict[str, Any]] = field(default_factory=list)
    duplicates: list[dict[str, Any]] = field(default_factory=list)
    zero_length: list[dict[str, Any]] = field(default_factory=list)
    self_intersections: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not any(
            [
                self.crossing_pairs,
                self.dangles,
                self.duplicates,
                self.zero_length,
                self.self_intersections,
            ]
        )

    @property
    def defect_count(self) -> int:
        return sum(
            len(items)
            for items in (
                self.crossing_pairs,
                self.dangles,
                self.duplicates,
                self.zero_length,
                self.self_intersections,
            )
        )

    def describe(self) -> str:
        """A summary a geologist can act on, or a clean bill of health."""
        if self.is_clean:
            return "Fault network is clean: no crossings, dangles, or duplicates."

        lines = [f"{self.defect_count} problem(s) in the fault network:"]
        for label, items in (
            ("crossing without a shared node", self.crossing_pairs),
            ("dangling end", self.dangles),
            ("duplicate trace", self.duplicates),
            ("zero-length trace", self.zero_length),
            ("self-intersection", self.self_intersections),
        ):
            for item in items:
                where = item.get("at")
                place = f" at ({where[0]:,.0f}, {where[1]:,.0f})" if where else ""
                names = item.get("names") or ([item["name"]] if item.get("name") else [])
                who = f" — {' and '.join(str(n) for n in names)}" if names else ""
                lines.append(f"  {label}{place}{who}")
        return "\n".join(lines)


def validate_network(constraints: list[Constraint], snap_tolerance: float) -> ValidationReport:
    """Identify problems that prevent triangulation. Modifies nothing.

    `snap_tolerance` is in analysis-CRS units. A reasonable default is half the
    median control-point spacing: closer than that and two ends are the same
    node as far as the data can tell.

    Cleaning is a separate, explicit step so the geologist sees what changed
    (§4's design rule).
    """
    if snap_tolerance <= 0:
        raise DegenerateInput(
            f"Snap tolerance must be positive; got {snap_tolerance:g}. A "
            f"reasonable default is half the median control-point spacing."
        )

    report = ValidationReport()

    for index, constraint in enumerate(constraints):
        line = constraint.geometry
        name = constraint.name or f"constraint {index}"

        if line.is_empty or len(line.coords) < 2 or line.length <= 0:
            report.zero_length.append(
                {
                    "index": index,
                    "name": name,
                    "at": tuple(line.coords[0]) if len(line.coords) else None,
                }
            )
            continue

        # `is_simple` is false for a line that touches itself. Splitting it is
        # a cleaning step; here it is only reported.
        if not line.is_simple:
            crossing = _self_intersection_point(line)
            report.self_intersections.append({"index": index, "name": name, "at": crossing})

    usable = [
        (index, constraint)
        for index, constraint in enumerate(constraints)
        if not constraint.geometry.is_empty and len(constraint.geometry.coords) >= 2
    ]

    for position, (index_a, a) in enumerate(usable):
        for index_b, b in usable[position + 1 :]:
            if a.geometry.equals(b.geometry):
                report.duplicates.append(
                    {
                        "indices": [index_a, index_b],
                        "names": [
                            a.name or f"constraint {index_a}",
                            b.name or f"constraint {index_b}",
                        ],
                        "at": tuple(a.geometry.coords[0]),
                    }
                )
                continue

            if not a.geometry.intersects(b.geometry):
                continue

            # A crossing is a problem only when the traces cross *without* a
            # shared node. Two faults meeting end-to-end at a node are an
            # ordinary Y junction, and reporting it would bury the real ones.
            intersection = a.geometry.intersection(b.geometry)
            for point in _points_of(intersection):
                if _is_shared_node(a.geometry, b.geometry, point, snap_tolerance):
                    continue
                report.crossing_pairs.append(
                    {
                        "indices": [index_a, index_b],
                        "names": [
                            a.name or f"constraint {index_a}",
                            b.name or f"constraint {index_b}",
                        ],
                        "at": (point.x, point.y),
                    }
                )

    report.dangles.extend(_find_dangles(usable, snap_tolerance))
    return report


def _find_dangles(
    usable: list[tuple[int, Constraint]], snap_tolerance: float
) -> list[dict[str, Any]]:
    """Ends that nearly touch another trace but do not.

    Only *near* misses are reported. An end far from everything is a fault
    tipping out, which is ordinary geology — reporting it would make the report
    useless by filling it with things that are correct.
    """
    dangles: list[dict[str, Any]] = []

    for index, constraint in usable:
        for which, coordinate in (("start", 0), ("end", -1)):
            end = Point(constraint.geometry.coords[coordinate])
            for other_index, other in usable:
                if other_index == index:
                    continue
                distance = end.distance(other.geometry)
                # Zero means it already touches; beyond tolerance it is a tip.
                if 0 < distance <= snap_tolerance:
                    dangles.append(
                        {
                            "index": index,
                            "name": constraint.name or f"constraint {index}",
                            "end": which,
                            "at": (end.x, end.y),
                            "gap": float(distance),
                            "near": other.name or f"constraint {other_index}",
                        }
                    )
                    break

    return dangles


def clean_network(
    constraints: list[Constraint],
    report: ValidationReport,
    snap_tolerance: float,
) -> tuple[list[Constraint], list[str]]:
    """Apply fixes. Returns cleaned constraints and a human-readable changelog.

    In order:

    1. Drop zero-length segments.
    2. Split self-intersecting lines at their crossings.
    3. Insert shared nodes at crossings between different faults.
    4. Snap dangling ends within tolerance to the nearest fault.
    5. Merge exact duplicates.

    **Dangles beyond tolerance are not extended.** An unresolved dangle means
    the compartment is open there, which may be geologically correct. It is
    reported and left for the geologist to decide — closing it would change
    which wells are believed to be in communication.

    The changelog is not decoration. Every entry is a change someone did not
    ask for, and the rule against silent repair is only honoured if they can
    read what happened.
    """
    changelog: list[str] = []
    working: list[Constraint] = []

    dropped = {item["index"] for item in report.zero_length}
    for index, constraint in enumerate(constraints):
        if index in dropped:
            changelog.append(
                f"Dropped zero-length {constraint.kind.value} '{constraint.name or index}'."
            )
            continue
        working.append(constraint)

    working, split_log = _split_self_intersections(working)
    changelog.extend(split_log)

    working, duplicate_log = _merge_duplicates(working)
    changelog.extend(duplicate_log)

    working, snap_log = _snap_dangles(working, snap_tolerance)
    changelog.extend(snap_log)

    # Crossings are noded last: splitting and snapping both create new
    # geometry that can itself cross something.
    working, node_log = _node_crossings(working, snap_tolerance)
    changelog.extend(node_log)

    return working, changelog


def _split_self_intersections(
    constraints: list[Constraint],
) -> tuple[list[Constraint], list[str]]:
    """A line that crosses itself becomes several that do not."""
    result: list[Constraint] = []
    changelog: list[str] = []

    for constraint in constraints:
        line = constraint.geometry
        if line.is_simple:
            result.append(constraint)
            continue

        # unary_union on a self-intersecting line splits it at its crossings.
        pieces = unary_union(line)
        # `unary_union` is typed as returning any geometry; on a self-crossing
        # LineString it yields LineStrings, and anything else means the input
        # was not what this function accepts.
        candidates = list(pieces.geoms) if isinstance(pieces, MultiLineString) else [pieces]
        parts = [part for part in candidates if isinstance(part, LineString)]
        name = constraint.name or "unnamed"
        changelog.append(
            f"Split self-intersecting {constraint.kind.value} '{name}' into "
            f"{len(parts)} pieces."
        )
        for number, part in enumerate(parts, start=1):
            if part.length <= 0:
                continue
            # **A split breakline loses its Z, and becomes a fault.**
            #
            # The split points have no elevation of their own, and interpolating
            # one would invent data — a value that looks measured and is not.
            # Demoting to a fault and saying so in the name is the honest
            # outcome: it is visibly wrong in the layer list, which is what
            # makes someone re-pick it rather than ship it.
            if constraint.kind is ConstraintKind.BREAKLINE:
                result.append(
                    Constraint(
                        geometry=part,
                        kind=ConstraintKind.FAULT,
                        name=f"{name} ({number}) — Z lost in split, re-pick",
                    )
                )
            else:
                result.append(
                    Constraint(
                        geometry=part,
                        kind=constraint.kind,
                        name=f"{name} ({number})",
                    )
                )

    return result, changelog


def _merge_duplicates(
    constraints: list[Constraint],
) -> tuple[list[Constraint], list[str]]:
    result: list[Constraint] = []
    changelog: list[str] = []

    for constraint in constraints:
        if any(constraint.geometry.equals(kept.geometry) for kept in result):
            changelog.append(
                f"Merged duplicate {constraint.kind.value} '{constraint.name or 'unnamed'}'."
            )
            continue
        result.append(constraint)

    return result, changelog


def _snap_dangles(
    constraints: list[Constraint], snap_tolerance: float
) -> tuple[list[Constraint], list[str]]:
    """Move a near-miss end onto the trace it nearly touches.

    Only within tolerance. Beyond it the gap is a fault tip, and closing one
    changes which wells are believed to be in communication.
    """
    result = list(constraints)
    changelog: list[str] = []

    for index, constraint in enumerate(result):
        coords = list(constraint.geometry.coords)
        changed = False

        for position in (0, -1):
            end = Point(coords[position])
            best: tuple[float, Any] | None = None
            for other_index, other in enumerate(result):
                if other_index == index:
                    continue
                distance = end.distance(other.geometry)
                if 0 < distance <= snap_tolerance and (best is None or distance < best[0]):
                    best = (distance, other.geometry)

            if best is None:
                continue

            from shapely.ops import nearest_points

            _, target = nearest_points(end, best[1])
            coords[position] = (target.x, target.y)
            changed = True
            changelog.append(
                f"Snapped {'start' if position == 0 else 'end'} of "
                f"'{constraint.name or index}' by {best[0]:,.1f} units to meet "
                f"an adjacent trace."
            )

        if changed:
            result[index] = Constraint(
                geometry=LineString(coords),
                kind=constraint.kind,
                name=constraint.name,
                z_values=constraint.z_values,
            )

    return result, changelog


def _node_crossings(
    constraints: list[Constraint], snap_tolerance: float
) -> tuple[list[Constraint], list[str]]:
    """Split traces at crossings so they share a node.

    Triangulation needs shared nodes: two segments that cross without one
    produce overlapping triangles, and the mesh's adjacency then says two
    compartments are connected when the map shows a fault between them.
    """
    result: list[Constraint] = []
    changelog: list[str] = []

    for index, constraint in enumerate(constraints):
        crossings: list[Point] = []
        for other_index, other in enumerate(constraints):
            if other_index == index:
                continue
            if not constraint.geometry.intersects(other.geometry):
                continue
            intersection = constraint.geometry.intersection(other.geometry)
            for point in _points_of(intersection):
                if _is_shared_node(constraint.geometry, other.geometry, point, snap_tolerance):
                    continue
                crossings.append(point)

        if not crossings:
            result.append(constraint)
            continue

        pieces = _split_at(constraint.geometry, crossings)
        name = constraint.name or f"constraint {index}"
        changelog.append(
            f"Noded '{name}' at {len(crossings)} crossing(s), producing "
            f"{len(pieces)} segment(s)."
        )
        for number, piece in enumerate(pieces, start=1):
            result.append(
                Constraint(
                    geometry=piece,
                    kind=constraint.kind,
                    name=f"{name} ({number})" if len(pieces) > 1 else name,
                )
            )

    return result, changelog


def _split_at(line: LineString, points: list[Point]) -> list[LineString]:
    """Split `line` at each point, keeping vertex order."""
    from shapely.ops import split as shapely_split

    pieces: list[LineString] = [line]
    for point in points:
        next_pieces: list[LineString] = []
        for piece in pieces:
            if piece.distance(point) > 1e-9:
                next_pieces.append(piece)
                continue
            parts = shapely_split(piece, point)
            next_pieces.extend(
                part for part in parts.geoms if isinstance(part, LineString) and part.length > 0
            )
        pieces = next_pieces
    return pieces


def _points_of(geometry: Any) -> list[Point]:
    """Every point in an intersection result, whatever its type."""
    if geometry.is_empty:
        return []
    if isinstance(geometry, Point):
        return [geometry]
    if hasattr(geometry, "geoms"):
        points: list[Point] = []
        for part in geometry.geoms:
            points.extend(_points_of(part))
        return points
    # A LineString intersection means the two traces are collinear over a
    # stretch. Its ends are the nodes that matter.
    if isinstance(geometry, LineString) and len(geometry.coords) >= 2:
        return [Point(geometry.coords[0]), Point(geometry.coords[-1])]
    return []


def _is_shared_node(a: LineString, b: LineString, point: Point, tolerance: float) -> bool:
    """Whether `point` is an endpoint of both — an ordinary Y junction.

    Two faults meeting end-to-end at a node are correct geology. Reporting them
    as crossings would bury the real problems under the normal ones.
    """
    return _is_endpoint(a, point, tolerance) and _is_endpoint(b, point, tolerance)


def _is_endpoint(line: LineString, point: Point, tolerance: float) -> bool:
    coords = list(line.coords)
    return any(Point(coords[position]).distance(point) <= tolerance for position in (0, -1))


def _self_intersection_point(line: LineString) -> tuple[float, float] | None:
    """Where a line crosses itself, for the report's location field."""
    coords = list(line.coords)
    for i in range(len(coords) - 1):
        first = LineString([coords[i], coords[i + 1]])
        # Skip the adjacent segment: it shares a vertex by construction.
        for j in range(i + 2, len(coords) - 1):
            second = LineString([coords[j], coords[j + 1]])
            if first.intersects(second):
                where = first.intersection(second)
                points = _points_of(where)
                if points:
                    return (points[0].x, points[0].y)
    return None


__all__ = [
    "Constraint",
    "ConstraintKind",
    "ValidationReport",
    "clean_network",
    "validate_network",
]
