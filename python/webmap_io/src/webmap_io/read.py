"""Vector readers. `11-file-io.md` §3.

pyogrio rather than fiona: GDAL bindings with a vectorized read path, 5-10x
faster on large layers, returning arrays rather than per-feature dicts.

**The CRS rule is the important thing in this module.** Resolution order is
the file's declared CRS, then a sidecar `.prj`, then *fail*. Never guess.
Guessing is how data ends up 300 km from where it belongs, and the error is
invisible until someone overlays it on a basemap — which this project has
already done to itself once (`tests/test_seed_extent.py`).
"""

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pyogrio
import shapely
from numpy.typing import NDArray

from webmap_io.exceptions import MissingCRS, UnsupportedFormat

#: Shapefiles store the CRS in a companion file. Everything else embeds it.
SIDECAR_SUFFIX = ".prj"

#: And the attribute encoding in another one. Absent, the encoding is
#: genuinely unknown — a .dbf carries no marker (`11-file-io.md` §4.1).
ENCODING_SIDECAR_SUFFIX = ".cpg"

#: Offered in the error when decoding fails. Not tried automatically:
#: cp1252 and cp850 decode the same bytes to different letters, so
#: guessing produces text that is wrong rather than text that is missing,
#: and nothing downstream can tell.
COMMON_ENCODINGS = ("cp1252", "ISO-8859-1", "cp850", "utf-8")

#: Shapely geometry type ids collapsed to the four `geometry_kind_t` values
#: (`02-data-model.md` §3.1). Single and multi variants are the same kind: a
#: layer of MultiPolygons is a polygon layer to a geologist.
_GEOMETRY_KINDS: dict[int, str] = {
    0: "point",
    4: "point",
    1: "linestring",
    2: "linestring",  # LinearRing
    5: "linestring",
    3: "polygon",
    6: "polygon",
}


@dataclass(frozen=True)
class ReadResult:
    """What a reader produces, before anything is written.

    Geometry as a Shapely array and attributes as plain Python columns —
    deliberately not a GeoDataFrame, so `webmap_io` does not put geopandas in
    the dependency graph of everything that reads a file.
    """

    geometry: NDArray[np.object_]
    attributes: dict[str, list[Any]]
    srid: int
    geometry_kind: str
    warnings: list[str] = field(default_factory=list)

    @property
    def feature_count(self) -> int:
        return len(self.geometry)

    def props(self) -> list[dict[str, Any]]:
        """Attributes as per-feature dicts, the shape `write_features` wants."""
        keys = list(self.attributes)
        return [
            {key: self.attributes[key][i] for key in keys} for i in range(self.feature_count)
        ]


def read_vector(
    path: Path,
    layer: str | None = None,
    *,
    srid_override: int | None = None,
    encoding: str | None = None,
) -> ReadResult:
    """Read any OGR-supported vector format.

    CRS RESOLUTION, in order:
      1. The file's declared CRS (`.prj` for shapefile, embedded elsewhere)
      2. A sidecar `.prj` if the format lacks embedded CRS
      3. FAIL — never guess.

    `srid_override` is the third option the error message offers: a user who
    genuinely knows the CRS can state it. It is an explicit act, not a
    fallback, and it is never inferred from the data.

    `encoding` is the same shape of escape for attribute text. Resolution
    is the `.cpg` sidecar, then UTF-8, then *ask* — see `_read_table`.
    """
    if not path.exists():
        raise UnsupportedFormat(
            f"{path} does not exist. For a shapefile, check that the upload "
            f"included every component — a bare .shp is not readable without "
            f"its .shx and .dbf."
        )

    try:
        info = pyogrio.read_info(path, layer=layer)
    except Exception as exc:
        raise UnsupportedFormat(
            f"{path.name} could not be opened as a vector dataset: {exc}. "
            f"WebMap reads shapefile, GeoJSON, GeoPackage, CSV/XYZ, KML, and "
            f"DXF; a .shp also needs its .shx and .dbf alongside it."
        ) from exc

    srid = _resolve_srid(path, info, srid_override)
    warnings: list[str] = []

    meta, table = _read_table(path, layer, encoding)
    geometry_column = meta.get("geometry_name") or "wkb_geometry"
    if geometry_column not in table.column_names:
        geometry_column = next(
            (c for c in table.column_names if c.lower() in {"geometry", "wkb_geometry"}),
            "",
        )
    if not geometry_column:
        raise UnsupportedFormat(
            f"{path.name} has no geometry column. If this is tabular point "
            f"data, read it with read_xyz and name the X, Y, and value "
            f"columns explicitly."
        )

    geometry = shapely.from_wkb(table.column(geometry_column).to_numpy(zero_copy_only=False))
    attributes = _decode_attributes(table, geometry_column, path)

    geometry, attributes, warnings = _normalise(geometry, attributes, path.name)
    if not len(geometry):
        raise UnsupportedFormat(
            f"{path.name} contains no usable geometry. Every feature was "
            f"missing or empty, which usually means the file is a template or "
            f"the wrong layer was selected."
        )

    return ReadResult(
        geometry=geometry,
        attributes=attributes,
        srid=srid,
        geometry_kind=_geometry_kind(geometry),
        warnings=warnings,
    )


def _read_table(path: Path, layer: str | None, encoding: str | None) -> tuple[Any, Any]:
    """Read the feature table, resolving the attribute encoding.

    A `.dbf` carries no encoding marker. The `.cpg` sidecar is the convention
    for declaring one, and it is frequently missing.
    """
    resolved = encoding
    if resolved is None:
        sidecar = path.with_suffix(ENCODING_SIDECAR_SUFFIX)
        if sidecar.exists():
            resolved = sidecar.read_text(encoding="ascii", errors="replace").strip() or None
    meta, table = pyogrio.read_arrow(path, layer=layer, encoding=resolved)
    return meta, table


def _decode_attributes(table: Any, geometry_column: str, path: Path) -> dict[str, list[Any]]:
    """Materialise attribute columns, refusing to guess an encoding.

    GDAL hands back raw bytes when it has no encoding to work from, and
    pyarrow raises on the first column that is not valid UTF-8. Left
    unhandled that surfaces as `UnicodeDecodeError` from deep inside pyarrow,
    which tells a geologist nothing about what to do.

    Guessing is the tempting fix and the wrong one. cp1252 and cp850 decode
    the same bytes to *different letters*, so a wrong guess produces plausible
    text that is silently incorrect — the same failure as a guessed CRS, one
    level down, and just as invisible. Ask instead.
    """
    attributes: dict[str, list[Any]] = {}
    for name in table.column_names:
        if name == geometry_column:
            continue
        try:
            attributes[name] = table.column(name).to_pylist()
        except UnicodeDecodeError as exc:
            raise UnsupportedFormat(
                f"The attribute column '{name}' in {path.name} is not valid "
                f"UTF-8, and the file does not say what encoding it uses. "
                f"Shapefiles declare it in a companion .cpg file; this one has "
                f"none. Re-import specifying the encoding — files from Windows "
                f"tools are usually cp1252 — or add a .cpg. Common values: "
                f"{', '.join(COMMON_ENCODINGS)}. WebMap does not guess, because "
                f"two encodings can decode the same bytes to different letters "
                f"and the result would be wrong rather than obviously broken."
            ) from exc
    return attributes


def _resolve_srid(path: Path, info: dict[str, Any], override: int | None) -> int:
    """Resolve the CRS, or refuse.

    `11-file-io.md` §3 spells out why the refusal matters more than the
    convenience: a guessed CRS produces a layer that renders, projects, and
    interpolates without complaint, in the wrong place.
    """
    if override is not None:
        return override

    declared = info.get("crs")
    if declared:
        srid = _srid_from_wkt(str(declared))
        if srid is not None:
            return srid
        raise MissingCRS(
            f"{path.name} declares a coordinate reference system that does "
            f"not correspond to a known EPSG code. WebMap identifies CRSs by "
            f"EPSG code so that grids, contours, and exports agree. Specify "
            f"the EPSG code explicitly on import if you know which one this "
            f"projection is."
        )

    sidecar = path.with_suffix(SIDECAR_SUFFIX)
    if sidecar.exists():
        srid = _srid_from_wkt(sidecar.read_text(encoding="utf-8", errors="replace"))
        if srid is not None:
            return srid

    raise MissingCRS(
        f"{path.name} has no coordinate reference system. Shapefiles store "
        f"CRS in a companion .prj file — check it was included in the upload. "
        f"You can also specify the CRS explicitly on import if you know it."
    )


def _srid_from_wkt(wkt: str) -> int | None:
    """Map a CRS description to an EPSG code.

    Delegates to `webmap_geo.crs`, which owns pyproj
    (`adr/0003-geoprocessing-owns-crs.md`). An earlier revision imported
    pyproj here directly; the import-linter contract caught it, which is what
    that contract is for.
    """
    from webmap_geo.crs import epsg_from_user_input

    return epsg_from_user_input(wkt)


def _normalise(
    geometry: NDArray[np.object_], attributes: dict[str, list[Any]], name: str
) -> tuple[NDArray[np.object_], dict[str, list[Any]], list[str]]:
    """Drop empty geometries, normalise ring orientation, and report both.

    `11-file-io.md` §6: "A layer with 40 dropped null geometries should say so
    rather than silently having 40 fewer features than the source file."
    """
    warnings: list[str] = []

    missing = shapely.is_missing(geometry)
    empty = np.zeros(len(geometry), dtype=bool)
    empty[~missing] = shapely.is_empty(geometry[~missing])
    drop = missing | empty
    if drop.any():
        warnings.append(
            f"Dropped {int(drop.sum())} of {len(geometry)} features from {name} "
            f"because their geometry was missing or empty. The remaining "
            f"{int((~drop).sum())} were read normally."
        )
        geometry = geometry[~drop]
        attributes = {
            key: [v for v, keep in zip(values, ~drop, strict=True) if keep]
            for key, values in attributes.items()
        }

    invalid = ~shapely.is_valid(geometry)
    if invalid.any():
        # Reported, not repaired. Silently fixing a self-intersecting polygon
        # changes area and boundaries, and a geologist who digitised it should
        # decide — see 09-editing.md §12.
        reasons = {str(shapely.is_valid_reason(g)) for g in geometry[invalid][:3]}
        warnings.append(
            f"{int(invalid.sum())} of {len(geometry)} geometries in {name} are "
            f"invalid ({'; '.join(sorted(reasons))}). They were read as-is and "
            f"not repaired — area and overlay results involving them will be "
            f"unreliable until they are fixed."
        )

    polygons = shapely.get_type_id(geometry)
    is_polygon = np.isin(polygons, [3, 6])  # Polygon, MultiPolygon
    if is_polygon.any():
        # Readers disagree about ring orientation (`11` §4.1). Normalising on
        # read means everything downstream can assume one convention.
        geometry = geometry.copy()
        geometry[is_polygon] = shapely.force_2d(shapely.orient_polygons(geometry[is_polygon]))

    return geometry, attributes, warnings


def _geometry_kind(geometry: NDArray[np.object_]) -> str:
    """The layer's geometry kind, or 'mixed'.

    `mixed` is not a failure — GeoPackage and GeoJSON both permit it. It
    matters later: shapefile stores one geometry type per file, so an export
    has to split (`11-file-io.md` §4.1).
    """
    kinds = {_GEOMETRY_KINDS.get(int(t)) for t in shapely.get_type_id(geometry)}
    kinds.discard(None)
    if len(kinds) == 1:
        return next(iter(kinds))  # type: ignore[return-value]
    return "mixed"


def read_xyz(
    path: Path,
    *,
    x_column: str,
    y_column: str,
    z_column: str,
    srid: int,
    delimiter: str | None = None,
) -> ReadResult:
    """Read scattered XYZ or tabular point data.

    Column mapping is REQUIRED, not sniffed. Files arrive with headers like
    'X,Y,Z', 'EAST,NORTH,TVDSS', 'lon,lat,porosity', or no header at all.
    Sniffing gets it wrong occasionally, and 'occasionally' means a map with
    latitude and longitude swapped that nobody notices until it is in a deck.

    The API exposes `POST /datasets/preview` so the UI and Claude can confirm
    a proposed mapping before committing to it.
    """
    import csv

    text = path.read_text(encoding="utf-8", errors="replace")
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;\t| ").delimiter
        except csv.Error:
            delimiter = ","

    rows = list(csv.DictReader(text.splitlines(), delimiter=delimiter))
    if not rows:
        raise UnsupportedFormat(f"{path.name} has no data rows.")

    header = list(rows[0])
    for role, column in (("x", x_column), ("y", y_column), ("z", z_column)):
        if column not in header:
            raise UnsupportedFormat(
                f"{path.name} has no column named '{column}' for {role}. "
                f"Its columns are: {', '.join(header)}. Column mapping is "
                f"required rather than guessed — sniffing swaps latitude and "
                f"longitude often enough to matter."
            )

    warnings: list[str] = []
    xs: list[float] = []
    ys: list[float] = []
    values: list[float | None] = []
    skipped = 0
    for row in rows:
        try:
            xs.append(float(row[x_column]))
            ys.append(float(row[y_column]))
        except (TypeError, ValueError):
            skipped += 1
            continue
        values.append(_maybe_float(row[z_column]))

    if skipped:
        warnings.append(
            f"Skipped {skipped} of {len(rows)} rows in {path.name} because "
            f"'{x_column}' or '{y_column}' could not be read as a number."
        )
    if not xs:
        raise UnsupportedFormat(
            f"No row in {path.name} had numeric values in both '{x_column}' "
            f"and '{y_column}'. Check the column mapping and the delimiter."
        )

    x = np.asarray(xs, dtype=np.float64)
    y = np.asarray(ys, dtype=np.float64)
    warnings.extend(_suspect_axis_order(x, y, srid, path.name))

    return ReadResult(
        geometry=np.asarray(shapely.points(x, y), dtype=object),
        attributes={z_column: list(values)},
        srid=srid,
        geometry_kind="point",
        warnings=warnings,
    )


def _maybe_float(raw: str | None) -> float | None:
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _suspect_axis_order(
    x: NDArray[np.float64], y: NDArray[np.float64], srid: int, name: str
) -> list[str]:
    """Warn when X and Y look swapped.

    Only meaningful for a geographic CRS, where the ranges differ: latitude
    cannot exceed 90 but longitude can reach 180. If every X is within +/-90
    and some Y is outside it, the columns are almost certainly the wrong way
    round — the exact mistake `read_xyz` refuses to guess about, caught after
    the fact so at least it is *said*.
    """
    from webmap_geo.crs import is_geographic

    if not is_geographic(srid):
        return []

    x_fits_latitude = bool(np.all(np.abs(x) <= 90))
    y_exceeds_latitude = bool(np.any(np.abs(y) > 90))
    if x_fits_latitude and y_exceeds_latitude:
        return [
            f"In {name}, every X value is within +/-90 while some Y values "
            f"exceed it. That is the signature of latitude and longitude "
            f"being mapped the wrong way round. Check the column mapping "
            f"before using this layer."
        ]
    return []


def sanitize_name(name: str, *, fallback: str = "layer") -> str:
    """Make a dataset or layer name safe to use in a filesystem path.

    `03-auth-security.md` §9: a layer called `../../etc/passwd` is a real
    shapefile you may receive. Path separators, traversal segments, control
    characters, and Windows device names all have to go — and the result must
    still be non-empty, because an empty filename is its own vulnerability.
    """
    normalised = unicodedata.normalize("NFKC", name)
    # Strip anything that is not plainly safe rather than blocklisting the
    # dangerous parts: blocklists are always incomplete.
    cleaned = re.sub(r"[^\w.\- ]+", "_", normalised, flags=re.UNICODE).strip(" .")
    cleaned = re.sub(r"_{2,}", "_", cleaned)

    # Windows refuses these as filenames regardless of extension.
    reserved = {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
    if not cleaned or cleaned.lower() in reserved:
        return fallback
    return cleaned[:120]


def describe_result(result: ReadResult, source: str) -> dict[str, Any]:
    """A compact summary for the ingest response and the audit detail."""
    return {
        "source": source,
        "srid": result.srid,
        "geometry_kind": result.geometry_kind,
        "feature_count": result.feature_count,
        "attributes": list(result.attributes),
        "warnings": result.warnings,
    }


def attribute_schema(result: ReadResult) -> list[dict[str, Any]]:
    """Infer the attribute schema recorded on the dataset row (`02` §3.5)."""
    schema: list[dict[str, Any]] = []
    for name, values in result.attributes.items():
        present = [v for v in values if v is not None]
        schema.append(
            {
                "name": name,
                "type": _attribute_type(present),
                "nullable": len(present) != len(values),
            }
        )
    return schema


def _attribute_type(values: list[Any]) -> str:
    if not values:
        return "string"
    sample = values[0]
    if isinstance(sample, bool):
        return "boolean"
    if isinstance(sample, int):
        return "integer"
    if isinstance(sample, float):
        return "number"
    from datetime import date, datetime

    if isinstance(sample, datetime):
        return "datetime"
    if isinstance(sample, date):
        return "date"
    return "string"


def to_json_ready(props: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Coerce attribute values into something `json.dumps` accepts.

    OGR hands back dates, decimals, and bytes. The Parquet writer stores props
    as a JSON string, so the coercion has to happen somewhere; doing it here
    keeps the writer free of format-specific knowledge.
    """
    from datetime import date, datetime

    def convert(value: Any) -> Any:
        if value is None or isinstance(value, str | int | float | bool):
            return value
        if isinstance(value, datetime | date):
            return value.isoformat()
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    return [{k: convert(v) for k, v in row.items()} for row in props]


__all__ = [
    "ReadResult",
    "attribute_schema",
    "describe_result",
    "read_vector",
    "read_xyz",
    "sanitize_name",
    "to_json_ready",
]
