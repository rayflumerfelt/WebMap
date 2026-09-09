"""Contour extraction. `05-geoprocessing.md` §7.

A contour map is how a geologist *reads* a surface, so its properties are
interpretive, not cosmetic:

- **Intervals are round numbers.** Geologists read them off the legend and
  hold them in their head. 1.0 with 18 contours beats 1.18 with 15.
- **Index contours** are every fifth, drawn heavier and labelled. Without
  them a dense map is unreadable.
- **Smoothing has a limit.** A smoothed contour can drift off the value it
  claims to represent, and a contour labelled 8,600 ft that runs 20 ft away
  from where the grid says 8,600 is a lie on a map somebody will measure.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from shapely.geometry import LineString

from webmap_geo.contour.smooth import MAX_SMOOTHING, chaikin
from webmap_geo.exceptions import DegenerateInput
from webmap_geo.grid import GridDefinition

#: Nice-number mantissas. 2.5 is included because a 25 ft interval on a
#: structure map reads naturally even though it is not a power of two or ten.
_NICE = (1.0, 2.0, 2.5, 5.0, 10.0)

#: Every fifth contour is an index contour — the convention on every published
#: structure map, and the reason a dense one is readable at all.
INDEX_EVERY = 5


@dataclass(frozen=True)
class ContourLine:
    """One contour, with what a style needs to draw it."""

    geometry: LineString
    value: float
    #: Every fifth level: drawn heavier and labelled.
    is_index: bool
    closed: bool

    @property
    def length(self) -> float:
        return float(self.geometry.length)


def auto_levels(vmin: float, vmax: float, target_count: int = 15) -> NDArray[np.float64]:
    """Choose a contour interval a geologist would choose.

    Snaps to 1, 2, 2.5 or 5 x 10^n. A range of 4.1 to 21.8 gives an interval of
    1.0 — eighteen contours — not 1.18 for exactly fifteen. **The round number
    wins over the requested count**, because the count is a preference and the
    interval is something a reader has to do arithmetic with.

    Levels land on multiples of the interval, not on the data's minimum. A
    contour set starting at 4.1 and stepping by 1 would label 4.1, 5.1, 6.1 —
    round intervals between unround values, which is the worst of both.
    """
    if not vmax > vmin:
        raise DegenerateInput(
            f"Cannot contour a surface with no range: min and max are both "
            f"{vmin:g}. A flat surface has no contours."
        )
    if target_count < 2:
        raise DegenerateInput(
            f"A contour map needs at least 2 levels; got target_count={target_count}."
        )

    span = vmax - vmin
    ideal = span / target_count
    exponent = int(np.floor(np.log10(ideal)))

    # **The nearest nice interval, measured in log space.** Not the smallest
    # one at or above the ideal: for the spec's own example — 4.1 to 21.8 into
    # about fifteen — rounding up gives 2.0 where §7 says the answer is 1.0.
    # An interval a little finer than asked for is a legible map with more
    # detail; one twice as coarse loses structure the data supports.
    #
    # Log space because 1.18 sits between 1 and 2, and in linear terms it looks
    # closer to 1 by a hair while in ratio terms — which is how a reader
    # perceives interval density — it clearly is.
    candidates = sorted(
        {m * 10.0**e for e in (exponent - 1, exponent, exponent + 1) for m in _NICE}
    )
    interval = min(candidates, key=lambda c: abs(np.log(c / ideal)))

    first = np.ceil(vmin / interval) * interval
    levels = np.arange(first, vmax + interval * 0.5, interval)

    # Trim any level sitting exactly on the surface's extremes: a contour at
    # the maximum is a single point or a hairline around the peak, which draws
    # as noise.
    levels = levels[(levels > vmin) & (levels < vmax)]
    if len(levels) == 0:
        raise DegenerateInput(
            f"No round contour interval fits a range of {span:g} "
            f"({vmin:g} to {vmax:g}). Widen the range or set levels explicitly."
        )
    return np.asarray(_snap(levels, interval), dtype=np.float64)


def _snap(values: NDArray[np.float64], interval: float) -> NDArray[np.float64]:
    """Round away binary dust.

    `3 * 0.1` is `0.30000000000000004`, and a contour labelled that instead of
    `0.3` defeats the entire purpose of choosing a round interval.
    """
    decimals = max(0, -int(np.floor(np.log10(interval))) + 2)
    return np.round(values, decimals)


def contour_grid(
    surface: NDArray[np.floating],
    grid: GridDefinition,
    levels: NDArray[np.floating] | None = None,
    *,
    target_count: int = 15,
    smoothing: float = 0.0,
    min_length: float | None = None,
    index_every: int = INDEX_EVERY,
) -> list[ContourLine]:
    """Extract contour lines from a gridded surface.

    NaN cells — fault-blanked areas and extrapolation masks — are respected:
    contourpy's `corner_mask` stops a contour being drawn across a hole, which
    would otherwise draw a line through ground where there is no data.

    `min_length` drops fragments shorter than it, defaulting to three cell
    widths. Without it a noisy surface produces a scatter of tiny closed
    contours that read as structure and are quantisation.
    """
    import contourpy

    values = np.asarray(surface, dtype=float)
    if values.shape != (grid.ny, grid.nx):
        raise DegenerateInput(
            f"Surface is {values.shape} but the grid is ({grid.ny}, {grid.nx}). "
            f"A transposed array here produces contours at right angles to the "
            f"structure."
        )
    if not 0.0 <= smoothing <= MAX_SMOOTHING:
        raise DegenerateInput(
            f"Smoothing runs from 0 to {MAX_SMOOTHING}; got {smoothing:g}. Above "
            f"{MAX_SMOOTHING} a contour drifts measurably off the value it "
            f"claims to represent, which is a lie on a map somebody will "
            f"measure."
        )

    finite = values[np.isfinite(values)]
    if finite.size == 0:
        raise DegenerateInput(
            "The surface is entirely NaN, so there is nothing to contour. Every "
            "cell was outside the search radius or blanked by a fault."
        )

    if levels is None:
        levels = auto_levels(float(finite.min()), float(finite.max()), target_count)
    levels = np.asarray(levels, dtype=float)

    if min_length is None:
        min_length = 3.0 * grid.cell_size

    generator = contourpy.contour_generator(
        x=grid.x_coordinates(),
        y=grid.y_coordinates(),
        z=values,
        # Respects NaN: a contour is not drawn across a hole.
        corner_mask=True,
        chunk_size=0,
        # Named rather than left to the default: with `Separate`, `lines()`
        # returns a plain list of (n, 2) arrays. Other line types return
        # nested structures, and code written against one silently mis-reads
        # another — the default has changed between contourpy versions.
        line_type=contourpy.LineType.Separate,
    )

    lines: list[ContourLine] = []
    for position, level in enumerate(levels):
        for raw in generator.lines(float(level)):
            # `lines()` is typed as a union across every LineType contourpy
            # supports. With `Separate` each entry is an (n, 2) float array,
            # and narrowing here is what lets the rest of this loop be typed
            # rather than threading `Any` through it.
            vertices = np.asarray(raw, dtype=float)
            if vertices.ndim != 2 or len(vertices) < 2:
                continue
            geometry = LineString(vertices)
            if smoothing > 0:
                geometry = _chaikin(geometry, smoothing)
            if geometry.length < min_length:
                continue
            lines.append(
                ContourLine(
                    geometry=geometry,
                    value=float(level),
                    # Indexed from the level list rather than from the value, so
                    # a set starting at an odd multiple still gets every fifth
                    # line heavier rather than an arbitrary one.
                    is_index=(position % index_every == 0),
                    closed=bool(
                        np.allclose(vertices[0], vertices[-1], atol=grid.cell_size * 1e-6)
                    ),
                )
            )

    return lines


def _chaikin(line: LineString, smoothing: float) -> LineString:
    """Corner-cutting, by the same rule a filled band's edge uses.

    Shared with `contour.bands` through `contour.smooth`: a band boundary and
    the line drawn over it are the same level, and two smoothing
    implementations would let the fill creep out from under the line.
    """
    coords = np.asarray(line.coords, dtype=float)
    closed = bool(np.allclose(coords[0], coords[-1]))
    return LineString(chaikin(coords, smoothing, closed=closed))


def index_levels(lines: list[ContourLine]) -> list[float]:
    """The values drawn as index contours, for a legend or a caption."""
    return sorted({line.value for line in lines if line.is_index})


__all__ = [
    "INDEX_EVERY",
    "MAX_SMOOTHING",
    "ContourLine",
    "auto_levels",
    "contour_grid",
    "index_levels",
]
