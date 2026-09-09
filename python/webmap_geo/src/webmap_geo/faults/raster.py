"""Rasterising fault traces onto grid edges. `05-geoprocessing.md` §6.1.

"The fault mask is computed once by rasterizing constraint geometries onto the
grid edges — an edge between two cells is blocked if a hard fault segment
crosses it."

This is the join between geology and arithmetic: a fault is a polyline in
analysis-CRS coordinates, and the solver needs to know which of its cell
boundaries that polyline crosses. Everything about fault-aware gridding depends
on this being right, and it is the kind of code where an off-by-one produces a
map that is subtly wrong rather than obviously broken — the fault appears half
a cell from where it was drawn, and a well ends up on the wrong side of it.

**Only hard constraints block.** A breakline is continuous in value and
discontinuous only in gradient (`CLAUDE.md` §13), so blocking an edge for one
would cut a surface that should not be cut.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray
from shapely.geometry import LineString, Point

from webmap_geo.faults.network import Constraint
from webmap_geo.grid import GridDefinition


def blocked_edges(
    constraints: list[Constraint], grid: GridDefinition
) -> tuple[NDArray[np.bool_], NDArray[np.bool_]]:
    """Which cell-to-cell links a hard fault severs.

    Returns `(vertical, horizontal)`:

    - `vertical[r, c]` — the link between cell (r, c) and (r+1, c), the
      north-south one. Shape (ny-1, nx).
    - `horizontal[r, c]` — the link between (r, c) and (r, c+1), the east-west
      one. Shape (ny, nx-1).

    The names describe **which links they block**, not the direction the fault
    runs: a north-south fault blocks east-west links. Getting that backwards
    makes the surface step across the fault's strike instead of along it, which
    looks like structure and is an indexing error.

    **A link is blocked when the segment joining the two cell centres crosses
    the fault.** That is the physical question — can the surface propagate from
    this cell to that one without crossing the barrier — and it is not the same
    as asking which cell *boundaries* the fault crosses. A fault running
    exactly along a column of cell centres crosses no boundary at all; it
    passes through the cells, and a boundary-crossing test finds nothing while
    the fault plainly separates the columns either side. That was the first
    implementation here, and it silently blocked nothing.

    Only hard constraints block. A breakline is continuous in value and
    discontinuous only in gradient (`CLAUDE.md` §13), so blocking a link for
    one would cut a surface that should not be cut.
    """
    vertical = np.zeros((grid.ny - 1, grid.nx), dtype=bool)
    horizontal = np.zeros((grid.ny, grid.nx - 1), dtype=bool)

    hard = [c for c in constraints if c.is_hard and not c.geometry.is_empty]
    if not hard:
        return vertical, horizontal

    for constraint in hard:
        for start_xy, end_xy in _segments(constraint.geometry):
            _mark_segment(start_xy, end_xy, grid, vertical, horizontal)

    return vertical, horizontal


def _segments(line: LineString) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    coords = list(line.coords)
    return [
        ((coords[i][0], coords[i][1]), (coords[i + 1][0], coords[i + 1][1]))
        for i in range(len(coords) - 1)
    ]


def _mark_segment(
    start: tuple[float, float],
    end: tuple[float, float],
    grid: GridDefinition,
    vertical: NDArray[np.bool_],
    horizontal: NDArray[np.bool_],
) -> None:
    """Block every link whose centre-to-centre path this segment crosses.

    Only links near the segment are examined — its bounding box grown by one
    cell — so the cost is proportional to the fault's length rather than to the
    grid's area. On a 1000x1000 grid an exhaustive test would be two million
    intersections per segment.
    """
    segment = LineString([start, end])
    # A fault passing exactly through a cell centre is geometrically ambiguous:
    # it separates neither pair cleanly, and testing plain intersection blocks
    # *both* adjacent links plus every link collinear with it — which chopped a
    # 21x21 grid into 23 compartments instead of 2.
    #
    # The convention: a link is blocked when the fault meets it anywhere except
    # at its **first** centre. A fault through centre c then blocks the link
    # arriving from c-1 and leaves the one departing to c+1 open, so the cell
    # on the fault joins the side ahead of it. Deterministic, and it puts every
    # on-fault cell on the same side rather than isolating it.
    touch = grid.cell_size * 1e-9

    lo_x, hi_x = sorted((start[0], end[0]))
    lo_y, hi_y = sorted((start[1], end[1]))

    # Cell index range covering the segment, grown by one so a link whose
    # centres straddle the segment's end is still considered.
    col_lo = max(0, _col_of(lo_x, grid) - 1)
    col_hi = min(grid.nx - 1, _col_of(hi_x, grid) + 1)
    row_lo = max(0, _row_of(hi_y, grid) - 1)
    row_hi = min(grid.ny - 1, _row_of(lo_y, grid) + 1)

    xs = grid.x_coordinates()
    ys = grid.y_coordinates()

    # East-west links: centre (r, c) to centre (r, c+1).
    for row in range(row_lo, row_hi + 1):
        y = float(ys[row])
        for col in range(col_lo, min(col_hi, grid.nx - 2) + 1):
            if horizontal[row, col]:
                continue
            first = (float(xs[col]), y)
            link = LineString([first, (float(xs[col + 1]), y)])
            if link.intersects(segment) and segment.distance(Point(first)) > touch:
                horizontal[row, col] = True

    # North-south links: centre (r, c) to centre (r+1, c).
    for row in range(row_lo, min(row_hi, grid.ny - 2) + 1):
        y0, y1 = float(ys[row]), float(ys[row + 1])
        for col in range(col_lo, col_hi + 1):
            if vertical[row, col]:
                continue
            x = float(xs[col])
            first = (x, y0)
            link = LineString([first, (x, y1)])
            if link.intersects(segment) and segment.distance(Point(first)) > touch:
                vertical[row, col] = True


def _col_of(x: float, grid: GridDefinition) -> int:
    return int(np.floor((x - grid.xmin) / grid.cell_size))


def _row_of(y: float, grid: GridDefinition) -> int:
    return int(np.floor((grid.ymax - y) / grid.cell_size))


def compartments(
    blocked: tuple[NDArray[np.bool_], NDArray[np.bool_]], grid: GridDefinition
) -> tuple[NDArray[np.int64], int]:
    """Label each cell with the fault compartment it belongs to.

    A compartment is a region bounded by faults (`CLAUDE.md` §13) — cells
    reachable from one another without crossing a hard constraint. Returned as
    an (ny, nx) label array and a count.

    `05` §6.5 requires per-compartment control-point counts in the diagnostics,
    and this is what makes them computable: a compartment holding no control at
    all is entirely extrapolated, however good the surface looks there.
    """
    import scipy.sparse as sp
    import scipy.sparse.csgraph as csgraph

    vertical, horizontal = blocked
    index = np.arange(grid.n_cells).reshape(grid.ny, grid.nx)

    rows: list[int] = []
    cols: list[int] = []

    for row in range(grid.ny):
        for col in range(grid.nx - 1):
            if not horizontal[row, col]:
                rows.append(int(index[row, col]))
                cols.append(int(index[row, col + 1]))
    for row in range(grid.ny - 1):
        for col in range(grid.nx):
            if not vertical[row, col]:
                rows.append(int(index[row, col]))
                cols.append(int(index[row + 1, col]))

    adjacency = sp.coo_matrix(
        (np.ones(len(rows)), (rows, cols)), shape=(grid.n_cells, grid.n_cells)
    )
    count, labels = csgraph.connected_components(adjacency, directed=False)
    return labels.reshape(grid.ny, grid.nx).astype(np.int64), int(count)


def control_per_compartment(
    points: NDArray[np.floating],
    labels: NDArray[np.int64],
    grid: GridDefinition,
) -> dict[int, int]:
    """How many control points fall in each compartment.

    The diagnostic `05` §6.5 asks for. A compartment with two wells and a
    compartment with two hundred are drawn with the same colours and the same
    contour interval, and nothing on the map distinguishes them.
    """
    cols = np.floor((points[:, 0] - grid.xmin) / grid.cell_size + 0.5).astype(np.int64)
    rows = np.floor((grid.ymax - points[:, 1]) / grid.cell_size + 0.5).astype(np.int64)

    inside = (cols >= 0) & (cols < grid.nx) & (rows >= 0) & (rows < grid.ny)
    found = labels[rows[inside], cols[inside]]

    unique, counts = np.unique(found, return_counts=True)
    tally = {int(label): int(count) for label, count in zip(unique, counts, strict=True)}

    # Compartments with no control are the ones worth naming, so they are
    # present with a zero rather than absent from the dictionary.
    for label in np.unique(labels):
        tally.setdefault(int(label), 0)
    return tally


__all__ = ["blocked_edges", "compartments", "control_per_compartment"]
