"""Clipping a grid to a polygon, or to its own control. `08` §5.2.

**The clip sets cells to nodata in the grid**, not per tile. Masking each tile
as it is proxied measured at 21 ms median and a pan touches around twenty
tiles; that buys nothing except avoiding a duplicate COG of a few megabytes,
and costs the lineage and the ability to export the clipped surface. Nothing
changes in rendering, because nodata is already transparent.

**Two numbers a clipped grid must recompute or it lies** (`08` §5.2): its
extrapolation fraction, since clipping away an invented corner is a legitimate
way to make a grid honest, and its display range, or the legend spans values no
longer on the map. `clip` returns both, and returning them rather than leaving
them to the caller is deliberate — a caller that forgot would produce a grid
claiming 61% extrapolation after the invention had been removed.

Clipping is also the cleanest answer to extrapolation. `control_boundary`
builds the "clip to the control" preset: the convex or concave hull of the
control points, or everything within the search radius, which removes the
unsupported area rather than warning about it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

from webmap_geo.exceptions import DegenerateInput
from webmap_geo.grid import GridDefinition

#: The percentiles a grid's display range is taken across, matching
#: `gridding.DISPLAY_PERCENTILES`. Not the extremes: minimum curvature
#: overshoots into extrapolated corners and the overshoot is unbounded, so a
#: min/max range gives most of the ramp to a few invented cells (`08` §5.2).
DISPLAY_PERCENTILES = (5.0, 95.0)

#: How the "clip to the control" preset draws its boundary.
ControlBoundary = Literal["convex_hull", "concave_hull", "radius"]


@dataclass(frozen=True)
class ClipResult:
    """A clipped surface and the two numbers that must move with it."""

    surface: NDArray[np.float64]
    #: Fraction of cells that are nodata after the clip. Not the same as the
    #: extrapolation fraction — see `extrapolated_fraction`.
    clipped_fraction: float
    #: P5-P95 of the finite cells that survive, or None if none do.
    display_range: tuple[float, float] | None
    #: `05` §6.5's number, recomputed over the surviving cells.
    extrapolated_fraction: float | None = None


def clip(
    surface: NDArray[np.floating],
    grid: GridDefinition,
    geometry: Any,
    *,
    invert: bool = False,
    control_points: NDArray[np.floating] | None = None,
    search_radius: float | None = None,
) -> ClipResult:
    """Set cells outside `geometry` to NaN, or inside it when `invert`.

    `geometry` is any Shapely polygonal geometry in the grid's own frame — a
    polygon, a multipolygon, or the union of the features somebody selected.
    **No reprojection happens here**: `webmap_geo` entry points take arrays
    already in the analysis frame (`adr/0003`), and a clip against a boundary
    in a different CRS would silently remove the wrong half of the map.

    `invert=True` excludes an area instead of including one — a lease to leave
    out, a no-permit block — which is the same operation with the mask negated
    rather than a second code path.

    Pass `control_points` to have the extrapolation fraction recomputed. It is
    optional because the clip itself does not need them, and left out the
    result simply reports `None` rather than the pre-clip figure, which would
    be wrong in the direction that flatters the grid.
    """
    import shapely

    values = np.asarray(surface, dtype=np.float64)
    if values.shape != (grid.ny, grid.nx):
        raise DegenerateInput(
            f"Surface is {values.shape} but the grid is ({grid.ny}, {grid.nx}). "
            f"They have to be the same grid — a clip against a different one "
            f"would remove cells by index rather than by location."
        )
    if geometry is None or geometry.is_empty:
        raise DegenerateInput(
            "The clip boundary is empty. Clipping to nothing would blank the "
            "whole grid, which is never what anyone means — check that the "
            "polygon layer has features, and that a selection was actually made."
        )
    if not geometry.is_valid:
        # Cleaned rather than refused. Digitised lease outlines self-intersect
        # routinely, and `make_valid` on a polygon is well defined; refusing
        # here would make an ordinary layer unusable for a defect nobody can
        # see on the map.
        geometry = shapely.make_valid(geometry)

    inside = _cells_inside(grid, geometry)
    remove = inside if invert else ~inside

    clipped = values.copy()
    clipped[remove] = np.nan

    finite = clipped[np.isfinite(clipped)]
    if finite.size == 0:
        raise DegenerateInput(
            "The clip removed every cell. The boundary and the grid may be in "
            "different coordinate systems, or `invert` may be the wrong way "
            "round — check that the polygon overlaps "
            f"{_describe_bounds(grid)}."
        )

    low, high = np.percentile(finite, DISPLAY_PERCENTILES)
    return ClipResult(
        surface=clipped,
        clipped_fraction=float(np.isnan(clipped).mean()),
        display_range=(float(low), float(high)),
        extrapolated_fraction=(
            None
            if control_points is None
            else extrapolated_fraction(
                clipped,
                grid,
                np.asarray(control_points, dtype=float),
                search_radius,
                domain=~remove,
            )
        ),
    )


def _cells_inside(grid: GridDefinition, geometry: Any) -> NDArray[np.bool_]:
    """Which cell centres fall inside the boundary.

    **By centre, not by overlap.** A cell is one value at one location, and a
    partially covered cell has no partial value to give; including it because
    a corner is inside would extend the clipped grid half a cell beyond the
    lease line, which on a 250 ft grid is 125 ft of somebody else's acreage.

    **A centre exactly on the boundary counts as inside**, which is why this
    is `intersects_xy` rather than `contains_xy`. `contains` excludes the
    boundary, and a lease outline digitised on round coordinates lands on grid
    lines constantly — excluding those cells carves a one-cell notch along
    every straight edge, and on the map "on the line" reads as in.

    Vectorised, and prepared-geometry-backed, so the whole grid is one call.
    The obvious loop over cells is unusable at a million of them.
    """
    import shapely

    centres = grid.cell_centres()
    return np.asarray(
        shapely.intersects_xy(geometry, centres[:, 0], centres[:, 1]), dtype=bool
    ).reshape(grid.ny, grid.nx)


def extrapolated_fraction(
    surface: NDArray[np.floating],
    grid: GridDefinition,
    control_points: NDArray[np.floating],
    search_radius: float | None = None,
    *,
    domain: NDArray[np.bool_] | None = None,
) -> float:
    """`05` §6.5's number, over the cells that are still on the map.

    The numerator is unchanged — cells with no control point within the search
    radius, plus cells nothing could estimate. **The denominator is what a clip
    changes**, and getting that wrong inverts the meaning of the whole number.

    `domain` is the set of cells that still count. Cells a clip removed are
    excluded from it, because they are not drawn at all: the question the
    fraction answers is "of the map you can see, how much is invention", and a
    cell that is not on the map is not part of either half of it. Counted the
    other way — blanked cells in the numerator, the full grid in the
    denominator — clipping away a wholly invented corner *raises* the
    extrapolation fraction, which is the exact opposite of what `08` §5.2 says
    clipping is for. Measured on the fixture in the tests: 95% before the clip
    and 97% after, for a grid that had just had its invented nine tenths
    removed.

    With no `domain` the whole grid counts and a nodata cell is extrapolated,
    which is the convention the gridding diagnostics use for an unclipped grid.
    """
    from scipy.spatial import cKDTree

    values = np.asarray(surface, dtype=np.float64)
    coords = np.asarray(control_points, dtype=float)
    if coords.ndim != 2 or coords.shape[1] != 2:
        raise DegenerateInput(
            f"Control points must be an (n, 2) array in {grid.frame.describe()}; "
            f"got shape {coords.shape}."
        )

    counts = np.ones((grid.ny, grid.nx), dtype=bool) if domain is None else domain
    if not counts.any():
        raise DegenerateInput(
            "No cells remain to measure. An extrapolation fraction over an empty "
            "map has no meaning; check the clip boundary overlaps the grid."
        )

    radius = search_radius if search_radius is not None else _default_radius(coords)
    if not np.isfinite(radius):
        return float(np.isnan(values[counts]).mean())

    distance, _ = cKDTree(coords).query(grid.cell_centres())
    beyond = distance.reshape(grid.ny, grid.nx) > radius
    return float((beyond | np.isnan(values))[counts].mean())


def _default_radius(coords: NDArray[np.float64]) -> float:
    """Three times the median control spacing, as `05` §6.5 measures it.

    Duplicated from the gridding dispatcher rather than imported, because
    importing it would make `clip` depend on `interpolate` for one number and
    a clip has nothing to do with interpolation. The constant is the one place
    they must agree, and a test asserts they do.
    """
    from scipy.spatial import cKDTree

    if len(coords) < 2:
        return float("inf")
    spacing = cKDTree(coords).query(coords, k=2)[0][:, 1]
    return float(3.0 * np.median(spacing))


def control_boundary(
    control_points: NDArray[np.floating],
    *,
    method: ControlBoundary = "convex_hull",
    search_radius: float | None = None,
    concavity: float = 0.4,
) -> Any:
    """The "clip to the control" preset (`08` §5.2).

    Three shapes, and they answer slightly different questions:

    - **`convex_hull`** — the smallest convex outline containing every pick.
      Predictable and conservative; it keeps the bays between two arms of a
      trend that no well has ever touched.
    - **`concave_hull`** — follows the outline of the control, so those bays
      come out. Better on a linear or L-shaped acquisition, and it has a knob,
      which means it has a wrong setting.
    - **`radius`** — the union of discs of the search radius around every
      point, which is literally the area `05` §6.5 calls supported. Holes
      appear where control is sparse in the middle of the survey, and those
      holes are honest.

    `convex_hull` is the default because it is the one whose result nobody has
    to be told how to read.
    """
    import shapely

    coords = np.asarray(control_points, dtype=float)
    if coords.ndim != 2 or coords.shape[1] != 2:
        raise DegenerateInput(
            f"Control points must be an (n, 2) array; got shape {coords.shape}."
        )
    if len(coords) < 3:
        raise DegenerateInput(
            f"A control boundary needs at least 3 points; got {len(coords)}. "
            f"Two points have no interior to clip to."
        )

    points = shapely.multipoints(coords)

    if method == "convex_hull":
        return shapely.convex_hull(points)
    if method == "concave_hull":
        # `ratio` runs 0 (maximally concave) to 1 (the convex hull). 0.4 keeps
        # genuine embayments while not chasing every gap between two wells
        # into a spike.
        return shapely.concave_hull(points, ratio=concavity)
    if method == "radius":
        radius = search_radius if search_radius is not None else _default_radius(coords)
        if not np.isfinite(radius):
            raise DegenerateInput(
                "A radius boundary needs a search radius, and one cannot be "
                "derived from fewer than two control points. Pass "
                "search_radius explicitly, or use the convex hull."
            )
        return shapely.union_all(shapely.buffer(shapely.points(coords), radius))

    raise DegenerateInput(
        f"'{method}' is not a control-boundary method. Use 'convex_hull' "
        f"(conservative), 'concave_hull' (follows the control outline), or "
        f"'radius' (the supported area, holes included)."
    )


def _describe_bounds(grid: GridDefinition) -> str:
    xmin, ymin, xmax, ymax = grid.bounds
    return (
        f"the grid extent {xmin:,.6g} {ymin:,.6g} to {xmax:,.6g} {ymax:,.6g} "
        f"in {grid.frame.describe()}"
    )


__all__ = [
    "DISPLAY_PERCENTILES",
    "ClipResult",
    "ControlBoundary",
    "clip",
    "control_boundary",
    "extrapolated_fraction",
]
