"""Simple and block kriging. `13-kriging.md` §11.0, §11.1.

Ordinary kriging already exists in `kriging.py` and is the default. These are
the two estimators the rest of `13` needs from it:

**Simple kriging** assumes the mean is known rather than estimating it per
neighbourhood. That sounds like a weaker method and in isolation it is — but it
is what Stage 2 of regression kriging wants, because the trend has already
removed the mean and the residual's is zero by construction. It is also what the
dual-kriging leave-one-out identity of §12.1 needs, which is the diagnostic that
makes cross-validation affordable on a large layer.

**Block kriging** estimates the average over a cell rather than the value at its
centre. For anything reported per unit area — recoverable volume, acreage-weighted
thickness — that is the quantity actually wanted, and the point estimate
systematically overstates its variability. Discretised as a point grid inside the
block, per §11.1, with the covariances averaged.

**A block average of indicators is not an indicator**, so block kriging refuses
them with `ERROR / BLOCK_INDICATOR_UNSUPPORTED`. The average of a 0/1 field over
a block is a proportion, and feeding that back into an indicator CDF as though it
were a threshold exceedance is the kind of error that produces a plausible map
and a wrong volume.

**Singular systems are counted, not announced per node.** §11.1: one
`INFO / SINGULAR_SYSTEM` carrying a count. A flag per node would bury every
other flag on a grid with a few thousand duplicate-adjacent samples.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from webmap_geo.exceptions import DegenerateInput
from webmap_geo.flags import FlagList, raise_flag
from webmap_geo.grid import GridDefinition
from webmap_geo.interpolate.kriging import KrigingResult
from webmap_geo.variogram.model import FittedVariogram, anisotropy_transform

#: Nodes solved per batch. Large enough that the KD-tree query amortises, small
#: enough that the neighbour arrays stay in cache.
BLOCK_SIZE = 512

#: Points per axis discretising a block. §11.1's default. 4×4 is accurate to
#: well under a percent of the block variance for any block smaller than the
#: range, and 8×8 costs four times as much to say the same thing.
BLOCK_DISCRETISATION = 4

#: §11.0's neighbourhood defaults. The maximum matches
#: `InterpolationRequest.n_neighbors` so that two grids of the same data cannot
#: differ for a reason nobody can find.
MIN_NEIGHBOURS = 8
MAX_NEIGHBOURS = 48


@dataclass
class SolveStats:
    """What the solve path did, for the flags. §11.1's count."""

    singular: int = 0
    unestimated: int = 0

    def report(self, flags: FlagList, total: int) -> None:
        if self.singular:
            flags.add(
                "SINGULAR_SYSTEM",
                f"{self.singular:,} of {total:,} kriging systems were singular and "
                f"were solved by least squares. That happens where control points "
                f"are duplicated or very nearly so; the estimate there is the "
                f"best fit rather than an exact interpolation.",
                count=self.singular,
                total=total,
            )


def simple_kriging(
    points: NDArray[np.floating],
    values: NDArray[np.floating],
    grid: GridDefinition,
    variogram: FittedVariogram,
    *,
    mean: float,
    min_neighbours: int = MIN_NEIGHBOURS,
    max_neighbours: int = MAX_NEIGHBOURS,
    max_radius: float | None = None,
    flags: FlagList | None = None,
) -> KrigingResult:
    """Kriging with a known mean.

    **Not a weaker ordinary kriging.** The mean is an input because the caller
    knows it: after a trend fit the residual's mean is zero by construction, and
    estimating it again per neighbourhood would spend degrees of freedom
    re-discovering a zero — and, worse, would let a neighbourhood of unusually
    high residuals shift the local mean and hide the very anomaly the residual
    exists to show.

    Below `min_neighbours` a node is **unestimated rather than extrapolated**
    (§11.0). A mean drawn across a gap looks like data.
    """
    return _krige(
        points,
        values,
        grid,
        variogram,
        mean=mean,
        block=None,
        min_neighbours=min_neighbours,
        max_neighbours=max_neighbours,
        max_radius=max_radius,
        flags=flags,
    )


def block_kriging(
    points: NDArray[np.floating],
    values: NDArray[np.floating],
    grid: GridDefinition,
    variogram: FittedVariogram,
    *,
    mean: float | None = None,
    discretisation: int = BLOCK_DISCRETISATION,
    min_neighbours: int = MIN_NEIGHBOURS,
    max_neighbours: int = MAX_NEIGHBOURS,
    max_radius: float | None = None,
    is_indicator: bool = False,
    flags: FlagList | None = None,
) -> KrigingResult:
    """The average over each cell, rather than the value at its centre.

    For anything reported per unit area this is the quantity actually wanted,
    and the difference is not cosmetic: the block estimate has a *lower*
    variance than the point estimate by the block's own dispersion variance,
    which is exactly the smoothing a volumetric calculation should have and a
    point map should not.

    `mean=None` gives ordinary block kriging (the mean estimated per
    neighbourhood); a mean gives simple block kriging.
    """
    if is_indicator:
        raise_flag(
            "BLOCK_INDICATOR_UNSUPPORTED",
            "Block kriging an indicator would average a 0/1 field into a "
            "proportion, and a proportion is not an exceedance probability — "
            "feeding one back into an indicator CDF produces a plausible map and "
            "a wrong volume. Krige the indicators at points and average the "
            "resulting probabilities, or block-krige the underlying variable.",
        )
    if discretisation < 1:
        raise DegenerateInput(
            f"Block discretisation must be at least 1; got {discretisation}. "
            f"One point per block is point kriging at the cell centre."
        )

    return _krige(
        points,
        values,
        grid,
        variogram,
        mean=mean,
        block=discretisation,
        min_neighbours=min_neighbours,
        max_neighbours=max_neighbours,
        max_radius=max_radius,
        flags=flags,
    )


def _krige(
    points: NDArray[np.floating],
    values: NDArray[np.floating],
    grid: GridDefinition,
    variogram: FittedVariogram,
    *,
    mean: float | None,
    block: int | None,
    min_neighbours: int,
    max_neighbours: int,
    max_radius: float | None,
    flags: FlagList | None,
) -> KrigingResult:
    """The shared assembly and solve path. §11.1: all estimators share it."""
    from scipy.spatial import cKDTree

    coords = np.asarray(points, dtype=float)
    z = np.asarray(values, dtype=float).ravel()
    _check_inputs(coords, z, grid)

    finite = np.isfinite(z) & np.isfinite(coords).all(axis=1)
    coords, z = coords[finite], z[finite]

    transform = (
        anisotropy_transform(variogram.anisotropy_ratio, variogram.anisotropy_angle)
        if variogram.anisotropy_ratio > 1.0
        else np.eye(2)
    )
    search_points = coords @ transform.T
    centres = grid.cell_centres()
    radius = max_radius if max_radius is not None else variogram.range_
    neighbours = min(max_neighbours, len(coords))

    tree = cKDTree(search_points)
    offsets = _block_offsets(grid, block) if block else None

    # **The block's own average covariance**, and the reason a block estimate is
    # better known than a point one. The point formula starts from C(0) — the
    # sill — because a point is perfectly correlated with itself. A block is
    # not: its discretisation points sit at a spread of lags from each other, so
    # the block is correlated with itself by C-bar(V,V), which is smaller. Using
    # the sill here instead gives a block variance *higher* than the point
    # variance, which is backwards, and is what the first version of this did.
    within_block = (
        _within_block_covariance(offsets, transform, variogram)
        if offsets is not None
        else variogram.sill
    )

    estimate = np.full(grid.n_cells, np.nan, dtype=np.float64)
    variance = np.full(grid.n_cells, np.nan, dtype=np.float64)
    stats = SolveStats()

    for start in range(0, grid.n_cells, BLOCK_SIZE):
        chunk = centres[start : start + BLOCK_SIZE]
        query = chunk @ transform.T
        distances, indices = tree.query(query, k=neighbours, distance_upper_bound=radius)
        if neighbours == 1:
            distances = distances[:, None]
            indices = indices[:, None]

        missing = ~np.isfinite(distances)
        indices = np.where(missing, 0, indices)

        for offset in range(len(chunk)):
            usable = ~missing[offset]
            if int(usable.sum()) < max(1, min_neighbours):
                # Unestimated, not extrapolated (§11.0). The mask is the point:
                # `05` §6.5 reports the fraction, and this makes it visible.
                stats.unestimated += 1
                continue

            local = indices[offset][usable]
            node = chunk[offset]

            if offsets is None:
                right_side = variogram.covariance(distances[offset][usable])
            else:
                right_side = _block_covariance(
                    search_points[local], node, offsets, transform, variogram
                )

            value, sigma = _solve(
                search_points[local],
                z[local],
                right_side,
                variogram,
                mean,
                stats,
                self_covariance=within_block,
            )
            estimate[start + offset] = value
            variance[start + offset] = sigma

    if flags is not None:
        stats.report(flags, grid.n_cells)

    return KrigingResult(
        estimate=estimate.reshape(grid.ny, grid.nx),
        variance=variance.reshape(grid.ny, grid.nx),
        n_extrapolated=stats.unestimated,
    )


def _check_inputs(
    coords: NDArray[np.float64], z: NDArray[np.float64], grid: GridDefinition
) -> None:
    if coords.ndim != 2 or coords.shape[1] != 2:
        raise DegenerateInput(
            f"Points must be an (n, 2) array of planar coordinates in "
            f"{grid.frame.describe()}; got shape {coords.shape}."
        )
    if len(coords) != len(z):
        raise DegenerateInput(
            f"Got {len(coords)} points and {len(z)} values. Every control point "
            f"needs exactly one value."
        )


def _block_offsets(grid: GridDefinition, discretisation: int) -> NDArray[np.float64]:
    """Discretisation points inside one cell, relative to its centre.

    Offset by half a step from the cell edges rather than reaching them: points
    on a shared edge belong to two blocks, and a discretisation that doubles
    them biases the block covariance towards the boundary.
    """
    step = grid.cell_size / discretisation
    axis = (np.arange(discretisation) + 0.5) * step - grid.cell_size / 2.0
    mesh_x, mesh_y = np.meshgrid(axis, axis)
    return np.stack([mesh_x.ravel(), mesh_y.ravel()], axis=1)


def _within_block_covariance(
    offsets: NDArray[np.float64],
    transform: NDArray[np.float64],
    variogram: FittedVariogram,
) -> float:
    """`C-bar(V,V)`: the average covariance of a block with itself.

    Computed once per grid, because every cell has the same shape — the offsets
    are relative to the centre, so the answer does not depend on where the cell
    is. The diagonal is included: a discretisation point is perfectly correlated
    with itself, and dropping those terms would understate the block's own
    variance by 1/n.
    """
    discretised = offsets @ transform.T
    separation = discretised[:, None, :] - discretised[None, :, :]
    distance = np.hypot(separation[..., 0], separation[..., 1])
    return float(variogram.covariance(distance).mean())


def _block_covariance(
    neighbour_xy: NDArray[np.float64],
    node: NDArray[np.float64],
    offsets: NDArray[np.float64],
    transform: NDArray[np.float64],
    variogram: FittedVariogram,
) -> NDArray[np.float64]:
    """Average covariance between each neighbour and the block.

    The block-to-point covariance, which is what replaces the point-to-point
    right-hand side. Averaging covariances rather than averaging estimates is
    the whole of block kriging: the second gives the right mean and the wrong
    variance.
    """
    discretised = (node + offsets) @ transform.T
    separation = neighbour_xy[:, None, :] - discretised[None, :, :]
    distance = np.hypot(separation[..., 0], separation[..., 1])
    return np.asarray(variogram.covariance(distance).mean(axis=1), dtype=np.float64)


def _solve(
    neighbour_xy: NDArray[np.float64],
    neighbour_z: NDArray[np.float64],
    right_side: NDArray[np.float64],
    variogram: FittedVariogram,
    mean: float | None,
    stats: SolveStats,
    *,
    self_covariance: float,
) -> tuple[float, float]:
    """One kriging system, simple or ordinary depending on `mean`.

    Cholesky first, `lstsq` on failure — §11.1's fallback. A singular system
    means duplicated or near-duplicated control, and least squares gives the
    best fit rather than an exact interpolation, which is what the data
    actually supports.
    """
    count = len(neighbour_z)
    separation = neighbour_xy[:, None, :] - neighbour_xy[None, :, :]
    pair_distance = np.hypot(separation[..., 0], separation[..., 1])
    covariance = variogram.covariance(pair_distance)

    if mean is not None:
        weights = _solve_system(covariance, right_side, stats)
        estimate = float(mean + np.dot(weights, neighbour_z - mean))
        sigma = float(self_covariance - np.dot(weights, right_side))
        return estimate, max(0.0, sigma)

    matrix = np.ones((count + 1, count + 1), dtype=np.float64)
    matrix[:count, :count] = covariance
    matrix[count, count] = 0.0
    augmented = np.ones(count + 1, dtype=np.float64)
    augmented[:count] = right_side

    solution = _solve_system(matrix, augmented, stats)
    weights = solution[:count]
    multiplier = float(solution[count])
    estimate = float(np.dot(weights, neighbour_z))
    sigma = float(self_covariance - np.dot(weights, right_side) - multiplier)
    return estimate, max(0.0, sigma)


def _solve_system(
    matrix: NDArray[np.float64], right: NDArray[np.float64], stats: SolveStats
) -> NDArray[np.float64]:
    try:
        return np.asarray(np.linalg.solve(matrix, right), dtype=np.float64)
    except np.linalg.LinAlgError:
        stats.singular += 1
        solution, *_ = np.linalg.lstsq(matrix, right, rcond=None)
        return np.asarray(solution, dtype=np.float64)


__all__ = [
    "BLOCK_DISCRETISATION",
    "MAX_NEIGHBOURS",
    "MIN_NEIGHBOURS",
    "SolveStats",
    "block_kriging",
    "simple_kriging",
]
