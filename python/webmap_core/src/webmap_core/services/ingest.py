"""The ingest pipeline. `11-file-io.md` §6.

    1. describe    what the upload is
    2. fetch       materialise locally (and safely — uploads are archives)
    3. detect      format from extension and contents, not extension alone
    4. read        format-specific reader
    5. validate    CRS present, geometries valid, encoding sane
    6. normalise   ring orientation, drop empty geometries
    7. write       GeoParquet, Hilbert-sorted so row groups prune
    8. register    dataset row, version row, schema, bbox, feature count
    9. caption     the one-line description Claude reads
   10. audit       emit the ingest event

Steps 5 and 6 produce warnings attached to the dataset and returned to the
caller. A layer with forty dropped null geometries should say so rather than
silently having forty fewer features than the source file.

The registration happens **as the principal**, through the ordinary INSERT
policy. Ingest is not a privileged path: a dataset created by an upload is
owned by whoever uploaded it, and the database enforces that.
"""

import hashlib
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncConnection

from webmap_core.logging import get_logger
from webmap_core.models import DatasetKind, GeometryKind
from webmap_core.permissions import Principal, Visibility
from webmap_core.services.datasets import create_dataset

log = get_logger(__name__)

#: Uploads above this are refused before anything is parsed. `00-overview.md`
#: §5 puts vector display at up to 5M features; a file larger than this is
#: either the wrong file or belongs on a share, read by the sync connector
#: rather than pushed through a request.
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024

#: Which `dataset_kind_t` a geometry kind implies when the caller does not
#: say. Points are `pointset` because that is what gets interpolated;
#: `vector` is the general case for everything else.
_KIND_FOR_GEOMETRY = {
    GeometryKind.POINT: DatasetKind.POINTSET,
    GeometryKind.LINESTRING: DatasetKind.VECTOR,
    GeometryKind.POLYGON: DatasetKind.VECTOR,
    GeometryKind.MIXED: DatasetKind.VECTOR,
}


@dataclass(frozen=True)
class IngestResult:
    dataset_id: UUID
    name: str
    kind: DatasetKind
    geometry_kind: GeometryKind
    srid: int
    feature_count: int
    bbox_4326: list[float]
    parquet_key: str
    caption: str
    warnings: list[str]


@dataclass(frozen=True)
class IngestOptions:
    """Everything the caller may state and the pipeline must not guess."""

    name: str
    project_id: UUID | None = None
    visibility: Visibility = Visibility.TEAM
    owner_team_id: UUID | None = None
    kind: DatasetKind | None = None
    description: str | None = None

    #: The CRS, when the file does not carry one. Never inferred (`11` §3).
    srid_override: int | None = None
    #: The attribute encoding, when no `.cpg` declares it (`11` §4.1).
    encoding: str | None = None

    #: XYZ/CSV column mapping. Required for those formats, never sniffed —
    #: sniffing swaps latitude and longitude often enough to matter.
    x_column: str | None = None
    y_column: str | None = None
    z_column: str | None = None


async def ingest_upload(
    conn: AsyncConnection,
    principal: Principal,
    source: Path,
    options: IngestOptions,
    *,
    storage: Any,
    bucket: str,
) -> IngestResult:
    """Register an uploaded file as a dataset."""
    from webmap_io.parquet import write_features
    from webmap_io.read import (
        attribute_schema,
        read_vector,
        sanitize_name,
        to_json_ready,
    )
    from webmap_io.storage import feature_key, put_file
    from webmap_io.upload import prepare_upload

    size = source.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        from webmap_core.exceptions import LimitExceeded

        raise LimitExceeded(
            f"The upload is {size / 1e9:.1f} GB (limit "
            f"{MAX_UPLOAD_BYTES / 1e9:.0f} GB). Convert it to GeoPackage or "
            f"GeoParquet first, which are far smaller for the same data, or "
            f"register it from a file share instead of uploading it."
        )

    with tempfile.TemporaryDirectory(prefix="webmap-ingest-") as workdir:
        upload = prepare_upload(source, Path(workdir) / "extracted")
        warnings = list(upload.warnings)

        if upload.format == "csv":
            result = _read_tabular(upload.path, options)
        else:
            result = read_vector(
                upload.path,
                srid_override=options.srid_override,
                encoding=options.encoding,
            )
        warnings.extend(result.warnings)

        # The id is generated before the row exists because the storage key
        # embeds it. An orphaned object is recoverable; a row pointing at a
        # key that was never written is not.
        dataset_id = uuid4()
        key = feature_key(str(dataset_id), 1)

        parquet_path = Path(workdir) / "features.parquet"
        write_result = write_features(
            parquet_path,
            geometry=result.geometry,
            props=to_json_ready(result.props()),
            srid=result.srid,
        )
        put_file(storage, bucket, key, parquet_path)

    bbox = _bbox_4326(write_result.bbox, result.srid)
    geometry_kind = GeometryKind(result.geometry_kind)
    kind = options.kind or _KIND_FOR_GEOMETRY[geometry_kind]
    caption = _caption(options.name, kind, result.feature_count, result.srid, bbox)

    created_id = await create_dataset(
        conn,
        principal,
        name=sanitize_name(options.name, fallback="Imported layer"),
        description=options.description,
        kind=kind,
        geometry_kind=geometry_kind,
        storage_srid=result.srid,
        connector="upload",
        project_id=options.project_id,
        parquet_key=key,
        feature_count=result.feature_count,
        bbox_4326=bbox,
        attribute_schema=attribute_schema(result),
        owner_team_id=options.owner_team_id,
        visibility=options.visibility,
        caption=caption,
        source_uri=f"upload://{source.name}",
        source_checksum=_checksum(source),
        # Kept for the sync path, which has to read the file the way the person
        # importing it said to — the column mapping especially, which `11` §3.1
        # forbids sniffing and which is otherwise lost the moment this returns.
        source_options=_reader_options(options),
        dataset_id=dataset_id,
    )

    log.info(
        "ingest_complete",
        dataset_id=str(created_id),
        features=result.feature_count,
        srid=result.srid,
        warnings=len(warnings),
    )
    return IngestResult(
        dataset_id=created_id,
        name=options.name,
        kind=kind,
        geometry_kind=geometry_kind,
        srid=result.srid,
        feature_count=result.feature_count,
        bbox_4326=bbox,
        parquet_key=key,
        caption=caption,
        warnings=warnings,
    )


def _reader_options(options: IngestOptions) -> dict[str, Any] | None:
    """The subset of the ingest options a later read needs.

    Only what `read_xyz` and `read_vector` take. The rest of `IngestOptions` is
    about registration — name, project, visibility — and storing it here would
    make this a second, staler copy of the dataset row.
    """
    stored = {
        "x_column": options.x_column,
        "y_column": options.y_column,
        "z_column": options.z_column,
        "encoding": options.encoding,
    }
    kept = {key: value for key, value in stored.items() if value}
    return kept or None


def _read_tabular(path: Path, options: IngestOptions) -> Any:
    """Read CSV/XYZ, insisting on the column mapping and the CRS.

    Both are refusals rather than guesses, and for the same reason: a sniffed
    column mapping produces a map with latitude and longitude swapped, and an
    assumed CRS produces one in the wrong hemisphere. Neither raises anything
    at the time.
    """
    from webmap_io.exceptions import MissingCRS, UnsupportedFormat
    from webmap_io.read import read_xyz

    missing = [
        label
        for label, value in (
            ("x_column", options.x_column),
            ("y_column", options.y_column),
            ("z_column", options.z_column),
        )
        if not value
    ]
    if missing:
        raise UnsupportedFormat(
            f"Reading {path.name} as tabular point data needs an explicit "
            f"column mapping; {', '.join(missing)} were not given. Preview the "
            f"file first to see its columns — the mapping is never guessed, "
            f"because sniffing swaps latitude and longitude often enough to "
            f"put a map in the wrong hemisphere."
        )
    if options.srid_override is None:
        raise MissingCRS(
            f"{path.name} is a plain table and carries no coordinate reference "
            f"system. Specify the EPSG code of its X and Y columns — for "
            f"longitude and latitude that is 4326."
        )

    return read_xyz(
        path,
        x_column=str(options.x_column),
        y_column=str(options.y_column),
        z_column=str(options.z_column),
        srid=options.srid_override,
    )


def _bbox_4326(bbox: tuple[float, float, float, float], srid: int) -> list[float]:
    """Storage CRS bounds to the EPSG:4326 bbox the registry stores.

    `dataset.bbox_4326` is four floats, not a geometry — the control plane has
    no PostGIS (`adr/0002`). Everything that searches or zooms by extent reads
    this, so it is one of the few places a CRS error would be visible early.
    """
    from webmap_geo.crs import WGS84, transform_bbox

    return list(transform_bbox(bbox, srid, WGS84))


def _caption(name: str, kind: DatasetKind, count: int, srid: int, bbox: list[float]) -> str:
    """The one-liner Claude reads. `02-data-model.md` §3.5.

    Facts only, and every one of them checkable against the dataset row. The
    caption exists so Claude can describe a layer without inventing anything,
    which means it must never contain an interpretation.
    """
    where = f"{abs(bbox[1]):.1f}°{'N' if bbox[1] >= 0 else 'S'}, "
    where += f"{abs(bbox[0]):.1f}°{'E' if bbox[0] >= 0 else 'W'}"
    return f"{name}: {count:,} {kind.value} features in EPSG:{srid}, extent from about {where}."


def _checksum(path: Path) -> str:
    """Content hash, so a re-upload of the same bytes is recognisable.

    Streamed rather than read whole: this runs on files up to the 2 GB cap.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def preview(source: Path, *, rows: int = 50, encoding: str | None = None) -> dict[str, Any]:
    """Inspect an upload without committing to it. `11-file-io.md` §3.1.

    The UI and Claude use this to confirm a column mapping before ingest,
    which is what makes "the mapping is required, not sniffed" workable rather
    than merely strict.
    """
    import csv

    from webmap_io.read import read_vector
    from webmap_io.upload import prepare_upload

    with tempfile.TemporaryDirectory(prefix="webmap-preview-") as workdir:
        upload = prepare_upload(source, Path(workdir) / "extracted")

        if upload.format == "csv":
            text = upload.path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            try:
                delimiter = csv.Sniffer().sniff("\n".join(lines[:20])).delimiter
            except csv.Error:
                delimiter = ","
            reader = csv.DictReader(lines, delimiter=delimiter)
            sample = [row for _, row in zip(range(rows), reader, strict=False)]
            columns = list(sample[0]) if sample else []
            return {
                "format": "csv",
                "delimiter": delimiter,
                "columns": columns,
                "rows": sample,
                "proposed_mapping": _propose_mapping(columns),
                "detail": (
                    "Confirm the column mapping and the EPSG code before "
                    "importing. Neither is inferred."
                ),
            }

        result = read_vector(upload.path, encoding=encoding)
        return {
            "format": upload.format,
            "srid": result.srid,
            "geometry_kind": result.geometry_kind,
            "feature_count": result.feature_count,
            "columns": list(result.attributes),
            "rows": result.props()[:rows],
            "warnings": result.warnings,
        }


def _propose_mapping(columns: list[str]) -> dict[str, str | None]:
    """Suggest an X/Y/Z mapping for the user to confirm — never to apply.

    A proposal the user confirms is a different thing from a guess the
    software acts on. The distinction is the whole point of `11` §3.1.
    """
    lowered = {c.lower(): c for c in columns}

    def find(*names: str) -> str | None:
        return next((lowered[n] for n in names if n in lowered), None)

    x = find("x", "lon", "long", "longitude", "east", "easting")
    y = find("y", "lat", "latitude", "north", "northing")
    # The value column is whatever the survey measured — porosity, saturation,
    # net pay, a formation top. A fixed vocabulary would miss most of them, so
    # propose the first column that is not a coordinate and let the user
    # confirm. It is a proposal, not a guess the software acts on.
    z = find("z", "value", "depth", "tvdss", "elevation") or next(
        (c for c in columns if c not in {x, y}), None
    )
    return {"x_column": x, "y_column": y, "z_column": z}
