"""Hilbert curve indexing for spatial ordering of feature tables.

This exists for one reason, stated in `11-file-io.md` §6.1: Parquet prunes by
row-group statistics, and the tile query in `06-rendering.md` §7 filters on
the stored bbox columns. Features written in file order give row groups whose
bounding boxes each span the whole layer, so nothing prunes and every tile
reads every row group. Sorting on a Hilbert index of the centroid makes row
groups spatially compact and the pruning actually work.

The Hilbert curve rather than a Z-order/Morton curve because it has no long
jumps: consecutive indices are always adjacent cells, so a run of rows is a
connected region. Morton's jumps at quadrant boundaries put distant features
in the same row group, which is exactly the property being bought here.
"""

import numpy as np
from numpy.typing import NDArray

from webmap_geo.exceptions import DegenerateInput

# 16 bits per axis. A 65536x65536 grid over the layer extent resolves to
# about 1 m over a 65 km survey — far finer than row-group granularity needs,
# and the index still fits in 32 bits with room to spare.
DEFAULT_ORDER = 16


def hilbert_index(
    x: NDArray[np.float64],
    y: NDArray[np.float64],
    bbox: tuple[float, float, float, float],
    *,
    order: int = DEFAULT_ORDER,
) -> NDArray[np.uint64]:
    """Hilbert distance along a 2^order x 2^order curve over `bbox`.

    Coordinates outside `bbox` are clamped to its edge rather than rejected.
    A point one float-epsilon outside a bbox computed from the same points is
    a rounding artefact, not a data error, and failing on it would make ingest
    non-deterministic across platforms.
    """
    if order < 1 or order > 31:
        raise DegenerateInput(
            f"Hilbert order must be between 1 and 31; got {order}. Order 16 "
            f"is the default and resolves roughly 1 m over a 65 km extent."
        )
    if x.shape != y.shape:
        raise DegenerateInput(
            f"x has {x.size} coordinates and y has {y.size}. They describe the "
            f"same points and must be the same length."
        )

    west, south, east, north = bbox
    side = np.uint64(1) << np.uint64(order)
    top = int(side) - 1

    # A zero-width extent (every feature at one location, or a single point)
    # would divide by zero. Collapse it to column 0 — the ordering is
    # meaningless there and any stable answer is correct.
    width = east - west
    height = north - south
    xi = _quantize(x, west, width, top)
    yi = _quantize(y, south, height, top)

    return _xy_to_d(xi, yi, order)


def _quantize(
    values: NDArray[np.float64], origin: float, extent: float, top: int
) -> NDArray[np.int64]:
    if extent <= 0.0:
        return np.zeros(values.shape, dtype=np.int64)
    scaled = (values - origin) / extent * top
    return np.clip(np.nan_to_num(scaled, nan=0.0), 0, top).astype(np.int64)


def _xy_to_d(x: NDArray[np.int64], y: NDArray[np.int64], order: int) -> NDArray[np.uint64]:
    """Vectorised xy->d, the standard iterative Hilbert construction.

    Walks the quadtree from the coarsest level down, accumulating the curve
    distance and rotating the local frame at each level so the curve stays
    continuous across quadrant boundaries. Branchless via np.where because
    this runs over every feature in a layer at ingest.
    """
    x = x.copy()
    y = y.copy()
    d = np.zeros(x.shape, dtype=np.uint64)
    top = (np.int64(1) << np.int64(order)) - 1

    for level in range(order - 1, -1, -1):
        s = np.int64(1) << np.int64(level)
        rx = ((x & s) > 0).astype(np.int64)
        ry = ((y & s) > 0).astype(np.int64)
        d += (np.uint64(s) * np.uint64(s)) * ((3 * rx) ^ ry).astype(np.uint64)

        # Rotate the quadrant so the sub-curve joins up with its neighbours.
        # The reflection is against the full grid (`top`), not the current
        # level: it complements every bit, which is what carries the rotation
        # down to the levels still to be walked.
        flip = (ry == 0) & (rx == 1)
        x = np.where(flip, top - x, x)
        y = np.where(flip, top - y, y)
        swap = ry == 0
        x, y = np.where(swap, y, x), np.where(swap, x, y)

    return d
