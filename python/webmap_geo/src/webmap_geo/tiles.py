"""Vector tile generation from GeoParquet. `06-rendering.md` §7.

MVT is generated **in-process** from the dataset's current GeoParquet object
via DuckDB. No tile service, no build step — which still matters, because
layers are edited and a tile must reflect the current version the moment the
version pointer advances (`adr/0002-duckdb-data-plane.md`).

This lives in `webmap_geo` rather than in the API. `06` §7 shows it under
`apps/api/services/tiles.py`, but `adr/0004-geoprocessing-owns-geometry.md`
moved tile geometry preparation — simplification, clipping, quantization —
out of a database function and into this package. The dividing line is
coordinates: this reads and writes geometry, so it belongs here. The API
orchestrates.
"""

from dataclasses import dataclass
from typing import Any

from webmap_geo.crs import WEB_MERCATOR, transform_bbox
from webmap_geo.dataplane import ObjectStore, connect
from webmap_geo.exceptions import DegenerateInput

#: MVT coordinate space. 4096 is the near-universal choice and what MapLibre
#: assumes when a layer omits it.
EXTENT = 4096

#: `always_xy := true` on every DuckDB transform, for the same reason
#: `webmap_geo.crs.transformer` passes it to pyproj. EPSG defines 4326 as
#: (latitude, longitude); every file format and map API here uses
#: (longitude, latitude). Without the flag DuckDB honours EPSG and silently
#: swaps them — verified: a Midland Basin point comes back as
#: POINT (31.17 -102.88), which is in the Indian Ocean and raises nothing.
#: Two engines now, the same trap; assume any new one has it too.
ALWAYS_XY = "always_xy := true"

#: Buffer in tile units. Geometry is clipped to the tile plus this margin so
#: that a line crossing the edge still has the vertex outside it — without a
#: buffer, wide strokes and labels get cut exactly at the seam and the join
#: between two tiles is visible.
BUFFER = 64

#: `06-rendering.md` §7.1. Below this a layer is served as GeoJSON: editable
#: in place, instant style updates, no tile round-trip. Above it, anything but
#: MVT stalls the main thread.
GEOJSON_FEATURE_LIMIT = 5_000


@dataclass(frozen=True)
class TileRequest:
    dataset_id: str
    parquet_key: str
    storage_srid: int
    z: int
    x: int
    y: int
    layer_name: str = "features"

    def __post_init__(self) -> None:
        if self.z < 0 or self.z > 24:
            raise DegenerateInput(
                f"Zoom {self.z} is outside the tile scheme (0-24). Web Mercator "
                f"tiles beyond 24 are below millimetre resolution."
            )
        limit = 1 << self.z
        if not (0 <= self.x < limit and 0 <= self.y < limit):
            raise DegenerateInput(
                f"Tile {self.z}/{self.x}/{self.y} is outside the grid — at zoom "
                f"{self.z} the valid range is 0..{limit - 1} in each axis."
            )


def render_mvt(request: TileRequest, store: ObjectStore | None = None) -> bytes:
    """Produce one Mapbox Vector Tile, or empty bytes if nothing intersects.

    Two things carry the performance here, and both were defects in the
    previous PostGIS design (`06` §7):

    - **The filter is on the stored bbox columns**, not on a transformed
      geometry. GeoParquet writes per-row-group bounding boxes; a predicate
      over them lets DuckDB skip row groups without decoding them. Wrapping
      the geometry in `ST_Transform` inside the predicate defeats every
      statistic and reads the whole layer per tile.
    - **The tile envelope is transformed once, into storage CRS**, rather than
      transforming every feature into 3857 before comparing.

    An empty result returns `b""` rather than an empty tile: MapLibre treats a
    204 or a zero-length body as "nothing here", and an encoded empty tile
    costs bytes to say the same thing.
    """
    envelope = tile_bounds_3857(request.z, request.x, request.y)
    # Densified rather than corner-transformed — a curved edge under an
    # oblique projection under-covers, and features vanish from tiles with
    # nothing logged. See `webmap_geo.crs.transform_bbox`.
    west, south, east, north = transform_bbox(envelope, WEB_MERCATOR, request.storage_srid)

    # The feature count comes back alongside the tile because `ST_AsMVT` over
    # an empty set still returns a ~28-byte tile carrying a layer header. That
    # is a valid tile saying "nothing here" in the most expensive way
    # available, and it defeats any caching that keys on emptiness.
    sql = f"""
        SELECT ST_AsMVT(t, $layer, {EXTENT}, 'geom'), count(*)
        FROM (
            SELECT
                id,
                ST_AsMVTGeom(
                    ST_Transform(
                        geometry, $storage_crs, 'EPSG:{WEB_MERCATOR}', {ALWAYS_XY}
                    ),
                    ST_Extent(ST_TileEnvelope($z, $x, $y)),
                    {EXTENT}, {BUFFER}, true
                ) AS geom,
                props
            FROM read_parquet($parquet_key)
            WHERE bbox.xmin <= $env_xmax AND bbox.xmax >= $env_xmin
              AND bbox.ymin <= $env_ymax AND bbox.ymax >= $env_ymin
        ) AS t
        WHERE t.geom IS NOT NULL
    """
    params = {
        "layer": request.layer_name,
        "storage_crs": f"EPSG:{request.storage_srid}",
        "z": request.z,
        "x": request.x,
        "y": request.y,
        "parquet_key": request.parquet_key,
        "env_xmin": west,
        "env_ymin": south,
        "env_xmax": east,
        "env_ymax": north,
    }

    with connect(store) as conn:
        row = conn.execute(sql, params).fetchone()

    if row is None or row[0] is None or row[1] == 0:
        return b""
    return bytes(row[0])


def geojson_features(
    parquet_key: str,
    storage_srid: int,
    store: ObjectStore | None = None,
    *,
    limit: int = GEOJSON_FEATURE_LIMIT,
) -> dict[str, Any]:
    """The whole layer as GeoJSON, for layers small enough to send.

    RFC 7946 says GeoJSON is WGS84 **in (longitude, latitude) order**, so the
    transform carries `always_xy`. That is one of the defined reprojection
    boundaries — before display — not an ad-hoc conversion
    (`02-data-model.md` §1 rule 3).
    """
    import json

    sql = f"""
        SELECT id,
               ST_AsGeoJSON(
                   ST_Transform(geometry, $storage_crs, 'EPSG:4326', {ALWAYS_XY})
               ) AS geom,
               props
        FROM read_parquet($parquet_key)
        LIMIT $limit
    """
    with connect(store) as conn:
        rows = conn.execute(
            sql,
            {
                "storage_crs": f"EPSG:{storage_srid}",
                "parquet_key": parquet_key,
                "limit": limit,
            },
        ).fetchall()

    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": row[0],
                "geometry": json.loads(row[1]) if row[1] else None,
                "properties": json.loads(row[2]) if row[2] else {},
            }
            for row in rows
        ],
    }


def tile_bounds_3857(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """Web Mercator bounds of a tile, in metres.

    Computed here rather than asked of DuckDB because the result feeds
    `transform_bbox`, which is Python. Doing it in SQL would mean a round trip
    to learn something arithmetic.
    """
    # Half the circumference at the equator; the Web Mercator square runs from
    # -R to +R in both axes.
    half = 20_037_508.342789244
    span = (half * 2) / (1 << z)
    west = -half + x * span
    north = half - y * span
    return (west, north - span, west + span, north)


def should_use_geojson(feature_count: int | None) -> bool:
    """`06-rendering.md` §7.1: the automatic switch.

    Decided server-side and expressed in the style, so the client honours what
    the style says rather than making the same judgement separately and
    disagreeing about it.

    An unknown count is treated as large. A layer whose size we cannot state
    is not one to send whole to a browser.
    """
    if feature_count is None:
        return False
    return feature_count < GEOJSON_FEATURE_LIMIT
