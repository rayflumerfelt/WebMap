"""Writing features out. `11-file-io.md` §1, §7.

The read side has one entry point per family and so does this: a caller names a
format and gets a file. What varies underneath is which constraints apply, and
`export.py` has already told the user about those before anything reaches here.

**pyogrio for everything OGR can write**, for the reason `11` §1 gives on the
read side — it is vectorised, and a per-feature write loop on a half-million
lease polygons is minutes rather than seconds. Through its *array* API rather
than `write_dataframe`, because the latter takes a GeoDataFrame and geopandas is
a dependency this package deliberately does not have.

CSV is the exception and is written by hand, because OGR's CSV driver wants a
geometry column formatted its way and the thing a geologist actually asks for is
the X and Y columns they gave us back.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from webmap_io.exceptions import UnsupportedFormat
from webmap_io.export import FORMATS, shapefile_field_names


def write_features(
    path: Path,
    *,
    geometry: NDArray[np.object_],
    props: list[dict[str, Any]],
    srid: int,
    fmt: str,
) -> Path:
    """Write features in `fmt`, returning the file written.

    For `shapefile` this writes the `.shp` and its sidecars beside it; zipping
    is `export.zip_shapefile`'s job, because the caller decides what the archive
    is called and where it goes.
    """
    if fmt not in FORMATS:
        raise UnsupportedFormat(
            f"'{fmt}' is not an export format. Available: {', '.join(sorted(FORMATS))}."
        )
    if fmt == "csv":
        return _write_csv(path, geometry=geometry, props=props)
    if fmt == "parquet":
        from webmap_io.parquet import write_features as write_parquet

        write_parquet(path, geometry=geometry, props=props, srid=srid)
        return path
    return _write_ogr(path, geometry=geometry, props=props, srid=srid, fmt=fmt)


#: What each format is called to OGR.
_DRIVERS = {"geojson": "GeoJSON", "gpkg": "GPKG", "shapefile": "ESRI Shapefile"}


def _write_ogr(
    path: Path,
    *,
    geometry: NDArray[np.object_],
    props: list[dict[str, Any]],
    srid: int,
    fmt: str,
) -> Path:
    """Write through pyogrio's array API.

    `pyogrio.raw.write`, not `write_dataframe`: the latter takes a
    GeoDataFrame, and geopandas is a dependency `11` §1 deliberately keeps out
    of this package — the read path returns arrays precisely so that nothing
    downstream needs pandas. The raw call takes the same arrays we already hold.
    """
    import shapely
    from pyogrio.raw import write

    columns = sorted({key for row in props for key in row})
    # Shapefile renames as it writes, so the rename happens *here* rather than
    # inside the driver — that is what lets `plan_export` promise a name and be
    # right about it.
    renames = shapefile_field_names(columns) if fmt == "shapefile" else None

    fields = np.array(
        [renames[column] if renames else column for column in columns], dtype=object
    )
    field_data = [_column(props, column) for column in columns]

    write(
        str(path),
        geometry=shapely.to_wkb(geometry),
        field_data=field_data,
        fields=fields,
        driver=_DRIVERS[fmt],
        crs=f"EPSG:{srid}",
        geometry_type=_geometry_type(geometry),
        # Shapefile holds one geometry type per file, and a layer of polygons
        # with one multipolygon in it is otherwise refused mid-write.
        promote_to_multi=fmt == "shapefile",
    )
    return path


def _column(props: list[dict[str, Any]], column: str) -> NDArray[Any]:
    """One attribute as an array OGR can type.

    Typed by what is in it rather than left as `object`: an object column
    reaches the driver as text, so a numeric field arrives at the recipient as
    strings and every sum they try on it fails.
    """
    values = [row.get(column) for row in props]
    present = [value for value in values if value is not None]

    if present and all(isinstance(value, bool) for value in present):
        return np.array([bool(value) if value is not None else False for value in values])
    if present and all(
        isinstance(value, int | float) and not isinstance(value, bool) for value in present
    ):
        return np.array(
            [float(value) if value is not None else np.nan for value in values],
            dtype=np.float64,
        )
    return np.array(["" if value is None else str(value) for value in values], dtype=object)


def _geometry_type(geometry: NDArray[np.object_]) -> str:
    """The single OGR geometry type for this layer.

    A layer holding more than one is written as the generic type, which
    GeoPackage and GeoJSON accept. Shapefile does not, which is what
    `plan_export`'s `geometry_split` warning is about — the split is the
    caller's to make, before it gets here.
    """
    import shapely

    if len(geometry) == 0:
        return "Unknown"
    names = {shapely.get_type_id(item) for item in geometry}
    single = {
        0: "Point",
        1: "LineString",
        3: "Polygon",
        4: "MultiPoint",
        5: "MultiLineString",
        6: "MultiPolygon",
    }
    if len(names) == 1:
        return single.get(next(iter(names)), "Unknown")
    return "Unknown"


def _write_csv(
    path: Path, *, geometry: NDArray[np.object_], props: list[dict[str, Any]]
) -> Path:
    """Points as X and Y columns; everything else as WKT.

    Points get columns because that is the file a geologist asked for — the one
    they can open in a spreadsheet and hand to somebody who will re-import it.
    A WKT column for a point would be technically complete and practically
    useless.
    """
    import shapely

    columns = sorted({key for row in props for key in row})
    kinds = {shapely.get_type_id(item) for item in geometry} if len(geometry) else set()
    points_only = kinds == {0}

    with path.open("w", encoding="utf-8", newline="") as handle:
        header = (["x", "y"] if points_only else ["geometry"]) + columns
        writer = csv.DictWriter(handle, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        for item, row in zip(geometry, props, strict=True):
            spatial = (
                {"x": shapely.get_x(item), "y": shapely.get_y(item)}
                if points_only
                else {"geometry": shapely.to_wkt(item, rounding_precision=-1)}
            )
            writer.writerow({**spatial, **row})
    return path


__all__ = ["write_features"]
