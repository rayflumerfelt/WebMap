"""Exporting a dataset to a file somebody can download. `11-file-io.md` §7.

Three rules, and each of them is somebody's bad afternoon avoided:

**Always a new object.** Never a modification of a source, even where the source
is writable (`03-auth-security.md` §8, `CLAUDE.md` §3.4). An export is a copy by
definition, and a format conversion that wrote back over its input would be a
lossy edit nobody asked for.

**The loss report comes back with the result, not instead of it.** A geologist
sending a shapefile to a partner needs to know that `porosity_average` arrives
as `porosity_a`; §4.2 puts that in the confirmation dialog *and* in the MCP
response, so `plan_export` runs before the write and its warnings travel with
the download.

**The URL is short-lived and single-purpose.** The same reasoning as §6's tile
tokens: a leaked link exposes one file that its requester was already entitled
to, for fifteen minutes, rather than a bearer credential.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from webmap_core.logging import get_logger
from webmap_core.permissions import Principal
from webmap_core.services.audit import AuditAction
from webmap_core.services.audit import record as record_audit
from webmap_core.services.datasets import resolve_feature_object

log = get_logger(__name__)

#: How long a download link lives. Long enough to click, short enough that a
#: link pasted into a chat is useless by the time anybody else reads it.
DOWNLOAD_TTL_SECONDS = 900


class ExportError(Exception):
    """An export that could not be produced. Carries what to do instead."""


@dataclass(frozen=True)
class ExportRequest:
    dataset_id: UUID
    fmt: str = "gpkg"
    #: Columns to carry. Empty means every one of them.
    columns: list[str] = field(default_factory=list)
    #: Reproject on the way out. `None` keeps the storage CRS, which is what a
    #: recipient loading it beside the rest of the project wants.
    target_srid: int | None = None
    #: Proceed even when the format will lose something. The warnings come back
    #: either way; this decides whether they stop the export.
    accept_loss: bool = False

    @classmethod
    def from_parameters(cls, parameters: dict[str, Any]) -> ExportRequest:
        return cls(
            dataset_id=UUID(str(parameters["dataset_id"])),
            fmt=str(parameters.get("fmt", "gpkg")),
            columns=list(parameters.get("columns") or []),
            target_srid=(
                int(parameters["target_srid"]) if parameters.get("target_srid") else None
            ),
            accept_loss=bool(parameters.get("accept_loss", False)),
        )

    def to_parameters(self) -> dict[str, Any]:
        return {
            "dataset_id": str(self.dataset_id),
            "fmt": self.fmt,
            "columns": self.columns,
            "target_srid": self.target_srid,
            "accept_loss": self.accept_loss,
        }


@dataclass(frozen=True)
class ExportSource:
    dataset_id: UUID
    name: str
    parquet_key: str
    version: int
    storage_srid: int
    geometry_kind: str
    feature_count: int | None
    attribute_schema: list[dict[str, Any]]


@dataclass(frozen=True)
class ExportResult:
    key: str
    filename: str
    size_bytes: int
    fmt: str
    feature_count: int
    warnings: list[dict[str, Any]]


async def resolve_export(
    conn: AsyncConnection, principal: Principal, request: ExportRequest
) -> ExportSource:
    """Permission-checked lookup of what to export.

    Viewer, deliberately: exporting is reading. Anyone who can see the layer on
    a map can take a copy of it, and pretending otherwise would be security
    theatre — they can already screenshot it, and `03` §8 puts the control at
    the audit record rather than at the refusal.
    """
    parquet_key, version = await resolve_feature_object(conn, principal, request.dataset_id)

    row = (
        await conn.execute(
            text(
                """
                SELECT name, storage_srid, geometry_kind, feature_count, attribute_schema
                  FROM dataset WHERE id = :id
                """
            ),
            {"id": request.dataset_id},
        )
    ).one()

    return ExportSource(
        dataset_id=request.dataset_id,
        name=str(row.name),
        parquet_key=parquet_key,
        version=version,
        storage_srid=int(row.storage_srid),
        geometry_kind=str(row.geometry_kind or "unknown"),
        feature_count=row.feature_count,
        attribute_schema=list(row.attribute_schema or []),
    )


def plan(source: ExportSource, request: ExportRequest) -> list[dict[str, Any]]:
    """What this export will lose, as plain dicts for the job document."""
    from webmap_io.export import plan_export

    schema = source.attribute_schema
    if request.columns:
        schema = [item for item in schema if str(item.get("name")) in set(request.columns)]

    return [
        {"code": warning.code, "message": warning.message, "affected": warning.affected}
        for warning in plan_export(
            schema, source.geometry_kind, request.fmt, feature_count=source.feature_count
        )
    ]


#: Warnings that are advice rather than loss. `prefer_gpkg` is an offer of a
#: better format and `csv_geometry` states a fact about CSV; neither is a reason
#: to stop somebody who asked for that format on purpose.
ADVISORY = frozenset({"prefer_gpkg", "csv_geometry", "csv_geometry_wkt", "size_risk"})


def blocking(warnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The warnings that mean data is actually lost."""
    return [warning for warning in warnings if warning["code"] not in ADVISORY]


def filename_for(source: ExportSource, fmt: str) -> str:
    """A filename a recipient can make sense of a month later.

    The dataset name, slugged, with the version in it. The version matters more
    than it looks: two exports of the same layer a week apart are otherwise the
    same filename in a downloads folder, and the one that gets attached to the
    email is whichever sorted first.
    """
    from webmap_io.export import FORMATS
    from webmap_io.read import sanitize_name

    stem = sanitize_name(source.name, fallback="layer").replace(" ", "_").lower()
    return f"{stem}_v{source.version}{FORMATS[fmt]}"


def run_export(
    source: ExportSource,
    request: ExportRequest,
    *,
    store: Any,
    object_store: Any,
    bucket: str,
) -> ExportResult:
    """Read the dataset, write the file, put it where a link can reach it.

    Synchronous and CPU/IO-bound, so the worker calls it off the event loop.
    """
    from webmap_io.export import zip_shapefile
    from webmap_io.storage import put_file
    from webmap_io.write import write_features

    warnings = plan(source, request)
    if blocking(warnings) and not request.accept_loss:
        raise ExportError(
            f"Exporting '{source.name}' as {request.fmt} would lose information: "
            + " ".join(warning["message"] for warning in blocking(warnings))
            + " Re-run with accept_loss to proceed anyway, or choose GeoPackage, "
            "which keeps all of it."
        )

    geometry, props = _read(source, request, object_store=object_store, bucket=bucket)
    if request.target_srid and request.target_srid != source.storage_srid:
        geometry = _reproject(geometry, source.storage_srid, request.target_srid)
    srid = request.target_srid or source.storage_srid

    filename = filename_for(source, request.fmt)
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        if request.fmt == "shapefile":
            stem = filename.removesuffix(".zip")
            write_features(
                directory / f"{stem}.shp",
                geometry=geometry,
                props=props,
                srid=srid,
                fmt="shapefile",
            )
            written = zip_shapefile(directory, directory / filename, stem=stem)
        else:
            written = write_features(
                directory / filename, geometry=geometry, props=props, srid=srid, fmt=request.fmt
            )

        key = f"exports/{source.dataset_id}/{uuid4()}/{filename}"
        put_file(store, bucket, key, written)
        size = written.stat().st_size

    return ExportResult(
        key=key,
        filename=filename,
        size_bytes=size,
        fmt=request.fmt,
        feature_count=len(geometry),
        warnings=warnings,
    )


def _read(
    source: ExportSource, request: ExportRequest, *, object_store: Any, bucket: str
) -> tuple[Any, list[dict[str, Any]]]:
    """Geometry and attributes out of the GeoParquet object.

    Through DuckDB like every other reader in this codebase, rather than
    through pyarrow: the object lives in S3 and `webmap_geo.dataplane` is what
    knows how to reach it with the right credentials and extensions.
    """
    import json

    import numpy as np
    import shapely

    from webmap_geo.dataplane import connect

    with connect(object_store) as conn:
        rows = conn.execute(
            "SELECT geometry, props FROM read_parquet($key)",
            {"key": f"s3://{bucket}/{source.parquet_key}"},
        ).fetchall()

    wanted = set(request.columns) if request.columns else None
    geometry = np.array([shapely.from_wkb(bytes(row[0])) for row in rows], dtype=object)
    props = []
    for row in rows:
        attributes = json.loads(row[1]) if row[1] else {}
        props.append(
            {key: value for key, value in attributes.items() if wanted is None or key in wanted}
        )
    return geometry, props


def _reproject(geometry: Any, from_srid: int, to_srid: int) -> Any:
    """Reproject on the way out, at the boundary and nowhere else.

    `CLAUDE.md` §3.1: reprojection happens at defined boundaries. An export is
    one — the file is leaving the system and the recipient's CRS is theirs to
    choose, not ours to assume.
    """
    from webmap_geo.crs import transform_geometries

    return transform_geometries(geometry, from_srid, to_srid)


async def record_export(
    conn: AsyncConnection,
    principal: Principal,
    source: ExportSource,
    result: ExportResult,
) -> None:
    """Audit the export. `03-auth-security.md` §9 lists it explicitly.

    Exporting is reading, and the control on reading is knowing it happened:
    "who took a copy of this layer, and when" is the question asked after data
    turns up somewhere it should not have.
    """
    await record_audit(
        conn,
        principal=principal,
        action=AuditAction.EXPORT_CREATED,
        object_type="dataset",
        object_id=source.dataset_id,
        detail={
            "format": result.fmt,
            "features": result.feature_count,
            "bytes": result.size_bytes,
            "version": source.version,
            "warnings": [warning["code"] for warning in result.warnings],
        },
    )


__all__ = [
    "DOWNLOAD_TTL_SECONDS",
    "ExportError",
    "ExportRequest",
    "ExportResult",
    "ExportSource",
    "blocking",
    "filename_for",
    "plan",
    "record_export",
    "resolve_export",
    "run_export",
]
