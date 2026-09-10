"""GeoParquet reading and writing for the feature data plane.

`02-data-model.md` §3.5.1 defines the object layout; `11-file-io.md` §6.1
defines how it must be written. The sort is the part that matters:

    Parquet prunes by row-group statistics, and the tile query in
    06 §7 filters on the bbox columns. Features in file order have
    row-group bboxes covering the whole layer, so nothing prunes and
    every tile reads everything.

So features are ordered on a Hilbert index of their centroid before writing,
which makes row groups spatially compact and the pruning actually work. This
is the single highest-leverage decision in the ingest path, and it fails
silently when omitted — the layer is correct, just slow, on every tile
request forever.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import shapely
from numpy.typing import NDArray
from shapely import GeometryType

from webmap_geo.hilbert import hilbert_index

#: 128 MB row groups, ZSTD. Larger groups compress better and prune worse;
#: this is the balance point for the tile query at our layer sizes
#: (`11-file-io.md` §6.1).
TARGET_ROW_GROUP_BYTES = 128 * 1024 * 1024

#: Bounds on rows per group, so a pathological row size cannot produce either
#: one row group for the whole layer or a group per feature.
MIN_ROWS_PER_GROUP = 2_048
MAX_ROWS_PER_GROUP = 1_000_000

_GEOMETRY_TYPE_NAMES = {
    GeometryType.POINT: "Point",
    GeometryType.LINESTRING: "LineString",
    GeometryType.POLYGON: "Polygon",
    GeometryType.MULTIPOINT: "MultiPoint",
    GeometryType.MULTILINESTRING: "MultiLineString",
    GeometryType.MULTIPOLYGON: "MultiPolygon",
    GeometryType.GEOMETRYCOLLECTION: "GeometryCollection",
}


@dataclass(frozen=True)
class WriteResult:
    path: Path
    feature_count: int
    bbox: tuple[float, float, float, float]
    row_groups: int


def write_features(
    path: Path,
    *,
    geometry: NDArray[np.object_],
    props: list[dict[str, Any]],
    srid: int,
    ids: NDArray[np.int64] | None = None,
    updated_at: NDArray[np.datetime64] | None = None,
) -> WriteResult:
    """Write one GeoParquet object for a dataset version.

    Objects are immutable and whole-object: there is no incremental append.
    An edit produces a new version (`adr/0005-single-editor-persistence.md`),
    and the 5,000-feature viewport cap in `09-editing.md` §17 is what keeps
    that cheap.
    """
    if len(geometry) != len(props):
        raise ValueError(
            f"{len(geometry)} geometries and {len(props)} property records. "
            f"They describe the same features and must be the same length."
        )
    if len(geometry) == 0:
        raise ValueError(
            f"Refusing to write an empty feature object to {path.name}. An "
            f"ingest that dropped every feature is a validation failure, not "
            f"a zero-feature dataset — check the source CRS and geometry "
            f"validity warnings."
        )

    bounds = shapely.bounds(geometry)  # (n, 4): xmin, ymin, xmax, ymax
    layer_bbox = (
        float(np.nanmin(bounds[:, 0])),
        float(np.nanmin(bounds[:, 1])),
        float(np.nanmax(bounds[:, 2])),
        float(np.nanmax(bounds[:, 3])),
    )

    # Sort on the Hilbert index of the *centre of the bounding box* rather
    # than the true centroid. For a point layer they are identical; for lines
    # and polygons the bbox centre is what the pruning predicate compares
    # against, so ordering by it clusters exactly the quantity being filtered.
    order = _spatial_order(bounds, layer_bbox)

    n = len(geometry)
    ids_array = np.arange(1, n + 1, dtype=np.int64) if ids is None else ids
    if updated_at is None:
        updated_at = np.full(n, np.datetime64("now", "us"), dtype="datetime64[us]")

    table = pa.table(
        {
            "id": pa.array(ids_array[order]),
            "geometry": pa.array(shapely.to_wkb(geometry[order]), type=pa.binary()),
            "props": pa.array(
                [json.dumps(props[i], separators=(",", ":")) for i in order],
                type=pa.string(),
            ),
            "updated_at": pa.array(updated_at[order], type=pa.timestamp("us")),
            # The covering column the tile predicate filters on. A struct of
            # four doubles, so Parquet writes per-row-group min/max for each
            # and DuckDB can skip a group without decoding it.
            "bbox": pa.StructArray.from_arrays(
                [
                    pa.array(bounds[order, 0], type=pa.float64()),
                    pa.array(bounds[order, 1], type=pa.float64()),
                    pa.array(bounds[order, 2], type=pa.float64()),
                    pa.array(bounds[order, 3], type=pa.float64()),
                ],
                names=["xmin", "ymin", "xmax", "ymax"],
            ),
        }
    )
    table = table.replace_schema_metadata(
        {"geo": json.dumps(_geo_metadata(geometry, srid, layer_bbox))}
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        table,
        path,
        compression="zstd",
        row_group_size=_rows_per_group(table),
        # Statistics are what pruning reads. Off, the sort buys nothing.
        write_statistics=True,
        version="2.6",
    )

    return WriteResult(
        path=path,
        feature_count=n,
        bbox=layer_bbox,
        row_groups=pq.ParquetFile(path).num_row_groups,
    )


def _spatial_order(
    bounds: NDArray[np.float64], layer_bbox: tuple[float, float, float, float]
) -> NDArray[np.intp]:
    centre_x = (bounds[:, 0] + bounds[:, 2]) / 2.0
    centre_y = (bounds[:, 1] + bounds[:, 3]) / 2.0
    return np.argsort(hilbert_index(centre_x, centre_y, layer_bbox), kind="stable")


def _rows_per_group(table: pa.Table) -> int:
    """Rows that approximate TARGET_ROW_GROUP_BYTES for this table's shape.

    pyarrow sizes row groups in rows, not bytes, so the target is converted
    using the in-memory width of a row. That over-estimates the on-disk size
    because ZSTD has not run yet, which errs toward smaller groups — the safe
    direction for pruning.
    """
    if table.num_rows == 0:
        return MIN_ROWS_PER_GROUP
    bytes_per_row = max(1, table.nbytes // table.num_rows)
    return int(
        np.clip(
            TARGET_ROW_GROUP_BYTES // bytes_per_row,
            MIN_ROWS_PER_GROUP,
            MAX_ROWS_PER_GROUP,
        )
    )


def _geo_metadata(
    geometry: NDArray[np.object_], srid: int, bbox: tuple[float, float, float, float]
) -> dict[str, Any]:
    """GeoParquet 1.1 file metadata.

    The CRS travels with the object, so a `.parquet` handed to someone else is
    readable without this database — which was not true of a row in a
    per-dataset feature table (`02-data-model.md` §3.5.1).
    """
    present = {
        _GEOMETRY_TYPE_NAMES[GeometryType(t)]
        for t in np.unique(shapely.get_type_id(geometry))
        if GeometryType(t) in _GEOMETRY_TYPE_NAMES
    }
    return {
        "version": "1.1.0",
        "primary_column": "geometry",
        "columns": {
            "geometry": {
                "encoding": "WKB",
                "geometry_types": sorted(present),
                "crs": _projjson(srid),
                "bbox": list(bbox),
                # Names the covering column so a reader knows the bbox struct
                # is authoritative rather than an ordinary attribute.
                "covering": {
                    "bbox": {
                        "xmin": ["bbox", "xmin"],
                        "ymin": ["bbox", "ymin"],
                        "xmax": ["bbox", "xmax"],
                        "ymax": ["bbox", "ymax"],
                    }
                },
            }
        },
    }


def _projjson(srid: int) -> dict[str, Any]:
    # Imported here rather than at module scope: pyproj belongs to
    # webmap_geo.crs, and this is the one place webmap_io needs a CRS
    # serialisation rather than a transformation.
    from webmap_geo.crs import crs_of

    return dict(json.loads(crs_of(srid).to_json()))


def row_group_bounds(path: Path) -> list[tuple[float, float, float, float]]:
    """Per-row-group bounding boxes, read from Parquet statistics.

    This is how the pruning property is asserted in tests: it reads exactly
    what a query engine reads when deciding whether to skip a group, rather
    than trusting an EXPLAIN plan whose format changes between releases.
    """
    parquet = pq.ParquetFile(path)
    # Struct fields are flattened into leaf columns addressed by dotted path.
    # Locate them by name rather than by index — the column order is the
    # writer's business and has changed between pyarrow releases.
    first_group = parquet.metadata.row_group(0)
    wanted = {"bbox.xmin", "bbox.ymin", "bbox.xmax", "bbox.ymax"}
    columns = {
        first_group.column(i).path_in_schema: i
        for i in range(first_group.num_columns)
        if first_group.column(i).path_in_schema in wanted
    }
    if set(columns) != wanted:
        raise ValueError(
            f"{path.name} has no bbox covering column (found "
            f"{sorted(columns) or 'none'}). It was not written by "
            f"write_features, and the tile predicate in 06 §7 will not prune "
            f"against it."
        )

    boxes: list[tuple[float, float, float, float]] = []
    for group in range(parquet.num_row_groups):
        meta = parquet.metadata.row_group(group)
        stats = {name: meta.column(i).statistics for name, i in columns.items()}
        if any(s is None or not s.has_min_max for s in stats.values()):
            raise ValueError(
                f"Row group {group} of {path.name} has no bbox statistics. "
                f"Pruning cannot work without them — the object was written "
                f"with write_statistics disabled."
            )
        boxes.append(
            (
                float(stats["bbox.xmin"].min),
                float(stats["bbox.ymin"].min),
                float(stats["bbox.xmax"].max),
                float(stats["bbox.ymax"].max),
            )
        )
    return boxes
