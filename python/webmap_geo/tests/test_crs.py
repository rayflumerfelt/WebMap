"""Tests for the one module permitted to import pyproj."""

import numpy as np
import pytest

from webmap_geo.crs import (
    WEB_MERCATOR,
    WGS84,
    axis_units,
    frame_for,
    is_geographic,
    transform_bbox,
    transform_points,
)
from webmap_geo.exceptions import NotProjected

# NAD83 / Texas Central (ftUS) — the Midland Basin working CRS from
# 02-data-model.md §1.
TEXAS_CENTRAL = 2277
UTM14N = 32614


def test_geographic_crs_is_refused_an_axis_unit() -> None:
    """The message must name the fix, not just the fault (CLAUDE.md §8)."""
    with pytest.raises(NotProjected) as excinfo:
        axis_units(WGS84)

    message = str(excinfo.value)
    assert "geographic" in message
    assert "State Plane" in message


def test_texas_central_is_us_survey_feet() -> None:
    """US survey feet, not international feet.

    The two differ by 2 ppm. Over the 1.3e6 ft eastings of Texas Central that
    is about 0.8 m — small enough to look like noise and large enough to put a
    well on the wrong side of a lease line.
    """
    assert axis_units(TEXAS_CENTRAL) == "usft"


def test_utm_is_metres() -> None:
    assert axis_units(UTM14N) == "m"


def test_frame_for_carries_srid_and_units() -> None:
    frame = frame_for(TEXAS_CENTRAL)

    assert frame.srid == TEXAS_CENTRAL
    assert frame.units == "usft"
    assert frame.describe() == "EPSG:2277 (usft)"


def test_is_geographic_distinguishes_the_two_display_crss() -> None:
    assert is_geographic(WGS84)
    assert not is_geographic(WEB_MERCATOR)


def test_transform_points_is_lon_lat_not_lat_lon() -> None:
    """always_xy, asserted rather than assumed.

    Without it pyproj honours the EPSG axis order for 4326, which is
    (lat, lon). Midland is near -102 E, 32 N; a swapped result lands in the
    Indian Ocean and nothing raises.
    """
    lon = np.array([-102.0])
    lat = np.array([32.0])

    x, y = transform_points(lon, lat, WGS84, WEB_MERCATOR)

    assert x[0] == pytest.approx(-11_354_000, abs=2_000)
    assert y[0] == pytest.approx(3_763_000, abs=2_000)


def test_identity_transform_returns_input_untouched() -> None:
    """No PROJ round trip, so no sub-millimetre drift on data that never moved."""
    x = np.array([1_500_000.123456])
    y = np.array([6_800_000.654321])

    out_x, out_y = transform_points(x, y, TEXAS_CENTRAL, TEXAS_CENTRAL)

    assert out_x is x
    assert out_y is y


def test_bbox_transform_covers_the_region() -> None:
    """The densified bound must contain every transformed interior point.

    This is the property tile pruning depends on: a bound that under-covers
    silently drops features from tiles.
    """
    bbox = (-102.9, 31.6, -101.4, 32.5)
    bounded = transform_bbox(bbox, WGS84, TEXAS_CENTRAL)

    rng = np.random.default_rng(20260908)
    lon = rng.uniform(bbox[0], bbox[2], 5_000)
    lat = rng.uniform(bbox[1], bbox[3], 5_000)
    x, y = transform_points(lon, lat, WGS84, TEXAS_CENTRAL)

    assert bounded[0] <= x.min() and x.max() <= bounded[2]
    assert bounded[1] <= y.min() and y.max() <= bounded[3]


def test_bbox_transform_is_identity_for_same_crs() -> None:
    bbox = (-102.9, 31.6, -101.4, 32.5)

    assert transform_bbox(bbox, WGS84, WGS84) == bbox
