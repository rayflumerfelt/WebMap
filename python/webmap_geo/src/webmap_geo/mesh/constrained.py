"""Constrained Delaunay triangulation. `05-geoprocessing.md` §5.

The mesh exists for one property, and everything else here serves it:
**constrained edges are guaranteed present in the output triangulation**, so
no triangle spans a fault, and therefore no mesh-based operation can
interpolate across one.

That is a stronger guarantee than the grid rasterisation in `faults.raster`
gives. Rasterising blocks *links between cell centres*, which is exactly right
for a finite-difference stencil and says nothing about distances between
arbitrary points. Barrier-aware neighbour search needs the second thing: how
far apart are these two wells if you cannot walk through the fault. That is a
path length on this mesh.

Shewchuk's `triangle` does the work. The flags are `p` for a planar straight
line graph — which is what makes the segments constraints rather than hints —
plus `q` for quality and `a` for a size cap.

**`05` §5's `edge_is_blocked(v0, v1)` cannot work as specified, and this
module does something different.** In a constrained Delaunay triangulation no
edge ever crosses a constrained edge — that is the guarantee `p` provides — so
a predicate for "traversal between these vertices crosses a hard fault" is
never true of any edge in the mesh. Paths cross a fault through the fault's
*own vertices*, which the triangles on both sides share. Measured on a
60-point domain with one sealing fault: 38 of the 40 vertices lying on the
fault were adjacent to both sides, so edge blocking stopped nothing at all.

The fix is the standard one for cracks in a mesh: split every vertex on a hard
fault into one copy per side, so the two sides share no vertex and the
separation is topological. Nothing downstream has to remember to check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

from webmap_geo.exceptions import DegenerateInput
from webmap_geo.faults.network import Constraint, ConstraintKind
from webmap_geo.frame import AnalysisFrame

#: Minimum interior angle Shewchuk's refinement aims for. 30 degrees is the
#: highest value the algorithm is proven to terminate at; above it, refinement
#: can loop forever on a sharp input corner — and fault traces meeting at an
#: acute angle are exactly that.
MIN_ANGLE_DEGREES = 30.0

#: Segment kinds, as `05` §5 defines them.
FAULT_SEGMENT = 0
BREAKLINE_SEGMENT = 1


@dataclass(frozen=True)
class ConstrainedMesh:
    """A triangulation whose edges respect the constraint geometry."""

    vertices: NDArray[np.float64]
    triangles: NDArray[np.int32]
    segments: NDArray[np.int32]
    #: 0 = fault (hard), 1 = breakline (soft).
    segment_kind: NDArray[np.int32]
    frame: AnalysisFrame
    #: Known values at vertices, NaN where unknown. Control points carry
    #: theirs; constraint and boundary vertices do not.
    vertex_z: NDArray[np.float64] | None = None

    #: Vertices duplicated by the fault split — the copies that sit at the
    #: same coordinate on opposite sides of a sealing fault. Reported because
    #: it is how a caller can tell a split mesh from an unsplit one.
    split_vertices: frozenset[int] = field(default=frozenset(), repr=False)

    @property
    def n_vertices(self) -> int:
        return len(self.vertices)

    def adjacency(self) -> dict[int, list[int]]:
        """Vertex -> neighbouring vertices.

        **The separation is topological, not a filter.** `build_mesh` splits
        every vertex that lies on a hard fault into one copy per side, so the
        two sides of a sealing fault share no vertex and nothing here has to
        remember to exclude anything.

        Travel *along* a fault, on one side of it, is ordinary and permitted —
        which is why this cannot be done by blocking the fault's own edges.
        """
        neighbours: dict[int, set[int]] = {i: set() for i in range(self.n_vertices)}
        for a, b, c in self.triangles:
            for u, v in ((a, b), (b, c), (c, a)):
                neighbours[int(u)].add(int(v))
                neighbours[int(v)].add(int(u))
        return {vertex: sorted(others) for vertex, others in neighbours.items()}

    def describe(self) -> str:
        faults = int((self.segment_kind == FAULT_SEGMENT).sum())
        return (
            f"{len(self.triangles):,} triangles over {self.n_vertices:,} vertices, "
            f"{faults:,} constrained fault edges, {self.frame.describe()}"
        )


def build_mesh(
    points: NDArray[np.floating],
    constraints: list[Constraint],
    bbox: tuple[float, float, float, float],
    frame: AnalysisFrame,
    *,
    max_area: float | None = None,
    values: NDArray[np.floating] | None = None,
) -> ConstrainedMesh:
    """Constrained Delaunay triangulation with faults as constrained edges.

    `max_area` caps triangle size so the mesh resolves the output grid; `05`
    §5 puts it at roughly `2 * cell_size**2`. Omitting it produces a mesh too
    coarse to sample accurately — the triangulation is then only as fine as
    the control spacing, which in a sparse area is thousands of feet.

    `bbox` bounds the domain. Without it the triangulation is the convex hull
    of the input, and a fault that runs off the edge of the data would leave
    the region beyond its tip untriangulated rather than separated.
    """
    import triangle as shewchuk

    coords = np.asarray(points, dtype=np.float64)
    if coords.ndim != 2 or coords.shape[1] != 2:
        raise DegenerateInput(
            f"Mesh points must be an (n, 2) array in {frame.describe()}; got "
            f"shape {coords.shape}."
        )

    vertices, segments, kinds = _assemble_pslg(coords, constraints, bbox)

    if len(vertices) < 3:
        raise DegenerateInput(
            f"A triangulation needs at least 3 distinct vertices; got "
            f"{len(vertices)}. Check that the control points are not all "
            f"coincident."
        )

    flags = f"pq{MIN_ANGLE_DEGREES:g}"
    if max_area is not None:
        if max_area <= 0:
            raise DegenerateInput(
                f"max_area must be positive; got {max_area:g}. It caps triangle "
                f"size so the mesh resolves the grid — roughly twice the cell "
                f"area."
            )
        flags += f"a{max_area:.6g}"

    payload: dict[str, Any] = {"vertices": vertices}
    if len(segments):
        payload["segments"] = segments
        # Offset by one so that 0 keeps its meaning of "unmarked" inside
        # triangle. These come back on the refined output, which is the only
        # reliable way to know which kind a split segment came from.
        payload["segment_markers"] = (kinds + 1).reshape(-1, 1)

    result = shewchuk.triangulate(payload, flags)

    if "triangles" not in result:
        raise DegenerateInput(
            "Triangulation produced no triangles. The input is degenerate — "
            "usually every point collinear, or a bounding box with no area."
        )

    mesh_vertices = np.asarray(result["vertices"], dtype=np.float64)
    mesh_triangles = np.asarray(result["triangles"], dtype=np.int32)
    mesh_segments, mesh_kinds = _recover_segments(
        result, mesh_vertices, segments, kinds, vertices
    )

    # The barrier is made here. Until this runs the fault is drawn on the mesh
    # and blocks nothing, because a path crosses it through its own vertices.
    mesh_vertices, mesh_triangles, split = _split_fault_vertices(
        mesh_vertices, mesh_triangles, mesh_segments, mesh_kinds
    )

    return ConstrainedMesh(
        vertices=mesh_vertices,
        triangles=mesh_triangles,
        segments=mesh_segments,
        segment_kind=mesh_kinds,
        frame=frame,
        vertex_z=_place_values(mesh_vertices, coords, values),
        split_vertices=split,
    )


def _clip_to_bbox(
    geometry: Any, bbox: tuple[float, float, float, float]
) -> list[NDArray[np.float64]]:
    """The parts of a constraint that lie inside the domain.

    Returns a list because clipping can split one trace into several: a fault
    that leaves the box and re-enters is two constraints inside it, and joining
    them across the gap would draw a barrier where the data says there is none.
    """
    from shapely.geometry import box

    clipped = geometry.intersection(box(*bbox))
    if clipped.is_empty:
        return []

    parts = getattr(clipped, "geoms", [clipped])
    pieces: list[NDArray[np.float64]] = []
    for part in parts:
        # A trace touching the boundary at a single point clips to a Point,
        # which constrains nothing.
        if part.geom_type == "LineString" and len(part.coords) >= 2:
            pieces.append(np.asarray(part.coords, dtype=np.float64))
    return pieces


def _assemble_pslg(
    points: NDArray[np.float64],
    constraints: list[Constraint],
    bbox: tuple[float, float, float, float],
) -> tuple[NDArray[np.float64], NDArray[np.int32], NDArray[np.int32]]:
    """Vertices, constrained segments, and each segment's kind.

    Vertices are deduplicated to a tolerance-free exact match. Shewchuk's
    `triangle` rejects duplicated input vertices outright rather than merging
    them, and a fault trace that starts where a well sits is not unusual.
    """
    xmin, ymin, xmax, ymax = bbox
    if not (xmax > xmin and ymax > ymin):
        raise DegenerateInput(
            f"Mesh bounds are empty or inverted: ({xmin:g}, {ymin:g}) to "
            f"({xmax:g}, {ymax:g}). Order is (xmin, ymin, xmax, ymax)."
        )

    index: dict[tuple[float, float], int] = {}
    vertices: list[tuple[float, float]] = []

    def vertex_of(x: float, y: float) -> int:
        key = (float(x), float(y))
        found = index.get(key)
        if found is None:
            found = len(vertices)
            index[key] = found
            vertices.append(key)
        return found

    for x, y in points:
        vertex_of(x, y)

    segments: list[tuple[int, int]] = []
    kinds: list[int] = []

    for constraint in constraints:
        kind = FAULT_SEGMENT if constraint.kind is ConstraintKind.FAULT else BREAKLINE_SEGMENT
        # **Clipped to the domain.** Fault traces legitimately run past the
        # area of interest, and `triangle` keeps their outside vertices while
        # triangulating nothing around them — leaving isolated vertices that
        # then read as one-vertex fault compartments. Clipping preserves
        # sealing: the trace still meets the boundary, at the boundary.
        for coords in _clip_to_bbox(constraint.geometry, bbox):
            previous = vertex_of(coords[0][0], coords[0][1])
            for x, y in coords[1:]:
                current = vertex_of(x, y)
                # A zero-length segment is not a constraint and `triangle`
                # treats it as an error rather than as a no-op.
                if current != previous:
                    segments.append((previous, current))
                    kinds.append(kind)
                previous = current

    # The bounding box last, as four segments. Its corners are ordinary
    # vertices, so a constraint that reaches the boundary shares a node with
    # it rather than crossing it.
    corners = [
        vertex_of(xmin, ymin),
        vertex_of(xmax, ymin),
        vertex_of(xmax, ymax),
        vertex_of(xmin, ymax),
    ]
    for i in range(4):
        segments.append((corners[i], corners[(i + 1) % 4]))
        # The domain edge is not a fault: marking it one would make every
        # boundary vertex a barrier and cut the mesh off from itself.
        kinds.append(BREAKLINE_SEGMENT)

    return (
        np.asarray(vertices, dtype=np.float64),
        np.asarray(segments, dtype=np.int32).reshape(-1, 2),
        np.asarray(kinds, dtype=np.int32),
    )


def _recover_segments(
    result: dict[str, Any],
    mesh_vertices: NDArray[np.float64],
    input_segments: NDArray[np.int32],
    input_kinds: NDArray[np.int32],
    input_vertices: NDArray[np.float64],
) -> tuple[NDArray[np.int32], NDArray[np.int32]]:
    """Map the output's segments back to the kinds they were given.

    **Refinement splits constrained segments**, and the split pieces come back
    with new vertex indices that do not correspond to the input's. `triangle`
    reports `segmentmarkers` when markers were supplied, which is the reliable
    channel — recovering the kind by comparing coordinates would misattribute
    a breakline that happens to run along a fault.
    """
    segments = np.asarray(result.get("segments", []), dtype=np.int32).reshape(-1, 2)
    markers = result.get("segment_markers")

    if markers is not None and len(markers) == len(segments):
        # Markers are 1-based in the payload below, so that 0 keeps its
        # meaning of "unmarked" inside triangle.
        kinds = np.asarray(markers, dtype=np.int32).ravel() - 1
        kinds[kinds < 0] = BREAKLINE_SEGMENT
        return segments, kinds

    # Without markers, fall back to matching endpoints against the input
    # segments' geometry. Correct for an unrefined mesh and conservative
    # otherwise: an unmatched segment is treated as a breakline, so a fault is
    # never invented where there was none.
    lookup = {
        tuple(np.round(input_vertices[a], 9)) + tuple(np.round(input_vertices[b], 9)): kind
        for (a, b), kind in zip(input_segments, input_kinds, strict=True)
    }
    kinds = np.full(len(segments), BREAKLINE_SEGMENT, dtype=np.int32)
    for i, (a, b) in enumerate(segments):
        key = tuple(np.round(mesh_vertices[a], 9)) + tuple(np.round(mesh_vertices[b], 9))
        reverse = tuple(np.round(mesh_vertices[b], 9)) + tuple(np.round(mesh_vertices[a], 9))
        kinds[i] = lookup.get(key, lookup.get(reverse, BREAKLINE_SEGMENT))
    return segments, kinds


def _split_fault_vertices(
    vertices: NDArray[np.float64],
    triangles: NDArray[np.int32],
    segments: NDArray[np.int32],
    kinds: NDArray[np.int32],
) -> tuple[NDArray[np.float64], NDArray[np.int32], frozenset[int]]:
    """Duplicate every vertex on a hard fault, once per side.

    A vertex lying on a sealing fault is shared by the triangles on both sides
    of it, so a path can step across the fault through that vertex without ever
    traversing a constrained edge. Splitting it into one copy per fan of
    triangles disconnects the sides in the graph itself.

    The fans are the connected components of "incident triangles joined by an
    edge through this vertex that is not a fault". A fault vertex in the middle
    of a trace has two fans; one at a fault *tip* has a single fan, which is
    exactly right — a tip is where the two sides genuinely do connect.
    """
    fault_edges = {
        frozenset((int(a), int(b)))
        for (a, b), kind in zip(segments, kinds, strict=True)
        if kind == FAULT_SEGMENT
    }
    if not fault_edges:
        return vertices, triangles, frozenset()

    fault_vertices = {vertex for edge in fault_edges for vertex in edge}

    incident: dict[int, list[int]] = {vertex: [] for vertex in fault_vertices}
    for index, triangle in enumerate(triangles):
        for corner in triangle:
            if int(corner) in incident:
                incident[int(corner)].append(index)

    new_vertices = [tuple(point) for point in vertices]
    new_triangles = triangles.copy()
    split: set[int] = set()

    for vertex, incident_triangles in incident.items():
        fans = _fans_around(vertex, incident_triangles, new_triangles, fault_edges)
        # One fan means the sides already meet here — a fault tip, or a vertex
        # the fault only touches. Nothing to separate.
        for fan in fans[1:]:
            copy = len(new_vertices)
            new_vertices.append(tuple(vertices[vertex]))
            split.add(copy)
            for index in fan:
                new_triangles[index][new_triangles[index] == vertex] = copy

    return (
        np.asarray(new_vertices, dtype=np.float64),
        new_triangles,
        frozenset(split),
    )


def _fans_around(
    vertex: int,
    incident_triangles: list[int],
    triangles: NDArray[np.int32],
    fault_edges: set[frozenset[int]],
) -> list[list[int]]:
    """Group a vertex's incident triangles into fans separated by faults.

    Two incident triangles belong to the same fan when they share an edge
    through this vertex that is not part of a hard fault — that is, when you
    can rotate from one to the other without stepping over the fault.
    """
    # Which triangles meet at each edge (vertex, other).
    by_edge: dict[int, list[int]] = {}
    for index in incident_triangles:
        for corner in triangles[index]:
            other = int(corner)
            if other != vertex:
                by_edge.setdefault(other, []).append(index)

    parent = {index: index for index in incident_triangles}

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for other, sharing in by_edge.items():
        if frozenset((vertex, other)) in fault_edges:
            continue
        first = sharing[0]
        for index in sharing[1:]:
            parent[find(index)] = find(first)

    grouped: dict[int, list[int]] = {}
    for index in incident_triangles:
        grouped.setdefault(find(index), []).append(index)
    return list(grouped.values())


def _place_values(
    mesh_vertices: NDArray[np.float64],
    points: NDArray[np.float64],
    values: NDArray[np.floating] | None,
) -> NDArray[np.float64] | None:
    """Attach known values to the mesh vertices that are control points.

    Matched by exact coordinate, because the control points were inserted as
    vertices verbatim — `triangle` adds Steiner points but never moves an
    input one. A tolerance here would risk attaching a well's value to a
    refinement vertex a few feet away.
    """
    if values is None:
        return None

    z = np.full(len(mesh_vertices), np.nan, dtype=np.float64)
    index = {(float(x), float(y)): i for i, (x, y) in enumerate(mesh_vertices)}
    for (x, y), value in zip(points, np.asarray(values, dtype=float), strict=True):
        found = index.get((float(x), float(y)))
        if found is not None:
            z[found] = value
    return z


__all__ = [
    "BREAKLINE_SEGMENT",
    "FAULT_SEGMENT",
    "MIN_ANGLE_DEGREES",
    "ConstrainedMesh",
    "build_mesh",
]
