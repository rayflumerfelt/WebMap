"""Cubic and thin-plate spline interpolation. `05-geoprocessing.md` §6.4.

A radial basis function through every control point. Fast, very smooth, and it
**overshoots** — which is the whole story of this method and the reason §6.4
attaches a warning to it rather than to the others.

Overshoot is not a defect to be tuned away. An RBF that honours every point
exactly must bend to reach them, and between two close points at different
values it swings past both. On a porosity map that produces negative porosity;
on a structure map it produces a dome nobody logged. `05` §6.4's rule — warn
when the output range exceeds the input range by more than 20% — is the
mitigation, and this module measures it rather than hoping.

**Not fault-aware.** Distance is Euclidean, as it is for kriging (`05` §6.2).
A spline surface is continuous across every fault, and is in fact the *worst*
of the methods at a fault, because its smoothness constraint actively resists
the discontinuity. Minimum curvature remains the fault-aware method.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from webmap_geo.exceptions import DegenerateInput
from webmap_geo.grid import GridDefinition

#: Kernels worth offering. `thin_plate_spline` is the classic minimum-curvature
#: RBF in two dimensions and the sensible default; `cubic` is stiffer and
#: overshoots more; `quintic` stiffer still; `linear` barely overshoots and
#: barely smooths.
#:
#: **Every one of these is scale-invariant**, which is why `multiquadric` and
#: the other Gaussian-family kernels are not here. SciPy requires an explicit
#: `epsilon` for those, and epsilon is in the *coordinate system's units* — a
#: value that works on a UTM layer in metres is wrong by a factor of three on
#: the same ground in feet, and there is no honest default. Offering a kernel
#: whose only tuning parameter cannot be chosen for the caller is offering a
#: way to get a bad surface. A parametrised test found this one listed and
#: unusable.
KERNELS = ("thin_plate_spline", "cubic", "quintic", "linear")

#: Neighbours per node. An RBF is global by construction — every control point
#: contributes to every estimate — which at 500,000 points is a dense
#: 500,000-square solve, the same intractability the moving neighbourhood in
#: `05` §6.2 exists to avoid. `RBFInterpolator` takes a neighbour count and
#: solves a local system per node instead.
DEFAULT_NEIGHBORS = 64

#: `05` §6.4: warn when the output range exceeds the input range by more than
#: this fraction.
OVERSHOOT_WARNING = 0.2

#: Below this the local systems are ill-conditioned and the surface develops
#: ringing between points. Thin-plate needs enough neighbours to define a
#: plane plus curvature.
MIN_POINTS = 10


@dataclass(frozen=True)
class SplineResult:
    """The surface, plus what it did to the data's range.

    `overshoot` is the fraction by which the output range exceeds the input
    range. It is reported rather than clamped: clamping would flatten the
    surface against an arbitrary ceiling and hide that the method was the wrong
    one, where a number lets the caller choose.
    """

    estimate: NDArray[np.float64]
    n_extrapolated: int
    input_range: tuple[float, float]
    output_range: tuple[float, float]
    overshoot: float
    kernel: str

    @property
    def overshoots_badly(self) -> bool:
        return self.overshoot > OVERSHOOT_WARNING


def spline(
    points: NDArray[np.floating],
    values: NDArray[np.floating],
    grid: GridDefinition,
    *,
    kernel: str = "thin_plate_spline",
    smoothing: float = 0.0,
    n_neighbors: int = DEFAULT_NEIGHBORS,
    max_radius: float | None = None,
) -> SplineResult:
    """Interpolate `values` at `points` onto `grid` with a radial basis function.

    `smoothing` of 0 interpolates every control point exactly. Raising it
    trades exactness for a calmer surface, which is usually the right trade on
    noisy data — an exact fit through measurement error reproduces the error
    as structure.

    `max_radius` masks nodes with no control within it. Without a mask an RBF
    will happily extrapolate to the corners of the grid, and it extrapolates
    *badly*: a thin-plate spline grows without bound away from its data, so an
    unmasked corner can be thousands of units past anything observed.
    """
    if kernel not in KERNELS:
        raise DegenerateInput(
            f"'{kernel}' is not a spline kernel. Available: {', '.join(KERNELS)}. "
            f"thin_plate_spline is the usual choice; cubic is stiffer and "
            f"overshoots more."
        )
    if smoothing < 0:
        raise DegenerateInput(
            f"Smoothing must be zero or positive; got {smoothing:g}. Zero "
            f"interpolates every control point exactly."
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
    if len(coords) < MIN_POINTS:
        raise DegenerateInput(
            f"A spline needs at least {MIN_POINTS} control points; got "
            f"{len(coords)}. Below that the local systems are ill-conditioned "
            f"and the surface rings between the points. Use nearest-neighbour to "
            f"inspect coverage first."
        )

    # Duplicated locations make the RBF system singular — two rows identical,
    # two different values demanded at one place. Averaging is the only sane
    # resolution and it is what a geologist would do by hand.
    coords, z, merged = _merge_duplicates(coords, z)

    from scipy.interpolate import RBFInterpolator

    neighbours = min(n_neighbors, len(coords))
    interpolator = RBFInterpolator(
        coords,
        z,
        neighbors=neighbours,
        kernel=kernel,
        smoothing=smoothing,
    )

    centres = grid.cell_centres()
    estimate = np.asarray(interpolator(centres), dtype=np.float64)

    extrapolated = 0
    if max_radius is not None:
        from scipy.spatial import cKDTree

        distance, _ = cKDTree(coords).query(centres, k=1)
        beyond = distance > max_radius
        estimate[beyond] = np.nan
        extrapolated = int(beyond.sum())

    return _summarise(estimate, z, grid, kernel, extrapolated, merged)


def _summarise(
    estimate: NDArray[np.float64],
    z: NDArray[np.float64],
    grid: GridDefinition,
    kernel: str,
    extrapolated: int,
    merged: int,
) -> SplineResult:
    """Measure the overshoot rather than asserting the method is fine."""
    del merged  # reported by the caller through ControlPoints, not here
    usable = estimate[np.isfinite(estimate)]
    if usable.size == 0:
        raise DegenerateInput(
            "The spline produced no finite values. Every node fell outside the "
            "search radius, or the control points are collinear."
        )

    input_span = float(z.max() - z.min())
    output_range = (float(usable.min()), float(usable.max()))
    output_span = output_range[1] - output_range[0]

    # Measured against the *input* span so the number means "how much did this
    # invent", not "how wide is the surface". A span of zero — every control
    # point at one value — makes the ratio undefined rather than infinite.
    overshoot = 0.0 if input_span <= 0 else max(0.0, (output_span - input_span) / input_span)

    return SplineResult(
        estimate=estimate.reshape(grid.ny, grid.nx),
        n_extrapolated=extrapolated,
        input_range=(float(z.min()), float(z.max())),
        output_range=output_range,
        overshoot=overshoot,
        kernel=kernel,
    )


def _merge_duplicates(
    coords: NDArray[np.float64], z: NDArray[np.float64]
) -> tuple[NDArray[np.float64], NDArray[np.float64], int]:
    """Average values at coincident locations.

    Two picks at one surface location is a real thing, and an RBF cannot
    represent two values at one point — the system is singular and
    `RBFInterpolator` raises rather than choosing. Kriging survives this
    because `05` §6.2's solver falls back to the nearest value; a spline has no
    such fallback, so the resolution happens here and the count is returned so
    a caller can report it.
    """
    rounded = np.round(coords, 6)
    _unique, index, inverse = np.unique(rounded, axis=0, return_index=True, return_inverse=True)
    if len(index) == len(coords):
        return coords, z, 0

    averaged = np.zeros(len(index), dtype=float)
    counts = np.zeros(len(index), dtype=int)
    np.add.at(averaged, inverse, z)
    np.add.at(counts, inverse, 1)
    return coords[index], averaged / counts, len(coords) - len(index)


__all__ = [
    "DEFAULT_NEIGHBORS",
    "KERNELS",
    "MIN_POINTS",
    "OVERSHOOT_WARNING",
    "SplineResult",
    "spline",
]
