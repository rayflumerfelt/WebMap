"""Vector tile generation. `06-rendering.md` §7.

The tile query is the thing the Hilbert ordering in `11-file-io.md` §6.1 was
built for, and it is where two silent failures live: a transform that swaps
axes, and a predicate that defeats row-group pruning. Neither raises anything.

Needs MinIO for the object-store path; the arithmetic and axis-order tests run
anywhere.
"""

import math
from pathlib import Path

import pytest

from webmap_geo.exceptions import DegenerateInput
from webmap_geo.tiles import (
    GEOJSON_FEATURE_LIMIT,
    TileRequest,
    geojson_features,
    render_mvt,
    should_use_geojson,
    tile_bounds_3857,
)

TEXAS_CENTRAL = 2277
MIDLAND = (-102.0, 31.74)


def tile_for(lon: float, lat: float, z: int) -> tuple[int, int]:
    return (
        int((lon + 180) / 360 * 2**z),
        int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * 2**z),
    )


# --- arithmetic, no dependencies -------------------------------------------


def test_tile_bounds_cover_the_whole_world_at_zoom_zero() -> None:
    west, south, east, north = tile_bounds_3857(0, 0, 0)
    half = 20_037_508.342789244

    assert west == pytest.approx(-half)
    assert east == pytest.approx(half)
    assert south == pytest.approx(-half)
    assert north == pytest.approx(half)


def test_tiles_tile_the_plane_without_gaps_or_overlap() -> None:
    """Adjacent tiles must share an edge exactly.

    A rounding error here shows as hairline seams between tiles, which reads
    as a rendering bug rather than an arithmetic one.
    """
    left = tile_bounds_3857(3, 2, 5)
    right = tile_bounds_3857(3, 3, 5)
    below = tile_bounds_3857(3, 2, 6)

    assert left[2] == pytest.approx(right[0]), "east edge must equal the next west edge"
    assert left[1] == pytest.approx(below[3]), "south edge must equal the next north edge"


def test_y_increases_southward() -> None:
    """The tile scheme counts y down from the north.

    Inverting this flips the map vertically — plausible-looking, and the sort
    of thing nobody checks until a well is on the wrong side of a fault.
    """
    north_tile = tile_bounds_3857(4, 8, 0)
    south_tile = tile_bounds_3857(4, 8, 15)

    assert north_tile[3] > south_tile[3]


@pytest.mark.parametrize(
    ("z", "x", "y"),
    [(0, 1, 0), (1, 2, 0), (1, 0, 2), (2, -1, 0), (-1, 0, 0), (25, 0, 0)],
)
def test_out_of_range_tiles_are_refused(z: int, x: int, y: int) -> None:
    """Refused at construction, so a bad request never reaches DuckDB."""
    with pytest.raises(DegenerateInput):
        TileRequest("d", "k", TEXAS_CENTRAL, z, x, y)


@pytest.mark.parametrize(
    ("count", "expected"),
    [(0, True), (4_999, True), (5_000, False), (500_000, False), (None, False)],
)
def test_the_geojson_switch_is_at_the_documented_threshold(
    count: int | None, expected: bool
) -> None:
    """`06-rendering.md` §7.1.

    `None` counts as large: a layer whose size we cannot state is not one to
    send whole to a browser.
    """
    assert should_use_geojson(count) is expected
    assert GEOJSON_FEATURE_LIMIT == 5_000


# --- against the seeded object ---------------------------------------------

pytest_integration = pytest.mark.integration


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, object]:
    """Write a small layer to MinIO and return its key, or skip."""
    import numpy as np
    import shapely

    from tests.fixtures.build import EXTENT
    from webmap_geo.dataplane import ObjectStore
    from webmap_io.parquet import write_features
    from webmap_io.storage import StorageConfig, client, ensure_bucket, put_file

    config = StorageConfig(
        endpoint="http://localhost:9000",
        bucket="webmap-test",
        access_key="minioadmin",
        secret_key="minioadmin",
    )
    try:
        s3 = client(config)
        ensure_bucket(s3, config.bucket)
    except Exception as exc:
        pytest.skip(
            f"No MinIO at {config.endpoint} ({type(exc).__name__}). Start it "
            f"with: docker compose -f infra/compose.yaml up -d minio"
        )

    rng = np.random.default_rng(11)
    geometry = shapely.points(
        rng.uniform(EXTENT[0], EXTENT[2], 400), rng.uniform(EXTENT[1], EXTENT[3], 400)
    )
    path = Path(tmp_path_factory.mktemp("tiles")) / "features.parquet"
    write_features(
        path,
        geometry=np.asarray(geometry, dtype=object),
        props=[{"well": f"W{i}", "porosity": 8.0 + i % 10} for i in range(400)],
        srid=TEXAS_CENTRAL,
    )
    key = "features/ds_tiletest/v1.parquet"
    put_file(s3, config.bucket, key, path)

    store = ObjectStore(
        endpoint="localhost:9000",
        access_key="minioadmin",
        secret_key="minioadmin",
        use_ssl=False,
    )
    return f"s3://{config.bucket}/{key}", store


@pytest_integration
def test_a_tile_over_the_data_has_content(seeded: tuple[str, object]) -> None:
    key, store = seeded
    z = 10
    x, y = tile_for(*MIDLAND, z)

    data = render_mvt(TileRequest("d", key, TEXAS_CENTRAL, z, x, y), store)  # type: ignore[arg-type]

    assert len(data) > 100, "a tile covering the layer should carry features"


@pytest_integration
def test_a_tile_away_from_the_data_is_empty_not_a_header(
    seeded: tuple[str, object],
) -> None:
    """`ST_AsMVT` over an empty set still returns a ~28-byte tile.

    That is a valid tile saying "nothing here" in the most expensive way
    available, and it defeats any cache that keys on emptiness. The count
    comes back alongside the tile so an empty result is genuinely empty.
    """
    key, store = seeded

    data = render_mvt(TileRequest("d", key, TEXAS_CENTRAL, 10, 1, 1), store)  # type: ignore[arg-type]

    assert data == b""


@pytest_integration
def test_geojson_is_longitude_latitude_not_the_epsg_axis_order(
    seeded: tuple[str, object],
) -> None:
    """The bug this test exists for, found by running the code.

    DuckDB honours EPSG's declared axis order, and EPSG defines 4326 as
    (latitude, longitude). Without `always_xy` a Midland Basin point comes
    back as `POINT (31.17 -102.88)` — the Indian Ocean — and nothing raises.
    RFC 7946 requires (longitude, latitude).

    The same trap already exists in pyproj, where `webmap_geo.crs.transformer`
    guards it. Two engines, one mistake; assume the next one has it too.
    """
    key, store = seeded

    features = geojson_features(key, TEXAS_CENTRAL, store, limit=20)["features"]  # type: ignore[arg-type]

    for feature in features:
        lon, lat = feature["geometry"]["coordinates"]
        assert -104 < lon < -100, f"longitude {lon} is not West Texas — axes swapped?"
        assert 30.5 < lat < 33.5, f"latitude {lat} is not West Texas — axes swapped?"


@pytest_integration
def test_geojson_carries_attributes_and_stable_ids(
    seeded: tuple[str, object],
) -> None:
    """`id` is the edit identity of a feature (`02-data-model.md` §3.5.1)."""
    key, store = seeded

    features = geojson_features(key, TEXAS_CENTRAL, store, limit=5)["features"]  # type: ignore[arg-type]

    assert len(features) == 5
    assert all(f["id"] is not None for f in features)
    assert {"well", "porosity"} <= set(features[0]["properties"])


@pytest_integration
def test_the_geojson_limit_is_honoured(seeded: tuple[str, object]) -> None:
    """A layer above the threshold must not be sent whole by accident."""
    key, store = seeded

    assert len(geojson_features(key, TEXAS_CENTRAL, store, limit=7)["features"]) == 7  # type: ignore[arg-type]


@pytest_integration
def test_neighbouring_tiles_together_cover_every_feature(
    seeded: tuple[str, object],
) -> None:
    """No feature falls between tiles.

    The predicate is a bbox test in storage CRS against a *transformed* tile
    envelope. If that transform under-covers — the reason `transform_bbox`
    densifies rather than transforming four corners — features drop out at
    tile edges, and the only symptom is a gap nobody attributes to projection.
    """
    key, store = seeded
    z = 8
    cx, cy = tile_for(*MIDLAND, z)

    covered = sum(
        len(render_mvt(TileRequest("d", key, TEXAS_CENTRAL, z, cx + dx, cy + dy), store))  # type: ignore[arg-type]
        > 0
        for dx in (-1, 0, 1)
        for dy in (-1, 0, 1)
    )

    assert covered >= 1, "the layer must appear in at least one tile at zoom 8"
