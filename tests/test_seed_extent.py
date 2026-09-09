"""The seed data must be where it claims to be.

This exists because it once was not. An earlier revision picked the seed's
EPSG:2277 northing by inferring it from the projection's false-northing
constant instead of checking it against a known location, and put 2,000
"Midland Basin" control points in central Mexico, 1,200 km south.

Nothing failed. The grids were internally consistent, the registry's
`bbox_4326` agreed with the geometry, every unit test passed, and DuckDB
happily served tiles. The error was invisible until something was overlaid on
a basemap — which is exactly the failure mode `02-data-model.md` §1 describes
and `11-file-io.md` §3 refuses to risk on ingest ("guessing is how data ends
up 300 km from where it belongs").

A CRS test that only round-trips coordinates cannot catch this: a wrong
coordinate round-trips perfectly. The check has to be against ground truth
outside the coordinate system, so these assertions are anchored on real towns.
"""

import numpy as np
import pytest

from webmap_geo.crs import WGS84, transform_bbox, transform_points

# NAD83 / Texas Central (ftUS).
TEXAS_CENTRAL = 2277

#: Towns inside the seeded area, with published coordinates. Ground truth that
#: does not come from our own projection maths.
TOWNS_INSIDE = {
    "Midland": (-102.0779, 31.9973),
    "Odessa": (-102.3676, 31.8457),
    "Big Spring": (-101.4787, 32.2504),
    "Stanton": (-101.7877, 32.1298),
}

#: Far enough outside to catch a gross displacement, close enough that a merely
#: sloppy extent still passes. Guadalajara is roughly where the broken seed
#: landed.
TOWNS_OUTSIDE = {
    "Guadalajara": (-103.3496, 20.6597),
    "Houston": (-95.3698, 29.7604),
    "Denver": (-104.9903, 39.7392),
}


@pytest.fixture(scope="module")
def extent_4326() -> tuple[float, float, float, float]:
    from scripts.seed import ANALYSIS_SRID, EXTENT_FT

    assert ANALYSIS_SRID == TEXAS_CENTRAL
    return transform_bbox(EXTENT_FT, ANALYSIS_SRID, WGS84)


def test_seed_extent_is_in_the_permian_basin(
    extent_4326: tuple[float, float, float, float],
) -> None:
    """A coarse geographic sanity check, before the town-level one.

    West Texas, not Mexico and not the Gulf.
    """
    west, south, east, north = extent_4326

    assert -104.0 < west < -101.0, f"western edge at {west:.2f} is not West Texas"
    assert -104.0 < east < -100.0, f"eastern edge at {east:.2f} is not West Texas"
    assert 30.5 < south < 32.5, f"southern edge at {south:.2f} is not West Texas"
    assert 31.0 < north < 33.5, f"northern edge at {north:.2f} is not West Texas"


@pytest.mark.parametrize(("town", "lonlat"), sorted(TOWNS_INSIDE.items()))
def test_real_towns_fall_inside_the_seed_extent(
    town: str, lonlat: tuple[float, float], extent_4326: tuple[float, float, float, float]
) -> None:
    lon, lat = lonlat
    west, south, east, north = extent_4326

    assert west <= lon <= east and south <= lat <= north, (
        f"{town} ({lon}, {lat}) is outside the seed extent "
        f"({west:.2f}, {south:.2f})-({east:.2f}, {north:.2f}). The seed claims "
        f"to cover the Midland Basin; {town} is in it."
    )


@pytest.mark.parametrize(("town", "lonlat"), sorted(TOWNS_OUTSIDE.items()))
def test_distant_places_fall_outside(
    town: str, lonlat: tuple[float, float], extent_4326: tuple[float, float, float, float]
) -> None:
    lon, lat = lonlat
    west, south, east, north = extent_4326

    assert not (west <= lon <= east and south <= lat <= north), (
        f"{town} ({lon}, {lat}) is inside the seed extent, which is supposed to "
        f"cover a 106 x 76 mile area of West Texas. The extent is far too large "
        f"or in the wrong place."
    )


def test_seeded_geometry_lands_where_the_extent_says() -> None:
    """The generated features, not just the declared rectangle.

    The extent constant being right does not prove the generators use it — an
    off-by-one in a clip or a swapped axis would still produce a plausible
    layer.
    """
    from scripts.seed import EXTENT_FT, control_points

    rng = np.random.default_rng(20260908)
    points = control_points(rng, n=500)
    x = np.asarray(points["x"])
    y = np.asarray(points["y"])

    lon, lat = transform_points(x, y, TEXAS_CENTRAL, WGS84)

    assert lon.min() > -104.0 and lon.max() < -100.0
    assert lat.min() > 30.5 and lat.max() < 33.5
    # And inside the declared rectangle, in native units.
    assert EXTENT_FT[0] <= x.min() and x.max() <= EXTENT_FT[2]
    assert EXTENT_FT[1] <= y.min() and y.max() <= EXTENT_FT[3]
