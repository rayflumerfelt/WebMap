"""The single interpolation entry point. `05-geoprocessing.md` §6.5.

"Validates, dispatches, returns grid + diagnostics."

**The diagnostics are the interesting part.** A gridded surface is the most
confident-looking artefact this system produces: smooth, coloured, contoured,
and identical in appearance whether it was built from two thousand wells or
from six. §6.5 lists what has to come back with it —

  - output value range against input range, so overshoot is visible
  - the fraction of cells with no control point within the search radius
  - per-compartment control counts when faults are used
  - cross-validation RMSE

— and the reason is spelled out there: "a grid with 40% of its area
extrapolated should say so." These feed the render metadata and the caption,
which is the only place a reader of the finished map will see them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import numpy as np
from numpy.typing import NDArray

from webmap_geo import __version__
from webmap_geo.exceptions import DegenerateInput
from webmap_geo.faults.network import Constraint
from webmap_geo.faults.raster import blocked_edges, compartments, control_per_compartment
from webmap_geo.grid import GridDefinition
from webmap_geo.interpolate.kriging import cross_validate, ordinary_kriging
from webmap_geo.interpolate.minimum_curvature import minimum_curvature
from webmap_geo.interpolate.spline import spline
from webmap_geo.interpolate.universal import universal_kriging
from webmap_geo.variogram.fit import fit_auto
from webmap_geo.variogram.model import FittedVariogram

#: Beyond this fraction of the surface being extrapolated, the map is mostly
#: invention. Not refused — a geologist may want it — but named prominently.
EXTRAPOLATION_WARNING = 0.4

#: `05` §6.4: "warn when output range exceeds input range by more than 20%."
OVERSHOOT_WARNING = 0.2


class Method(StrEnum):
    ORDINARY_KRIGING = "ordinary_kriging"
    UNIVERSAL_KRIGING = "universal_kriging"
    MINIMUM_CURVATURE = "minimum_curvature"
    CUBIC_SPLINE = "cubic_spline"
    IDW = "idw"
    NEAREST = "nearest"


@dataclass(frozen=True)
class InterpolationResult:
    """A gridded surface and everything needed to judge it.

    `lineage` is "sufficient to re-run and reproduce the identical grid"
    (Phase 4's criterion). That means the method, every parameter it took, the
    fitted variogram if there was one, the frame, and the package version —
    a grid re-run under a different `webmap_geo` is not the same grid, and
    saying so is cheaper than discovering it.
    """

    surface: NDArray[np.float64]
    grid: GridDefinition
    method: Method
    #: Kriging only. Elsewhere the uncertainty is not quantified, and a None
    #: here is more honest than a zero.
    variance: NDArray[np.float64] | None
    diagnostics: dict[str, Any]
    lineage: dict[str, Any]
    warnings: list[str] = field(default_factory=list)

    @property
    def is_mostly_extrapolated(self) -> bool:
        return bool(self.diagnostics.get("extrapolated_fraction", 0.0) > EXTRAPOLATION_WARNING)

    def describe(self) -> str:
        """One line for a caption. `04-mcp-server.md` §6.1."""
        parts = [self.method.value.replace("_", " ")]
        variogram = self.lineage.get("variogram")
        if variogram:
            parts.append(str(variogram.get("describe", "")))
        parts.append(self.grid.describe())
        return ", ".join(part for part in parts if part)


def interpolate(
    points: NDArray[np.floating],
    values: NDArray[np.floating],
    grid: GridDefinition,
    *,
    method: Method | str = Method.ORDINARY_KRIGING,
    constraints: list[Constraint] | None = None,
    variogram: FittedVariogram | None = None,
    n_neighbors: int = 48,
    max_radius: float | None = None,
    tension: float = 0.0,
    idw_power: float = 2.0,
    drift_order: int = 1,
    kernel: str = "thin_plate_spline",
    smoothing: float = 0.0,
    rng: np.random.Generator | None = None,
) -> InterpolationResult:
    """Grid `values` at `points`, with diagnostics.

    Constraints are honoured by whichever mechanism the method supports:
    minimum curvature drops blocked links from its stencil, and the diagnostics
    report per-compartment control counts either way — a compartment with no
    control is entirely extrapolated whatever the method, and that is worth
    saying before anyone contours it.
    """
    rng = rng if rng is not None else np.random.default_rng(0)
    method = Method(method)

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
            f"values — too few to interpolate. Check the value column for nulls "
            f"and the coordinates for a CRS mismatch."
        )

    hard = [c for c in (constraints or []) if c.is_hard]
    blocked = blocked_edges(hard, grid) if hard else None

    warnings: list[str] = []
    variance: NDArray[np.float64] | None = None
    lineage: dict[str, Any] = {
        "method": method.value,
        "webmap_geo_version": __version__,
        "frame": {"srid": grid.frame.srid, "units": grid.frame.units},
        "grid": {
            "xmin": grid.xmin,
            "ymin": grid.ymin,
            "cell_size": grid.cell_size,
            "nx": grid.nx,
            "ny": grid.ny,
        },
        "n_control_points": len(coords),
        "n_constraints": len(constraints or []),
        "n_hard_constraints": len(hard),
    }

    if method is Method.ORDINARY_KRIGING:
        if hard:
            # **Said plainly rather than silently ignored.** Kriging here is
            # Euclidean and stays that way: `05` §6.2 records the measurements
            # behind dropping barrier-aware kriging rather than shipping it.
            # A surface that smears throw across a sealing fault, under a fault
            # line the map draws on top, is the failure this sentence exists to
            # prevent — it looks like a finished structure map.
            warnings.append(
                f"{len(hard)} hard constraint(s) were supplied but ordinary "
                f"kriging does not honour them — it measures distance in a "
                f"straight line, so the surface is continuous across every "
                f"fault. Use minimum_curvature for a fault-aware surface, or "
                f"remove the constraints to acknowledge the choice."
            )
        if variogram is None:
            variogram = fit_auto(coords, z, rng=rng)
        result = ordinary_kriging(
            coords, z, grid, variogram, n_neighbors=n_neighbors, max_radius=max_radius
        )
        surface = result.estimate
        variance = result.variance
        lineage["variogram"] = {
            "model": variogram.model,
            "nugget": variogram.nugget,
            "sill": variogram.sill,
            "range": variogram.range_,
            "anisotropy_ratio": variogram.anisotropy_ratio,
            "anisotropy_angle": variogram.anisotropy_angle,
            "describe": variogram.describe(),
        }
        lineage["n_neighbors"] = n_neighbors
        lineage["max_radius"] = max_radius if max_radius is not None else variogram.range_

    elif method is Method.UNIVERSAL_KRIGING:
        if hard:
            # Same Euclidean distance as ordinary kriging, same sentence. A
            # drift term changes what the *mean* does across the map; it does
            # nothing about a discontinuity in it.
            warnings.append(
                f"{len(hard)} hard constraint(s) were supplied but universal "
                f"kriging does not honour them — it measures distance in a "
                f"straight line, so the surface is continuous across every "
                f"fault. Use minimum_curvature for a fault-aware surface, or "
                f"remove the constraints to acknowledge the choice."
            )
        if variogram is None:
            variogram = fit_auto(coords, z, rng=rng)
        universal = universal_kriging(
            coords,
            z,
            grid,
            variogram,
            drift_order=drift_order,
            n_neighbors=n_neighbors,
            max_radius=max_radius,
        )
        surface = universal.estimate
        variance = universal.variance
        lineage["variogram"] = {
            "model": variogram.model,
            "nugget": variogram.nugget,
            "sill": variogram.sill,
            "range": variogram.range_,
            "anisotropy_ratio": variogram.anisotropy_ratio,
            "anisotropy_angle": variogram.anisotropy_angle,
            "describe": variogram.describe(),
        }
        lineage["drift_order"] = drift_order
        lineage["n_neighbors"] = n_neighbors
        lineage["max_radius"] = max_radius if max_radius is not None else variogram.range_
        if variogram.model == "power" and drift_order == 0:
            # A power variogram means the mean is not constant, which is the
            # condition universal kriging exists for — and order 0 turns it
            # back into ordinary kriging, quietly.
            warnings.append(
                "The variogram fitted a power model, which means the mean varies "
                "across the map — but drift_order is 0, which assumes it does "
                "not. Raise it to 1 for a plane, or use ordinary kriging and "
                "accept the assumption knowingly."
            )

    elif method is Method.CUBIC_SPLINE:
        if hard:
            warnings.append(
                f"{len(hard)} hard constraint(s) were supplied but a spline does "
                f"not honour them. It is the *worst* method at a fault: its "
                f"smoothness constraint actively resists the discontinuity, so "
                f"throw is smeared into a ramp. Use minimum_curvature."
            )
        fitted = spline(
            coords,
            z,
            grid,
            kernel=kernel,
            smoothing=smoothing,
            n_neighbors=n_neighbors,
            max_radius=max_radius,
        )
        surface = fitted.estimate
        lineage["kernel"] = fitted.kernel
        lineage["smoothing"] = smoothing
        lineage["n_neighbors"] = n_neighbors
        lineage["overshoot"] = fitted.overshoot
        if fitted.overshoots_badly:
            # `05` §6.4's rule, measured rather than assumed. This is the one
            # thing this method does that the others do not, so it is said in
            # the method's own terms rather than left to the generic overshoot
            # check below.
            warnings.append(
                f"The spline's output range is {fitted.overshoot:.0%} wider than "
                f"the input range ({fitted.output_range[0]:,.4g} to "
                f"{fitted.output_range[1]:,.4g}, against control from "
                f"{fitted.input_range[0]:,.4g} to {fitted.input_range[1]:,.4g}). "
                f"A radial basis function must bend to reach every point, and "
                f"between two close points at different values it swings past "
                f"both. Raise `smoothing`, or use minimum curvature."
            )

    elif method is Method.MINIMUM_CURVATURE:
        curvature = minimum_curvature(coords, z, grid, tension=tension, blocked_edges=blocked)
        surface = curvature.estimate
        lineage["tension"] = tension
        lineage["iterations"] = curvature.n_iterations
        lineage["converged"] = curvature.converged
        if not curvature.converged:
            # `CLAUDE.md` §3.3: flagged, not silently returned.
            warnings.append(
                "The solver hit its iteration cap before converging. The surface "
                "is returned but may be wrong by a contour interval; re-run with "
                "a larger max_iterations or a coarser cell size."
            )

    elif method is Method.IDW:
        surface = _idw(coords, z, grid, power=idw_power, max_radius=max_radius)
        lineage["idw_power"] = idw_power
        warnings.append(
            "Inverse distance weighting produces bull's-eyes around control "
            "points and does not honour faults. It is offered for comparison; "
            "ordinary kriging or minimum curvature is the better default."
        )

    else:  # NEAREST
        surface = _nearest(coords, z, grid)
        warnings.append(
            "Nearest-neighbour is a coverage diagnostic, not a surface. Use it "
            "to see where control exists, not to contour."
        )

    diagnostics = _diagnostics(
        coords, z, surface, grid, blocked, variogram, n_neighbors, rng, max_radius
    )
    warnings.extend(_warnings_from(diagnostics, z, surface))

    return InterpolationResult(
        surface=surface,
        grid=grid,
        method=method,
        variance=variance,
        diagnostics=diagnostics,
        lineage=lineage,
        warnings=warnings,
    )


def _diagnostics(
    coords: NDArray[np.float64],
    z: NDArray[np.float64],
    surface: NDArray[np.float64],
    grid: GridDefinition,
    blocked: tuple[NDArray[np.bool_], NDArray[np.bool_]] | None,
    variogram: FittedVariogram | None,
    n_neighbors: int,
    rng: np.random.Generator,
    max_radius: float | None = None,
) -> dict[str, Any]:
    """Everything §6.5 requires, computed once."""
    finite_surface = surface[np.isfinite(surface)]

    radius = _search_radius(coords, max_radius)
    diagnostics: dict[str, Any] = {
        "input_range": [float(z.min()), float(z.max())],
        "output_range": (
            [float(finite_surface.min()), float(finite_surface.max())]
            if finite_surface.size
            else None
        ),
        "extrapolated_fraction": _extrapolated_fraction(coords, surface, grid, radius),
        "search_radius": radius,
        "n_control_points": len(coords),
    }

    if blocked is not None:
        labels, count = compartments(blocked, grid)
        tally = control_per_compartment(coords, labels, grid)
        diagnostics["n_compartments"] = count
        diagnostics["control_per_compartment"] = tally
        diagnostics["empty_compartments"] = sorted(
            label for label, n in tally.items() if n == 0
        )

    if variogram is not None and len(coords) >= 10:
        try:
            diagnostics["cross_validation"] = cross_validate(
                coords, z, variogram, n_neighbors=n_neighbors, rng=rng
            )
        except DegenerateInput:
            # A cross validation that cannot run is worth noting as absent
            # rather than as zero — a zero RMSE reads as a perfect fit.
            diagnostics["cross_validation"] = None

    return diagnostics


def _search_radius(coords: NDArray[np.float64], max_radius: float | None) -> float:
    """The distance beyond which a cell is not supported by data.

    `max_radius` when the caller gave one — that is literally the search
    radius, and kriging has already NaN'd everything past it.

    Otherwise **three times the median spacing between neighbouring control
    points**, and specifically *not* the fitted variogram range. Measured on a
    clustered 360-pick fixture over a 52,000 x 42,000 ft area: the fitted
    range came out at 48,290 ft — larger than the domain — so a range-based
    radius flagged 0% of a grid that was 61% invention. The correlation length
    a variogram reports and the distance at which a grid stops being supported
    by observations are different quantities, and on sparse control the first
    is routinely larger than the map.

    Three times, measured on that fixture against the true surface:

        radius                flagged   rms inside   rms outside
        2x spacing  1,284 ft     72%        19 ft      1,962 ft
        3x spacing  1,927 ft     62%        32 ft      2,126 ft
        4x spacing  2,569 ft     53%        52 ft      2,300 ft

    All three separate supported from unsupported by about two orders of
    magnitude. 3x is taken because 32 ft is roughly a third of a typical 100 ft
    structural contour interval: inside the radius the grid is worth
    contouring, and outside it is not.
    """
    from scipy.spatial import cKDTree

    if max_radius is not None:
        return float(max_radius)
    if len(coords) < 2:
        return float("inf")

    # k=2 because the nearest point to a control point is itself.
    spacing = cKDTree(coords).query(coords, k=2)[0][:, 1]
    return float(3.0 * np.median(spacing))


def _extrapolated_fraction(
    coords: NDArray[np.float64],
    surface: NDArray[np.float64],
    grid: GridDefinition,
    radius: float,
) -> float:
    """`05` §6.5: "the fraction of cells with no control point within the
    search radius".

    **Measured by distance, not by NaN.** Counting NaN cells was the obvious
    implementation and it is inert for the method most structure maps use:
    minimum curvature fills every cell, so `isnan(surface).mean()` is always
    0.0. A grid whose far corner sat 8,000 ft from any well, and which ran
    11,000 ft outside the range of the data that made it, reported that 0% of
    it was extrapolated — and that is the one number whose whole purpose is to
    stop somebody reading structure out of invention.

    A NaN cell still counts: it is a cell no method could estimate at all.
    """
    from scipy.spatial import cKDTree

    if not np.isfinite(radius):
        return float(np.isnan(surface).mean())

    distance, _ = cKDTree(coords).query(grid.cell_centres())
    beyond = distance.reshape(grid.ny, grid.nx) > radius
    return float((beyond | np.isnan(surface)).mean())


def _warnings_from(
    diagnostics: dict[str, Any],
    z: NDArray[np.float64],
    surface: NDArray[np.float64],
) -> list[str]:
    """Turn the numbers into sentences someone will actually read."""
    warnings: list[str] = []

    fraction = float(diagnostics.get("extrapolated_fraction", 0.0))
    if fraction > EXTRAPOLATION_WARNING:
        warnings.append(
            f"{fraction:.0%} of this grid has no control point within the search "
            f"radius and is extrapolated. It is drawn with the same colours and "
            f"contours as the well-controlled part — do not read structure from "
            f"those areas."
        )

    output = diagnostics.get("output_range")
    if output:
        spread = float(z.max() - z.min())
        over = max(output[1] - float(z.max()), float(z.min()) - output[0])
        if spread > 0 and over > OVERSHOOT_WARNING * spread:
            warnings.append(
                f"The surface ranges {output[0]:g} to {output[1]:g}, outside the "
                f"data's {z.min():g} to {z.max():g} by more than "
                f"{OVERSHOOT_WARNING:.0%} of its spread. That overshoot can "
                f"invent a closure the data does not support — raise the tension "
                f"or use a method that cannot overshoot."
            )

    empty = diagnostics.get("empty_compartments") or []
    if empty:
        warnings.append(
            f"{len(empty)} fault compartment(s) contain no control points at all. "
            f"The surface there is determined entirely by the smoothness term, "
            f"not by data."
        )

    validation = diagnostics.get("cross_validation")
    if validation and validation.get("field_std", 0) > 0:
        ratio = validation["rmse"] / validation["field_std"]
        if ratio > 0.9:
            warnings.append(
                f"Cross-validation RMSE ({validation['rmse']:.3g}) is close to the "
                f"data's own standard deviation ({validation['field_std']:.3g}), "
                f"so the surface is barely better than the mean. The property may "
                f"have no spatial structure at this well spacing."
            )

    return warnings


def _idw(
    coords: NDArray[np.float64],
    z: NDArray[np.float64],
    grid: GridDefinition,
    *,
    power: float,
    max_radius: float | None,
) -> NDArray[np.float64]:
    """Inverse distance weighting. `05` §6.4: "offer it, do not default to it."

    Produces bull's-eyes: every control point becomes a local extremum because
    its weight goes to infinity at zero distance. Useful for comparison and for
    a geologist who insists, and never the right default.
    """
    from scipy.spatial import cKDTree

    tree = cKDTree(coords)
    nodes = grid.cell_centres()
    k = min(16, len(coords))

    distances, indices = tree.query(
        nodes, k=k, distance_upper_bound=max_radius if max_radius else np.inf
    )
    if k == 1:
        distances, indices = distances[:, None], indices[:, None]

    missing = ~np.isfinite(distances)
    indices = np.where(missing, 0, indices)
    # An exact hit would divide by zero; the value there is simply that point's.
    exact = distances <= 0
    weights = np.where(missing | exact, 0.0, 1.0 / np.maximum(distances, 1e-12) ** power)

    total = weights.sum(axis=1)
    estimate = np.where(
        total > 0, (weights * z[indices]).sum(axis=1) / np.maximum(total, 1e-30), np.nan
    )
    # Exact hits win outright.
    hit_row, hit_col = np.where(exact & ~missing)
    estimate[hit_row] = z[indices[hit_row, hit_col]]
    # Nodes with no neighbour at all stay NaN.
    estimate[missing.all(axis=1)] = np.nan

    return np.asarray(estimate.reshape(grid.ny, grid.nx), dtype=np.float64)


def _nearest(
    coords: NDArray[np.float64], z: NDArray[np.float64], grid: GridDefinition
) -> NDArray[np.float64]:
    """Nearest neighbour. Diagnostic only — it shows where control exists."""
    from scipy.spatial import cKDTree

    _, indices = cKDTree(coords).query(grid.cell_centres())
    return np.asarray(z[indices].reshape(grid.ny, grid.nx), dtype=np.float64)


__all__ = [
    "EXTRAPOLATION_WARNING",
    "OVERSHOOT_WARNING",
    "InterpolationResult",
    "Method",
    "interpolate",
]
