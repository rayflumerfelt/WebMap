"""Chaikin corner-cutting, shared by lines and bands. `05-geoprocessing.md` §7.

**Shared rather than duplicated because the two have to agree.** A colour-filled
band and the contour line drawn over it are the same level, so if the band's
edge is smoothed by a different rule than the line, the fill creeps out from
under the line by a fraction of a cell all the way round the map. That reads as
a rendering fault rather than as two implementations of one curve, and nobody
can say what is wrong with it.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

#: Above this, smoothing moves a contour measurably off the value it claims.
#: `05` §7: "the API caps it and the render metadata records it."
MAX_SMOOTHING = 0.5

#: Two passes. One is visibly angular still; three starts pulling the curve in
#: toward its chord, which shrinks a closed contour and with it the area of
#: every band inside it.
_PASSES = 2


def chaikin(
    coords: NDArray[np.float64], smoothing: float, *, closed: bool
) -> NDArray[np.float64]:
    """Round the corners off a polyline or ring.

    Raw contours follow grid cell boundaries and look angular — a structure map
    of staircases. Chaikin replaces each corner with two points a fraction of
    the way along its edges, which rounds without introducing a control point
    the data did not have.

    The cut fraction is capped well below the 0.25 that would put a new vertex
    at the midpoint of its edge. `05` §7 is explicit that smoothing can drift a
    contour off the value it claims, and the cap is where that stops being
    negligible.

    `closed` is passed rather than inferred: a ring whose first and last points
    coincide and an open line that happens to return to its start are the same
    array, and treating a line as a ring drops its endpoints.
    """
    if len(coords) < 3:
        return coords

    fraction = 0.25 * (smoothing / MAX_SMOOTHING)

    for _ in range(_PASSES):
        cut: list[NDArray[np.float64]] = []
        if not closed:
            cut.append(coords[0])
        for index in range(len(coords) - 1):
            a, b = coords[index], coords[index + 1]
            cut.append(a + fraction * (b - a))
            cut.append(b - fraction * (b - a))
        if closed:
            cut.append(cut[0])
        else:
            cut.append(coords[-1])
        coords = np.asarray(cut)

    return coords


__all__ = ["MAX_SMOOTHING", "chaikin"]
