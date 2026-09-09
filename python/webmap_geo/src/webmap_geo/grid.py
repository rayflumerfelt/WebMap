"""The regular grid every part of this package works over.

`05-geoprocessing.md` §6. **At the package root, not under `interpolate`**:
faults rasterise onto it, contours are extracted from it, and interpolation
writes into it. Living under one of those three made `faults` import
`interpolate` and `interpolate` import `faults`, which is a cycle — and the
cycle was the signal that the grid is a shared value object rather than an
interpolation concept.

A `GridDefinition` is planar and in the analysis frame — it does not know
about EPSG:4326, and converting a `GridSpec.bbox` from WGS84 into these bounds
is one of the two reprojections `05` §2.2 permits, done by the caller before
this object exists.

**Row order is north-up.** Row 0 is the northern edge, which is what GeoTIFF
and COG expect and what every raster reader assumes. Getting it upside down
produces a map that looks like a plausible surface reflected about its
centre — and nothing errors.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from webmap_geo.exceptions import DegenerateInput
from webmap_geo.frame import AnalysisFrame

#: A million cells is a 1000x1000 grid, which `05` §10 puts at the top of the
#: interactive range. Above this a job is expected; a synchronous request for
#: one would occupy a worker for minutes.
SOFT_CELL_LIMIT = 4_000_000


@dataclass(frozen=True)
class GridDefinition:
    """A regular grid in the analysis frame.

    `xmin`/`ymin` are the **cell centre** of the south-west cell, not the
    corner of the extent. Confusing the two shifts every value by half a cell,
    which is invisible on a smooth surface and shows up as a systematic offset
    when the grid is compared against the control points that made it.
    """

    xmin: float
    ymin: float
    cell_size: float
    nx: int
    ny: int
    frame: AnalysisFrame

    def __post_init__(self) -> None:
        if self.cell_size <= 0:
            raise DegenerateInput(
                f"Cell size must be positive; got {self.cell_size:g} {self.frame.units}."
            )
        if self.nx < 2 or self.ny < 2:
            raise DegenerateInput(
                f"A grid needs at least 2x2 cells; got {self.nx}x{self.ny}. A "
                f"single row or column has no surface to interpolate."
            )
        if self.n_cells > SOFT_CELL_LIMIT:
            raise DegenerateInput(
                f"Requested {self.n_cells:,} grid cells (limit "
                f"{SOFT_CELL_LIMIT:,}). A {self.cell_size:g} {self.frame.units} "
                f"cell over this extent gives {self.n_cells:,}; "
                f"{self.cell_size * 2:g} gives {self.n_cells // 4:,}."
            )

    @property
    def n_cells(self) -> int:
        return self.nx * self.ny

    @property
    def xmax(self) -> float:
        return self.xmin + (self.nx - 1) * self.cell_size

    @property
    def ymax(self) -> float:
        return self.ymin + (self.ny - 1) * self.cell_size

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """Cell-centre bounds: (xmin, ymin, xmax, ymax)."""
        return (self.xmin, self.ymin, self.xmax, self.ymax)

    def x_coordinates(self) -> NDArray[np.float64]:
        return np.asarray(self.xmin + np.arange(self.nx) * self.cell_size, dtype=np.float64)

    def y_coordinates(self) -> NDArray[np.float64]:
        """North to south, so row 0 is the northern edge.

        The order GeoTIFF and COG expect. An ascending array here produces a
        raster that reads as a plausible surface reflected about its centre,
        and nothing about it errors.
        """
        return np.asarray(self.ymax - np.arange(self.ny) * self.cell_size, dtype=np.float64)

    def cell_centres(self) -> NDArray[np.float64]:
        """Every cell centre as an (ny*nx, 2) array, row-major from the north.

        Flattened in the same order `numpy.ravel` uses on a (ny, nx) array, so
        a result can be reshaped without any index arithmetic — which is where
        a transposed grid usually comes from.
        """
        xs, ys = np.meshgrid(self.x_coordinates(), self.y_coordinates())
        return np.asarray(np.column_stack([xs.ravel(), ys.ravel()]), dtype=np.float64)

    def transform(self) -> tuple[float, float, float, float, float, float]:
        """An affine transform for a GeoTIFF, in rasterio's coefficient order.

        Written from the extent's *corner*, not the cell centre — a GeoTIFF's
        origin is the outer edge of the first pixel, and the half-cell
        difference is the offset that makes a grid disagree with the points
        that produced it.
        """
        half = self.cell_size / 2.0
        return (
            self.cell_size,
            0.0,
            self.xmin - half,
            0.0,
            -self.cell_size,
            self.ymax + half,
        )

    @classmethod
    def covering(
        cls,
        bounds: tuple[float, float, float, float],
        cell_size: float,
        frame: AnalysisFrame,
        *,
        margin_cells: int = 0,
    ) -> GridDefinition:
        """A grid covering `bounds` (xmin, ymin, xmax, ymax) in the frame.

        Rounded outward so the requested extent is fully inside; a grid that
        stops short of the data would leave control points outside the surface
        they produced.
        """
        xmin, ymin, xmax, ymax = bounds
        if not (xmax > xmin and ymax > ymin):
            raise DegenerateInput(
                f"Grid bounds are empty or inverted: "
                f"({xmin:g}, {ymin:g}) to ({xmax:g}, {ymax:g}). Order is "
                f"(xmin, ymin, xmax, ymax) in {frame.describe()}."
            )

        nx = int(np.ceil((xmax - xmin) / cell_size)) + 1 + 2 * margin_cells
        ny = int(np.ceil((ymax - ymin) / cell_size)) + 1 + 2 * margin_cells
        return cls(
            xmin=xmin - margin_cells * cell_size,
            ymin=ymin - margin_cells * cell_size,
            cell_size=cell_size,
            nx=nx,
            ny=ny,
            frame=frame,
        )

    def describe(self) -> str:
        """For a lineage record and a figure caption (`04` §6.1)."""
        return (
            f"{self.cell_size:g} {self.frame.units} cells, "
            f"{self.nx} x {self.ny} ({self.n_cells:,} cells)"
        )


__all__ = ["SOFT_CELL_LIMIT", "GridDefinition"]
