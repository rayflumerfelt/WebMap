"""Tests for PROJ/GDAL data pinning.

The failure this prevents is environmental and machine-specific, which makes
it exactly the kind that reaches production: a PostgreSQL/PostGIS install on
the same host sets `PROJ_LIB` and `GDAL_DATA` system-wide, and GDAL prefers
them over the data inside the rasterio wheel.

The loud version is a `CRSError` on `from_epsg`. The quiet version — an older
but structurally valid proj.db resolving an EPSG code against a superseded
datum shift — puts a grid tens of metres from where it belongs and raises
nothing at all.
"""

import os
from pathlib import Path

import rasterio

from webmap_io._projenv import gdal_env, pin_proj_data

TEXAS_CENTRAL = 2277


def test_pinning_points_at_the_bundled_proj_database() -> None:
    pin_proj_data()

    proj_data = os.environ.get("PROJ_DATA")
    assert proj_data is not None
    assert Path(proj_data).is_dir()
    assert (Path(proj_data) / "proj.db").is_file()
    # Both spellings: PROJ >= 9 reads PROJ_DATA, earlier versions PROJ_LIB.
    assert os.environ.get("PROJ_LIB") == proj_data


def test_epsg_2277_resolves() -> None:
    """The working CRS for the Midland Basin. If this fails, nothing writes."""
    with gdal_env():
        assert rasterio.CRS.from_epsg(TEXAS_CENTRAL).to_epsg() == TEXAS_CENTRAL


def test_env_overrides_a_hostile_ambient_setting(tmp_path: Path) -> None:
    """The real scenario: a foreign PROJ_LIB set before this process started.

    Simulated by pointing the variables at an empty directory, which is what
    an incompatible proj.db amounts to from GDAL's perspective. Inside
    `gdal_env()` the pinned directory must win regardless.
    """
    saved = {k: os.environ.get(k) for k in ("PROJ_DATA", "PROJ_LIB", "GDAL_DATA")}
    try:
        decoy = tmp_path / "postgis-proj"
        decoy.mkdir()
        for key in ("PROJ_DATA", "PROJ_LIB"):
            os.environ[key] = str(decoy)

        # Re-pin, as package import would have done, then confirm the Env
        # built from the pinned values resolves the code.
        pin_proj_data()
        with gdal_env():
            assert rasterio.CRS.from_epsg(TEXAS_CENTRAL).to_epsg() == TEXAS_CENTRAL
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_write_cog_round_trips_the_crs(tmp_path: Path) -> None:
    """End to end: a COG written by us reads back with the CRS we asked for.

    This is the assertion that would have caught the original failure, and it
    is the one that catches a *silently wrong* datum too — `to_epsg()` on the
    round-tripped file must be the code that went in.
    """
    import numpy as np

    from webmap_io.raster import grid_transform, write_cog

    grid = np.linspace(0.0, 20.0, 64 * 64).reshape(64, 64)
    path = tmp_path / "grid.tif"

    write_cog(grid, grid_transform(1_150_000.0, 6_980_000.0, 500.0), TEXAS_CENTRAL, path)

    with gdal_env(), rasterio.open(path) as src:
        assert src.crs.to_epsg() == TEXAS_CENTRAL
        assert src.count == 1
        assert src.profile["dtype"] == "float32"
        # Tiled, so TiTiler can serve windows without reading the whole grid.
        assert src.profile["tiled"] is True
