"""Contouring a stored grid. `05-geoprocessing.md` §7.

The orchestration around `webmap_geo.contour`: read the COG as the requester,
trace, and register the lines as a vector layer with the attributes a style
needs to draw them.

**Contours are a reading aid, and the attributes carry that.** Every line goes
out with its `value` and an `is_index` flag, because index contours — every
fifth, drawn heavier and labelled — are what make a dense structure map
legible at all. A layer that loses that flag is a layer of identical hairlines.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from webmap_core.jobs import JobContext
from webmap_core.logging import get_logger
from webmap_core.models import DatasetKind, GeometryKind, Visibility
from webmap_core.permissions import Permission, Principal
from webmap_core.services.datasets import create_dataset, resolve_grid_object
from webmap_core.services.gridding import record_lineage
from webmap_core.services.ownable import load_and_require

log = get_logger(__name__)

#: A structure map with three thousand contour fragments on it is not a map.
#: Above this the interval is too fine for the surface, and saying so is more
#: useful than drawing it.
MAX_CONTOUR_FEATURES = 20_000


@dataclass(frozen=True)
class ContourRequest:
    dataset_id: UUID
    interval: float | None = None
    target_count: int = 15
    #: Explicit levels win over both interval and target count, for the case
    #: where a partner's map has to be matched exactly.
    levels: list[float] | None = None
    smoothing: float = 0.0
    index_every: int = 5
    min_length: float | None = None
    output_name: str | None = None
    project_id: UUID | None = None
    visibility: Visibility = Visibility.TEAM
    owner_team_id: UUID | None = None

    def to_parameters(self) -> dict[str, Any]:
        return {
            "dataset_id": str(self.dataset_id),
            "interval": self.interval,
            "target_count": self.target_count,
            "levels": self.levels,
            "smoothing": self.smoothing,
            "index_every": self.index_every,
            "min_length": self.min_length,
            "output_name": self.output_name,
            "project_id": str(self.project_id) if self.project_id else None,
            "visibility": self.visibility.value,
            "owner_team_id": str(self.owner_team_id) if self.owner_team_id else None,
        }

    @classmethod
    def from_parameters(cls, parameters: dict[str, Any]) -> ContourRequest:
        return cls(
            dataset_id=UUID(parameters["dataset_id"]),
            interval=parameters.get("interval"),
            target_count=parameters.get("target_count", 15),
            levels=parameters.get("levels"),
            smoothing=parameters.get("smoothing", 0.0),
            index_every=parameters.get("index_every", 5),
            min_length=parameters.get("min_length"),
            output_name=parameters.get("output_name"),
            project_id=(
                UUID(parameters["project_id"]) if parameters.get("project_id") else None
            ),
            visibility=Visibility(parameters.get("visibility", "team")),
            owner_team_id=(
                UUID(parameters["owner_team_id"]) if parameters.get("owner_team_id") else None
            ),
        )


@dataclass(frozen=True)
class GridSource:
    dataset_id: UUID
    cog_key: str
    storage_srid: int
    name: str
    bbox_4326: list[float] | None
    project_id: UUID | None


async def resolve_grid(
    conn: AsyncConnection, principal: Principal, request: ContourRequest
) -> GridSource:
    """Permission-checked lookup of the grid to contour.

    `resolve_grid_object` is the enforcement point for raster content, the
    counterpart of `resolve_feature_object` — the API is the only thing
    standing between a principal and the object (`02` §4.1).
    """
    cog_key = await resolve_grid_object(conn, principal, request.dataset_id)

    row = (
        await conn.execute(
            text(
                "SELECT name, storage_srid, bbox_4326, project_id FROM dataset WHERE id = :id"
            ),
            {"id": request.dataset_id},
        )
    ).one()

    return GridSource(
        dataset_id=request.dataset_id,
        cog_key=cog_key,
        storage_srid=int(row.storage_srid),
        name=str(row.name),
        bbox_4326=list(row.bbox_4326) if row.bbox_4326 else None,
        project_id=row.project_id,
    )


def read_grid(source: GridSource, store: Any, bucket: str) -> tuple[Any, Any]:
    """Read the COG back into a surface and a `GridDefinition`.

    The grid is reconstructed from the raster's own transform rather than from
    anything remembered about how it was made. A contour set is checked
    against the map it is drawn on, so the two must come from the same source
    of truth — and after a resample or a re-registration, the file is it.
    """
    import rasterio

    from webmap_geo.crs import frame_for
    from webmap_geo.grid import GridDefinition
    from webmap_io.storage import get_bytes

    data = get_bytes(store, bucket, source.cog_key)
    with rasterio.io.MemoryFile(data) as memory, memory.open() as src:
        surface = src.read(1).astype(np.float64)
        if src.nodata is not None and not np.isnan(src.nodata):
            surface[surface == src.nodata] = np.nan
        transform = src.transform

    cell_size = float(abs(transform.a))
    # A GeoTIFF's origin is the outer edge of the first pixel; GridDefinition's
    # xmin/ymin are the south-west *cell centre*. Half a cell, and it is the
    # offset that makes a contour set sit beside the grid it came from.
    xmin = float(transform.c) + cell_size / 2.0
    north = float(transform.f) - cell_size / 2.0
    ny, nx = surface.shape
    ymin = north - (ny - 1) * cell_size

    grid = GridDefinition(
        xmin=xmin,
        ymin=ymin,
        cell_size=cell_size,
        nx=nx,
        ny=ny,
        frame=frame_for(source.storage_srid),
    )
    return surface, grid


def choose_levels(surface: Any, request: ContourRequest) -> Any:
    """Explicit levels, a stated interval, or a round one chosen for a reader.

    In that order of precedence: matching a partner's map exactly beats a
    house interval, and a house interval beats an automatic one. What is never
    done is deriving an interval from the count alone — `auto_levels` exists
    because 1.0 with eighteen contours beats 1.18 with exactly fifteen.
    """
    from webmap_geo.contour.lines import auto_levels
    from webmap_geo.exceptions import DegenerateInput

    finite = surface[np.isfinite(surface)]
    if finite.size == 0:
        raise DegenerateInput(
            "Every cell of this grid is blank, so there is nothing to contour. "
            "The grid was either fully extrapolated away or written empty."
        )

    if request.levels:
        return np.asarray(sorted(request.levels), dtype=np.float64)

    vmin, vmax = float(finite.min()), float(finite.max())
    if request.interval:
        first = np.ceil(vmin / request.interval) * request.interval
        levels = np.arange(first, vmax, request.interval)
        if len(levels) == 0:
            raise DegenerateInput(
                f"An interval of {request.interval:g} produces no contours over a "
                f"range of {vmin:g} to {vmax:g}. Use an interval smaller than "
                f"{vmax - vmin:g}, or omit it and let one be chosen."
            )
        return np.asarray(levels, dtype=np.float64)

    return auto_levels(vmin, vmax, request.target_count)


def trace(surface: Any, grid: Any, levels: Any, request: ContourRequest) -> list[Any]:
    """Extract the lines, refusing a set too dense to read."""
    from webmap_geo.contour.lines import contour_grid
    from webmap_geo.exceptions import DegenerateInput

    lines = contour_grid(
        surface,
        grid,
        levels=levels,
        smoothing=request.smoothing,
        min_length=request.min_length,
        index_every=request.index_every,
    )

    if len(lines) > MAX_CONTOUR_FEATURES:
        interval = interval_of(levels)
        raise DegenerateInput(
            f"That interval produces {len(lines):,} contour lines (limit "
            f"{MAX_CONTOUR_FEATURES:,}), which is not a map anyone can read. "
            f"An interval of {interval * 5:g} instead of {interval:g} would give "
            f"roughly a fifth as many."
        )
    if not lines:
        raise DegenerateInput(
            f"No contours were produced at levels {levels.min():g} to "
            f"{levels.max():g}. Either every level fell outside the surface's "
            f"range, or the fragments were all shorter than the minimum length."
        )
    return lines


def interval_of(levels: Any) -> float:
    """The interval a reader would quote for this set.

    The *median* gap rather than the first: an explicit level list need not be
    evenly spaced, and quoting its first gap as "the interval" would put a
    number on the legend that most of the map does not follow.
    """
    if len(levels) < 2:
        return 0.0
    return float(np.median(np.diff(np.asarray(levels, dtype=float))))


def index_values(lines: list[Any]) -> list[float]:
    from webmap_geo.contour.lines import index_levels

    return index_levels(lines)


def caption(source: GridSource, lines: list[Any], levels: Any) -> str:
    """`04-mcp-server.md` §6.1. Names the interval, because that is the first
    thing a geologist asks about a contour map and the last thing a bare
    feature count tells them."""
    interval = interval_of(levels)
    return (
        f"{len(lines):,} contours of {source.name} at {interval:g} intervals, "
        f"{float(np.min(levels)):g} to {float(np.max(levels)):g}"
    )


async def write_contour_dataset(
    conn: AsyncConnection,
    principal: Principal,
    context: JobContext,
    request: ContourRequest,
    source: GridSource,
    lines: list[Any],
    levels: Any,
    *,
    store: Any,
    bucket: str,
) -> UUID:
    """Write the GeoParquet object, then register it.

    Object first, row second — an orphaned object is recoverable by a sweep,
    a row pointing at a key that was never written is a layer that 404s
    forever and looks like a permission problem.
    """
    import tempfile
    from pathlib import Path

    from webmap_io.parquet import write_features
    from webmap_io.storage import feature_key, put_bytes

    dataset_id = uuid4()
    key = feature_key(str(dataset_id), 1)

    geometry = np.asarray([line.geometry for line in lines], dtype=object)
    props: list[dict[str, Any]] = [
        {
            "value": line.value,
            # The flag a style reads to draw every fifth line heavier. Without
            # it a dense structure map is a field of identical hairlines.
            "is_index": line.is_index,
            "closed": line.closed,
        }
        for line in lines
    ]

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "contours.parquet"
        write_features(
            path,
            geometry=geometry,
            props=props,
            srid=source.storage_srid,
        )
        put_bytes(store, bucket, key, path.read_bytes())

    name = request.output_name or f"{source.name} contours"
    await create_dataset(
        conn,
        principal,
        name=name,
        kind=DatasetKind.VECTOR,
        geometry_kind=GeometryKind.LINESTRING,
        storage_srid=source.storage_srid,
        connector="derived",
        project_id=request.project_id or source.project_id,
        parquet_key=key,
        feature_count=len(lines),
        # Inherited from the grid rather than recomputed: contours are a
        # subset of the surface's extent by construction, and reprojecting a
        # planar bound to 4326 here would be a second, avoidable conversion.
        bbox_4326=source.bbox_4326,
        attribute_schema=[
            {"name": "value", "type": "double"},
            {"name": "is_index", "type": "boolean"},
            {"name": "closed", "type": "boolean"},
        ],
        owner_team_id=request.owner_team_id,
        visibility=request.visibility,
        caption=caption(source, lines, levels),
        dataset_id=dataset_id,
    )

    await record_lineage(
        conn,
        principal,
        output_dataset_id=dataset_id,
        operation="contour",
        parameters={
            "levels": [float(level) for level in levels],
            "interval": interval_of(levels),
            "smoothing": request.smoothing,
            "index_every": request.index_every,
            "min_length": request.min_length,
            "source_srid": source.storage_srid,
        },
        input_dataset_ids=[source.dataset_id],
        job_id=context.job_id,
    )
    return dataset_id


async def contours_of(
    conn: AsyncConnection, principal: Principal, dataset_id: UUID
) -> list[dict[str, Any]]:
    """Contour layers derived from a grid, newest first.

    So a conversation can say "you already contoured this at 50 ft" instead of
    producing a second identical layer beside the first.
    """
    await load_and_require(conn, "dataset", dataset_id, principal, Permission.VIEWER)

    result = await conn.execute(
        text(
            """
            SELECT d.id, d.name, d.caption, d.feature_count, l.parameters,
                   l.created_at
            FROM lineage l
            JOIN dataset d ON d.id = l.output_dataset_id
            WHERE l.operation = 'contour'
              AND :id = ANY(l.input_dataset_ids)
              AND d.deleted_at IS NULL
            ORDER BY l.created_at DESC
            """
        ),
        {"id": dataset_id},
    )

    found = []
    for row in result:
        record = dict(row._mapping)
        if isinstance(record.get("parameters"), str):
            record["parameters"] = json.loads(record["parameters"])
        found.append(record)
    return found


__all__ = [
    "MAX_CONTOUR_FEATURES",
    "ContourRequest",
    "GridSource",
    "caption",
    "choose_levels",
    "contours_of",
    "index_values",
    "interval_of",
    "read_grid",
    "resolve_grid",
    "trace",
    "write_contour_dataset",
]
