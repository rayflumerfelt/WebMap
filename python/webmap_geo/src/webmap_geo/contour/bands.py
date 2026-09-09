"""Filled contour bands — the polygons between levels. `05-geoprocessing.md` §7.

A colour-filled grid *renders* these bands; it does not produce them. The
difference matters as soon as anyone wants to do more than look:

- **Export.** A shapefile of "the 8,600–8,700 ft interval" is a deliverable. A
  coloured PNG is a picture of one.
- **Attribution.** Area per band answers "how much of this lease is above the
  spill point", which is the question a filled map is usually drawn to answer.
- **Editing.** A polygon can be snapped to, clipped and hand-corrected. A
  raster colour cannot.

Bands and lines come from one level list, smoothed by one rule, so the fill
edge sits exactly under the drawn contour (`08-styling-palettes.md` §5.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from typing import Any

import numpy as np
from numpy.typing import NDArray
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.validation import make_valid

from webmap_geo.contour.lines import auto_levels
from webmap_geo.contour.smooth import MAX_SMOOTHING, chaikin
from webmap_geo.exceptions import DegenerateInput
from webmap_geo.grid import GridDefinition


@dataclass(frozen=True)
class ContourBand:
    """The area of a surface between two levels.

    One band per interval rather than one per connected part: the band is what
    a legend entry names and what an area total is reported against, and a
    structure map's 8,600 ft interval is routinely a dozen disjoint pieces
    nobody wants listed separately. Explode the multipolygon downstream if
    parts are what is wanted.
    """

    geometry: MultiPolygon
    lower: float
    upper: float
    #: The band below the lowest contour and the one above the highest. They
    #: are open-ended in meaning — everything deeper than 8,600 — even though
    #: their bounds here are the surface's own extremes, and a legend that
    #: labels them "8,500–8,600" rather than "below 8,600" is claiming a floor
    #: the data does not have.
    is_open_ended: bool

    @property
    def area(self) -> float:
        """In the analysis frame's units squared. Never in degrees: a
        `GridDefinition` is planar by construction (`05` §2.1)."""
        return float(self.geometry.area)

    @property
    def midpoint(self) -> float:
        """The value a single colour stands for, for a legend swatch."""
        return (self.lower + self.upper) / 2.0


def contour_bands(
    surface: NDArray[np.floating],
    grid: GridDefinition,
    levels: NDArray[np.floating] | None = None,
    *,
    target_count: int = 15,
    smoothing: float = 0.0,
    min_area: float | None = None,
) -> list[ContourBand]:
    """Fill the intervals between contour levels.

    `n` levels give up to `n + 1` bands: one below the lowest level, one
    between each adjacent pair, and one above the highest. **Both ends are
    closed** — the outermost bands run to the surface's own extremes — because
    an unfilled margin is indistinguishable from no-data, which is exactly the
    confusion the extrapolation reporting exists to prevent (`08` §5.2).

    NaN cells stay unfilled. A fault-blanked compartment or an area outside the
    search radius becomes a hole in the band, not ground coloured as though it
    had been interpolated.

    Bands that come out empty are dropped rather than returned with empty
    geometry: a level above everything in the surface produces no area, and a
    feature with no geometry breaks every writer downstream.
    """
    import contourpy

    values = np.asarray(surface, dtype=float)
    if values.shape != (grid.ny, grid.nx):
        raise DegenerateInput(
            f"Surface is {values.shape} but the grid is ({grid.ny}, {grid.nx}). "
            f"A transposed array here produces bands at right angles to the "
            f"structure."
        )
    if not 0.0 <= smoothing <= MAX_SMOOTHING:
        raise DegenerateInput(
            f"Smoothing runs from 0 to {MAX_SMOOTHING}; got {smoothing:g}. Above "
            f"{MAX_SMOOTHING} a band edge drifts measurably off the level it "
            f"claims, and it no longer sits under the contour drawn on it."
        )

    finite = values[np.isfinite(values)]
    if finite.size == 0:
        raise DegenerateInput(
            "The surface is entirely NaN, so there is nothing to fill. Every "
            "cell was outside the search radius or blanked by a fault."
        )
    floor, ceiling = float(finite.min()), float(finite.max())

    if levels is None:
        levels = auto_levels(floor, ceiling, target_count)
    levels = np.sort(np.asarray(levels, dtype=float))

    if min_area is None:
        # One cell. Below that a polygon is not a resolved feature — it is the
        # marching-squares interpolation *inside* a single cell, and a scatter
        # of them around a noisy contour reads as structure.
        min_area = grid.cell_size**2

    # Levels outside the data contribute nothing but empty bands, and a level
    # exactly on an extreme would make a zero-width band at the end.
    interior = levels[(levels > floor) & (levels < ceiling)]
    edges = np.concatenate(([floor], interior, [ceiling]))

    generator = contourpy.contour_generator(
        x=grid.x_coordinates(),
        y=grid.y_coordinates(),
        z=values,
        # Respects NaN: a hole stays a hole rather than being filled across.
        corner_mask=True,
        chunk_size=0,
        # One entry per outer polygon, its offsets delimiting the exterior ring
        # and then its holes — which is exactly a Shapely polygon. The chunked
        # fill types return a different nesting, and code written against one
        # silently mis-reads another.
        fill_type=contourpy.FillType.OuterOffset,
    )

    bands: list[ContourBand] = []
    for position in range(len(edges) - 1):
        lower, upper = float(edges[position]), float(edges[position + 1])
        parts = _polygons(generator.filled(lower, upper), smoothing, min_area)
        if not parts:
            continue
        bands.append(
            ContourBand(
                geometry=MultiPolygon(parts),
                lower=lower,
                upper=upper,
                is_open_ended=position in (0, len(edges) - 2),
            )
        )

    return bands


def _polygons(filled: Any, smoothing: float, min_area: float) -> list[Polygon]:
    """Turn one `filled()` result into valid Shapely polygons.

    `filled` is typed `Any` because contourpy declares it as a union across
    every FillType it supports. Under `OuterOffset` it is two parallel lists —
    one vertex array and one offset array per outer polygon — and narrowing
    here is what lets the rest of this function be typed rather than threading
    `Any` through it. `contour_grid` narrows its line union the same way.
    """
    points: list[NDArray[np.float64]] = [np.asarray(p, dtype=float) for p in filled[0]]
    offsets: list[NDArray[np.int64]] = [np.asarray(o, dtype=np.int64) for o in filled[1]]
    parts: list[Polygon] = []

    for vertices, ring_offsets in zip(points, offsets, strict=True):
        rings = [_ring(vertices[start:end], smoothing) for start, end in pairwise(ring_offsets)]
        shell = rings[0]
        if shell is None:
            continue
        # A hole below the resolution of one cell is the same artefact as a
        # sliver band, and leaving it in punches a speck of basemap through an
        # otherwise solid fill.
        holes = [
            hole for hole in rings[1:] if hole is not None and Polygon(hole).area >= min_area
        ]
        parts.extend(_valid_parts(Polygon(shell, holes), min_area))

    return parts


def _ring(vertices: NDArray[np.float64], smoothing: float) -> NDArray[np.float64] | None:
    """One closed ring, smoothed. `None` when it has too few points to be one."""
    if len(vertices) < 4:
        return None
    if smoothing <= 0:
        return vertices
    return chaikin(np.asarray(vertices, dtype=float), smoothing, closed=True)


def _valid_parts(polygon: Polygon, min_area: float) -> list[Polygon]:
    """Repair a polygon and keep the pieces worth keeping.

    Corner-cutting can pull a ring across itself where a contour nearly
    touches, and Shapely refuses to compute the area of an invalid polygon —
    so the repair is not cosmetic. `make_valid` can split one into several or
    return a collection with lines in it; only polygonal parts survive here.
    """
    repaired: BaseGeometry = polygon if polygon.is_valid else make_valid(polygon)

    candidates: list[BaseGeometry] = (
        [repaired] if isinstance(repaired, Polygon) else list(getattr(repaired, "geoms", []))
    )

    return [
        part
        for part in candidates
        if isinstance(part, Polygon) and not part.is_empty and part.area >= min_area
    ]


__all__ = ["ContourBand", "contour_bands"]
