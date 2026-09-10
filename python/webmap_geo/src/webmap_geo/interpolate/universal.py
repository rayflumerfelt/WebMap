"""Universal kriging, with a polynomial drift. `05-geoprocessing.md` §6.2.

**What it is for.** Ordinary kriging assumes the mean is constant — unknown,
but the same everywhere within a neighbourhood. A structure map across a
dipping basin violates that: the mean depth genuinely changes across the map,
which is exactly what `variogram.fit` reports when it picks a `power` model
with no sill. Under that assumption ordinary kriging pulls toward a local mean
that is not the right one, and it understates its own uncertainty away from
control.

Universal kriging estimates the trend and the residual **together**, in one
system per node, rather than fitting a trend first and kriging what is left.
That distinction matters and is the one people get wrong: fitting a global
trend first and kriging the residual is *regression kriging*
(`13-kriging.md` §10.0), a different method with a different bias, and its
residual variogram must be fitted by REML rather than least squares
([`adr/0011`](../../../../docs/adr/0011-reml-for-trend-residual-variograms.md)).
Universal kriging avoids that problem entirely by never forming a residual.

**It is not fault-aware.** Distance here is Euclidean, exactly as in `05` §6.2:
the surface is continuous across every fault. Minimum curvature remains the
fault-aware method.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from webmap_geo.exceptions import DegenerateInput
from webmap_geo.grid import GridDefinition
from webmap_geo.interpolate.kriging import (
    BLOCK_SIZE,
    DEFAULT_NEIGHBORS,
    KrigingResult,
)
from webmap_geo.variogram.model import FittedVariogram, anisotropy_transform

#: Drift orders this supports. 0 is ordinary kriging and is offered so a
#: caller can compare like with like; 2 is available and rarely wanted, because
#: a quadratic drift over a moving neighbourhood extrapolates violently past
#: the last control point.
MAX_DRIFT_ORDER = 2

#: A drift of order p needs at least this many neighbours before the system is
#: determined: one per basis function, plus enough left over for the covariance
#: to say anything. Below it the drift is fitted through the points exactly and
#: the "kriging" is polynomial extrapolation with extra steps.
MIN_NEIGHBOURS_PER_TERM = 3


def drift_basis(coords: NDArray[np.float64], order: int) -> NDArray[np.float64]:
    """Polynomial basis functions evaluated at `coords`.

    Order 0 is `[1]` — the constant that makes this ordinary kriging. Order 1
    adds `x` and `y`, order 2 adds `x²`, `xy`, `y²`.

    **Coordinates are centred by the caller**, not here: an unshifted state-
    plane easting is around 3,000,000 ft, and its square is 9e12. Mixing that
    with a covariance of order 10,000 gives a system whose condition number is
    around 1e9, and the solve returns weights that are numerically meaningless
    while looking perfectly ordinary.
    """
    x = coords[:, 0]
    y = coords[:, 1]
    columns: list[NDArray[np.float64]] = [np.ones(len(coords), dtype=float)]
    if order >= 1:
        columns.extend([x, y])
    if order >= 2:
        columns.extend([x * x, x * y, y * y])
    return np.column_stack(columns)


def n_drift_terms(order: int) -> int:
    return {0: 1, 1: 3, 2: 6}[order]


def universal_kriging(
    points: NDArray[np.floating],
    values: NDArray[np.floating],
    grid: GridDefinition,
    variogram: FittedVariogram,
    *,
    drift_order: int = 1,
    n_neighbors: int = DEFAULT_NEIGHBORS,
    max_radius: float | None = None,
) -> KrigingResult:
    """Universal kriging of `values` at `points` onto `grid`.

    Returns estimate and variance, both `(ny, nx)` with row 0 at the north —
    the same contract as `ordinary_kriging`, so the dispatcher and every
    diagnostic downstream treat them identically.

    `drift_order` 1 is the useful default: a plane through each neighbourhood,
    which is what a dipping surface needs. Order 0 reduces exactly to ordinary
    kriging and exists so the two can be compared without changing method.
    """
    if drift_order not in (0, 1, 2):
        raise DegenerateInput(
            f"Drift order must be 0, 1 or 2; got {drift_order}. 0 is a constant "
            f"mean (ordinary kriging), 1 a plane, 2 a quadratic surface. Beyond "
            f"that the drift extrapolates violently past the last control point."
        )

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

    terms = n_drift_terms(drift_order)
    required = terms * MIN_NEIGHBOURS_PER_TERM
    if len(coords) < required:
        raise DegenerateInput(
            f"Universal kriging with a drift of order {drift_order} needs at least "
            f"{required} control points ({terms} drift terms); got {len(coords)}. "
            f"With fewer, the drift is fitted exactly through the data and the "
            f"result is polynomial extrapolation rather than kriging. Use "
            f"drift_order=0, or minimum curvature."
        )

    transform = (
        anisotropy_transform(variogram.anisotropy_ratio, variogram.anisotropy_angle)
        if variogram.anisotropy_ratio > 1.0
        else np.eye(2)
    )
    search_points = coords @ transform.T
    centres = grid.cell_centres()
    search_nodes = centres @ transform.T

    # **Centred on the data, once.** See `drift_basis`: an uncentred easting
    # squared is 9e12 against a covariance of order 1e4, and the resulting
    # condition number makes the solve meaningless without making it fail.
    origin = search_points.mean(axis=0)
    scale = float(np.max(np.abs(search_points - origin))) or 1.0

    radius = max_radius if max_radius is not None else variogram.range_
    neighbours = min(n_neighbors, len(coords))
    if neighbours < terms + 1:
        raise DegenerateInput(
            f"A drift of order {drift_order} has {terms} terms and needs more "
            f"neighbours than that to leave anything for the covariance to "
            f"explain; n_neighbors is {neighbours}. Raise it to at least "
            f"{terms * MIN_NEIGHBOURS_PER_TERM}."
        )

    from scipy.spatial import cKDTree

    tree = cKDTree(search_points)

    estimate = np.full(grid.n_cells, np.nan, dtype=np.float64)
    variance = np.full(grid.n_cells, np.nan, dtype=np.float64)
    extrapolated = 0
    singular = 0

    for start in range(0, grid.n_cells, BLOCK_SIZE):
        block = search_nodes[start : start + BLOCK_SIZE]
        distances, indices = tree.query(block, k=neighbours, distance_upper_bound=radius)
        if neighbours == 1:
            distances = distances[:, None]
            indices = indices[:, None]

        # cKDTree marks "no neighbour within the radius" with an index one past
        # the end and an infinite distance. Left as-is it indexes out of bounds;
        # clipped without masking it silently reuses point 0.
        missing = ~np.isfinite(distances)
        indices = np.where(missing, 0, indices)

        for offset in range(len(block)):
            usable = ~missing[offset]
            count = int(usable.sum())
            if count == 0:
                # No control within the radius. Left as NaN rather than filled:
                # a trend surface drawn across a gap looks like data and is not
                # (`05` §6.5) — and a *drifted* one looks even more like data,
                # because it slopes.
                extrapolated += 1
                continue
            if count < terms:
                # Fewer neighbours than drift terms leaves the constraint block
                # underdetermined, and the solve would return something the data
                # does not support.
                #
                # `count == terms` is allowed through: the drift constraints then
                # determine the weights exactly and the covariance contributes
                # nothing, which is degenerate but *defined* — and at order 0 it
                # is the single-neighbour case ordinary kriging also estimates.
                # Excluding it made universal kriging leave 21% more cells blank
                # than ordinary kriging on the same input, which broke the
                # reduction this method is checked against.
                extrapolated += 1
                continue

            local = indices[offset][usable]
            value, sigma, was_singular = _solve_node(
                (search_points[local] - origin) / scale,
                z[local],
                (block[offset] - origin) / scale,
                distances[offset][usable],
                variogram,
                drift_order,
                scale,
            )
            singular += int(was_singular)
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
    node_xy: NDArray[np.float64],
    node_distance: NDArray[np.float64],
    variogram: FittedVariogram,
    drift_order: int,
    scale: float,
) -> tuple[float, float, bool]:
    """One universal-kriging system.

    Solved in covariance form with the drift constraints as Lagrange
    multipliers:

        [ C   F ] [ w ]   [ c  ]
        [ Fᵀ  0 ] [ μ ] = [ f0 ]

    `C` is the covariance between neighbours, `c` between each neighbour and
    the node, `F` the drift basis at the neighbours and `f0` at the node. The
    lower block is what makes the estimator unbiased *for a mean that varies*:
    the weights must reproduce every drift function exactly, not merely sum to
    one. With `drift_order = 0`, `F` is a column of ones and this is precisely
    the ordinary-kriging system.

    Coordinates arrive centred and scaled (see `universal_kriging`).
    """
    n = len(neighbour_z)
    terms = n_drift_terms(drift_order)
    size = n + terms

    separation = neighbour_xy[:, None, :] - neighbour_xy[None, :, :]
    pair_distance = np.hypot(separation[..., 0], separation[..., 1])

    basis = drift_basis(neighbour_xy, drift_order)
    node_basis = drift_basis(node_xy.reshape(1, 2), drift_order).ravel()

    matrix = np.zeros((size, size), dtype=np.float64)
    # The covariance block is computed on the *unscaled* separation, because
    # the variogram's range is in the frame's units. Only the drift basis is
    # scaled — mixing the two would silently change the model.
    # **The covariance block is in frame units, the drift block is scaled.**
    # `neighbour_xy` arrives centred and divided by `scale` for conditioning, so
    # the pair distances come back multiplied by it — mixing the two would
    # silently change the variogram's range by a factor of `scale`, which on a
    # state-plane layer is around 100,000.
    matrix[:n, :n] = variogram.covariance(pair_distance * scale)
    matrix[:n, n:] = basis
    matrix[n:, :n] = basis.T

    right = np.zeros(size, dtype=np.float64)
    right[:n] = variogram.covariance(node_distance)
    right[n:] = node_basis

    try:
        solution = np.linalg.solve(matrix, right)
    except np.linalg.LinAlgError:
        # Collinear control — every point on one line, say — makes the drift
        # columns dependent and the block singular. Falling back to the nearest
        # value is what the data supports, and far better than a NaN that then
        # spreads through contouring.
        nearest = int(np.argmin(node_distance))
        return float(neighbour_z[nearest]), float(variogram.sill), True

    weights = solution[:n]
    multipliers = solution[n:]

    estimate = float(np.dot(weights, neighbour_z))
    # Universal kriging variance: sill - w.c - mu.f0. The drift terms enter
    # with a positive sign, which is why a UK variance is never below the
    # equivalent OK variance: estimating a trend costs certainty.
    sigma = float(variogram.sill - np.dot(weights, right[:n]) - np.dot(multipliers, node_basis))
    return estimate, max(0.0, sigma), False


__all__ = [
    "MAX_DRIFT_ORDER",
    "MIN_NEIGHBOURS_PER_TERM",
    "drift_basis",
    "n_drift_terms",
    "universal_kriging",
]
