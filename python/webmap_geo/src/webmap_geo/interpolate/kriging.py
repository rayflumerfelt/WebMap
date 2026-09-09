"""Ordinary kriging with a moving neighbourhood. `05-geoprocessing.md` §6.2.

**Why local.** Ordinary kriging solves an (n+1) x (n+1) system per estimate.
Global kriging at n = 500,000 is a 500,001-square dense system — roughly 2 TB
and O(n^3) to factor. Not slow: impossible. Moving neighbourhoods reduce this
to `n_neighbors`-square systems solved per grid node, which is standard
practice and also composes correctly with fault constraints, because the
neighbourhood search is exactly where barrier awareness applies.

**Anisotropy is applied by transforming coordinates** into the variogram's
principal frame before any search, so the neighbourhood is elliptical and lag
distances respect the direction of continuity. Doing it any later would mean
an isotropic neighbourhood feeding an anisotropic model, which produces a
surface elongated by roughly the square root of the ratio — visibly wrong to
nobody and numerically wrong to everyone.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from webmap_geo.exceptions import DegenerateInput
from webmap_geo.grid import GridDefinition
from webmap_geo.variogram.model import FittedVariogram, anisotropy_transform

#: `05` §6.2. Enough neighbours that the estimate is stable, few enough that
#: a 48x48 solve per node is cheap. Beyond about 64 the extra points are
#: outside the range and contribute weights near zero at real cost.
DEFAULT_NEIGHBORS = 48

#: Nodes are solved in blocks so the neighbour query is vectorised. Larger
#: blocks are faster and hold more memory; 4096 keeps the neighbour array
#: under a few hundred megabytes at the default neighbour count.
BLOCK_SIZE = 4096


@dataclass(frozen=True)
class KrigingResult:
    """Estimate and variance, both shaped (ny, nx).

    **The variance is not decoration.** It is the one number that says where
    the surface is interpolated and where it is invented: near control it is
    close to the nugget, and far from any it approaches the sill. A map drawn
    without regard to it presents an extrapolated corner with the same
    confidence as a well-controlled centre.
    """

    estimate: NDArray[np.float64]
    variance: NDArray[np.float64]
    #: Cells with no control point inside the search radius. These are
    #: extrapolated, not interpolated, and `05` §6.5 requires saying so.
    n_extrapolated: int

    @property
    def extrapolated_fraction(self) -> float:
        return self.n_extrapolated / self.estimate.size if self.estimate.size else 0.0


def ordinary_kriging(
    points: NDArray[np.floating],
    values: NDArray[np.floating],
    grid: GridDefinition,
    variogram: FittedVariogram,
    *,
    n_neighbors: int = DEFAULT_NEIGHBORS,
    max_radius: float | None = None,
) -> KrigingResult:
    """Ordinary kriging of `values` at `points` onto `grid`.

    Returns estimate and variance, both (ny, nx) with row 0 at the north.

    `max_radius` defaults to the variogram range: beyond it the covariance is
    zero, so a farther point contributes a weight of zero at the cost of a
    larger system. Passing a larger radius does not improve the estimate; it
    only slows it down.
    """
    coords = np.asarray(points, dtype=float)
    z = np.asarray(values, dtype=float).ravel()

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

    finite = np.isfinite(z) & np.isfinite(coords).all(axis=1)
    coords, z = coords[finite], z[finite]
    if len(coords) < 3:
        raise DegenerateInput(
            f"Only {len(coords)} control points have finite coordinates and "
            f"values. Ordinary kriging needs at least 3 to solve for a mean and "
            f"a gradient; below that, use nearest-neighbour to inspect coverage."
        )

    # **Into the variogram's frame, before anything is measured.** Distances,
    # the neighbourhood, and the covariance are all computed here, so the
    # ellipse is honoured by all three at once.
    transform = (
        anisotropy_transform(variogram.anisotropy_ratio, variogram.anisotropy_angle)
        if variogram.anisotropy_ratio > 1.0
        else np.eye(2)
    )
    search_points = coords @ transform.T
    search_nodes = grid.cell_centres() @ transform.T

    radius = max_radius if max_radius is not None else variogram.range_
    neighbours = min(n_neighbors, len(coords))

    from scipy.spatial import cKDTree

    tree = cKDTree(search_points)

    estimate = np.full(grid.n_cells, np.nan, dtype=np.float64)
    variance = np.full(grid.n_cells, np.nan, dtype=np.float64)
    extrapolated = 0

    for start in range(0, grid.n_cells, BLOCK_SIZE):
        block = search_nodes[start : start + BLOCK_SIZE]
        distances, indices = tree.query(block, k=neighbours, distance_upper_bound=radius)
        if neighbours == 1:
            distances = distances[:, None]
            indices = indices[:, None]

        # cKDTree marks "no neighbour within the radius" with an index one past
        # the end and an infinite distance. Left as-is it would index out of
        # bounds; clipped without masking it would silently reuse point 0.
        missing = ~np.isfinite(distances)
        indices = np.where(missing, 0, indices)

        for offset in range(len(block)):
            usable = ~missing[offset]
            count = int(usable.sum())
            if count == 0:
                # No control within the radius. Left as NaN rather than filled
                # with the global mean: a mean drawn across a gap looks like
                # data and is not (`05` §6.5).
                extrapolated += 1
                continue

            local = indices[offset][usable]
            local_distance = distances[offset][usable]

            value, sigma = _solve_node(
                search_points[local],
                z[local],
                local_distance,
                variogram,
            )
            estimate[start + offset] = value
            variance[start + offset] = sigma

    return KrigingResult(
        estimate=estimate.reshape(grid.ny, grid.nx),
        variance=variance.reshape(grid.ny, grid.nx),
        n_extrapolated=extrapolated,
    )


def _solve_node(
    neighbour_xy: NDArray[np.float64],
    neighbour_z: NDArray[np.float64],
    node_distance: NDArray[np.float64],
    variogram: FittedVariogram,
) -> tuple[float, float]:
    """One ordinary-kriging system.

    Solved in **covariance** form with a Lagrange multiplier enforcing that the
    weights sum to one — which is what makes it *ordinary* kriging: the mean is
    unknown and estimated locally rather than assumed.

    The system is:

        [ C   1 ] [ w ]   [ c ]
        [ 1^T 0 ] [ m ] = [ 1 ]

    where C is the covariance between neighbours, c the covariance between
    each neighbour and the node, w the weights, and m the multiplier.
    """
    n = len(neighbour_z)
    if n == 1:
        # One neighbour: the estimate is that value, and the variance is the
        # variogram at that distance. Solving a 2x2 system for this is not
        # wrong, just pointless.
        return float(neighbour_z[0]), float(variogram.gamma(node_distance)[0])

    separation = neighbour_xy[:, None, :] - neighbour_xy[None, :, :]
    pair_distance = np.hypot(separation[..., 0], separation[..., 1])

    matrix = np.ones((n + 1, n + 1), dtype=np.float64)
    matrix[:n, :n] = variogram.covariance(pair_distance)
    matrix[n, n] = 0.0

    right = np.ones(n + 1, dtype=np.float64)
    right[:n] = variogram.covariance(node_distance)

    try:
        solution = np.linalg.solve(matrix, right)
    except np.linalg.LinAlgError:
        # Duplicated control points make two rows identical and the matrix
        # singular. Falling back to the nearest value is honest — it is what
        # the data supports — and far better than propagating a NaN that then
        # spreads through contouring.
        nearest = int(np.argmin(node_distance))
        return float(neighbour_z[nearest]), float(variogram.sill)

    weights = solution[:n]
    multiplier = float(solution[n])

    estimate = float(np.dot(weights, neighbour_z))
    # Ordinary kriging variance: sill - w.c - m. Clamped at zero because
    # rounding can take it slightly negative, and a negative variance rendered
    # as a confidence map is nonsense.
    sigma = float(variogram.sill - np.dot(weights, right[:n]) - multiplier)
    return estimate, max(0.0, sigma)


def cross_validate(
    points: NDArray[np.floating],
    values: NDArray[np.floating],
    variogram: FittedVariogram,
    *,
    n_neighbors: int = DEFAULT_NEIGHBORS,
    subsample: int = 500,
    rng: np.random.Generator | None = None,
) -> dict[str, float]:
    """Leave-one-out cross validation on a subsample. `05` §6.5.

    The single most useful diagnostic a gridding job can report: it says how
    far the surface is from the data it was built on, in the data's own units.
    An RMSE close to the field's standard deviation means the model is barely
    better than the mean — which is worth knowing before the map goes on a
    slide.

    Subsampled because the full leave-one-out is one kriging system per point,
    and at 500,000 points that is the same intractability the moving
    neighbourhood exists to avoid.
    """
    rng = rng if rng is not None else np.random.default_rng(0)

    coords = np.asarray(points, dtype=float)
    z = np.asarray(values, dtype=float).ravel()
    finite = np.isfinite(z) & np.isfinite(coords).all(axis=1)
    coords, z = coords[finite], z[finite]

    if len(coords) < 10:
        raise DegenerateInput(
            f"Cross validation needs at least 10 control points; got {len(coords)}."
        )

    tested = min(subsample, len(coords))
    chosen = rng.choice(len(coords), size=tested, replace=False)

    transform = (
        anisotropy_transform(variogram.anisotropy_ratio, variogram.anisotropy_angle)
        if variogram.anisotropy_ratio > 1.0
        else np.eye(2)
    )
    search_points = coords @ transform.T

    from scipy.spatial import cKDTree

    tree = cKDTree(search_points)
    errors: list[float] = []

    for index in chosen:
        # k+1 because the point itself is always its own nearest neighbour —
        # and including it would make every prediction exact and the RMSE zero,
        # which is the classic way to produce a cross validation that validates
        # nothing.
        _, candidates = tree.query(search_points[index], k=min(n_neighbors + 1, len(coords)))
        neighbours = np.array([c for c in np.atleast_1d(candidates) if c != index])
        if len(neighbours) < 2:
            continue

        node_distance = np.hypot(*(search_points[neighbours] - search_points[index]).T)
        predicted, _ = _solve_node(
            search_points[neighbours], z[neighbours], node_distance, variogram
        )
        errors.append(predicted - float(z[index]))

    if not errors:
        raise DegenerateInput(
            "Cross validation produced no predictions — every point had fewer "
            "than two distinct neighbours. Check for duplicated coordinates."
        )

    residuals = np.array(errors)
    return {
        "rmse": float(np.sqrt(np.mean(residuals**2))),
        "mean_error": float(np.mean(residuals)),
        "mean_absolute_error": float(np.mean(np.abs(residuals))),
        "n_tested": float(len(residuals)),
        # The comparison that gives the RMSE meaning: an RMSE near the field's
        # own standard deviation says the model is barely better than the mean.
        "field_std": float(np.std(z)),
    }


__all__ = [
    "BLOCK_SIZE",
    "DEFAULT_NEIGHBORS",
    "KrigingResult",
    "cross_validate",
    "ordinary_kriging",
]
