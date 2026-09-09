"""Grid I/O. `11-file-io.md` §5."""

from pathlib import Path

import numpy as np
import rasterio
from numpy.typing import NDArray
from rasterio.enums import Resampling

from webmap_io._projenv import gdal_env

# Re-exported so callers can name a grid transform without importing
# rasterio — which would initialise GDAL ahead of the PROJ pinning.
Affine = rasterio.Affine

#: Surfer's nodata sentinel. Leaving it in place produces grids whose
#: statistics are dominated by the blank value, and the resulting colour ramp
#: is a single flat colour (`11-file-io.md` §5.1).
SURFER_BLANKING_VALUE = 1.70141e38


def write_cog(
    grid: NDArray[np.float64],
    transform: rasterio.Affine,
    srid: int,
    path: Path,
    nodata: float = float("nan"),
) -> None:
    """Write a Cloud-Optimized GeoTIFF.

    COG is the internal grid format because TiTiler serves it with dynamic
    colormap application — changing a palette becomes a URL parameter change
    rather than a regrid. That is what makes palette editing feel instant.

    Overviews are built at powers of 2 down to ~256 px, using average
    resampling. For a continuous surface, average is correct; nearest would
    make zoomed-out views noisy and misrepresent the surface.
    """
    if grid.ndim != 2:
        raise ValueError(
            f"write_cog expects a 2-D grid; got shape {grid.shape}. Multi-band "
            f"output is not part of the grid model — one dataset is one surface."
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    with gdal_env():
        _write(grid, transform, srid, path, nodata)


def _write(
    grid: NDArray[np.float64],
    transform: rasterio.Affine,
    srid: int,
    path: Path,
    nodata: float,
) -> None:
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "nodata": nodata,
        "width": grid.shape[1],
        "height": grid.shape[0],
        "count": 1,
        "crs": rasterio.CRS.from_epsg(srid),
        "transform": transform,
        "tiled": True,
        "blockxsize": 512,
        "blockysize": 512,
        "compress": "deflate",
        "predictor": 3,  # floating-point predictor
        "BIGTIFF": "IF_SAFER",
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(grid.astype("float32"), 1)
        factors = [2**i for i in range(1, 8) if min(grid.shape) // 2**i >= 256]
        if factors:
            dst.build_overviews(factors, Resampling.average)
            dst.update_tags(ns="rio_overview", resampling="average")


def blanking_to_nan(grid: NDArray[np.float64]) -> NDArray[np.float64]:
    """Convert Surfer's blanking value to NaN.

    Compared with a relative tolerance rather than equality: the sentinel
    round-trips through float32 in some exports and comes back a few ULPs off,
    which an `== 1.70141e38` test misses entirely.
    """
    out = grid.astype(np.float64, copy=True)
    out[np.isclose(out, SURFER_BLANKING_VALUE, rtol=1e-5)] = np.nan
    return out


def grid_transform(west: float, north: float, cell_size: float) -> rasterio.Affine:
    """The affine for a north-up grid anchored at its top-left corner.

    Exists so callers do not import rasterio to build one. Rasters are
    north-up: row 0 is the *top*, so the y axis runs down from `north`.
    Getting that backwards flips the map vertically and looks entirely
    plausible until someone checks a well against it.
    """
    with gdal_env():
        return rasterio.transform.from_origin(west, north, cell_size, cell_size)
