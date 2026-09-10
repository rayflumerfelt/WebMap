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
    #: Also fill the intervals between levels, as polygons (`08` §5.2).
    #:
    #: *Also*, not *instead*: bands without their contours is a map you cannot
    #: read a value off, and producing both from one call is what guarantees
    #: the fill edge sits under the line. Two datasets, one job, one level
    #: list.
    fill: bool = False
    #: Units per pixel the label gaps are cut for (`adr/0015`). A label is a
    #: fixed number of pixels wide and a gap a fixed number of feet, so the two
    #: agree at one scale only. `None` uses half the grid's cell size, which is
    #: about where a contour map stops showing detail the grid does not have.
    label_scale: float | None = None
    #: Distance along a contour between labels, in the grid's units. `None`
    #: takes a quarter of the grid's width, so a contour crossing the map is
    #: labelled about four times.
    label_spacing: float | None = None
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
            "label_scale": self.label_scale,
            "label_spacing": self.label_spacing,
            "fill": self.fill,
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
            fill=bool(parameters.get("fill", False)),
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


def fill_bands(surface: Any, grid: Any, levels: Any, request: ContourRequest) -> list[Any]:
    """Fill the intervals between the same levels the lines were traced at.

    No separate density guard: band complexity tracks line complexity, so an
    interval `trace` already accepted produces a band set of the same order.
    Guarding twice on the same quantity would let the two limits drift apart
    and start refusing filled maps whose contours were fine.
    """
    from webmap_geo.contour.bands import contour_bands
    from webmap_geo.exceptions import DegenerateInput

    bands = contour_bands(
        surface,
        grid,
        levels=levels,
        smoothing=request.smoothing,
    )
    if not bands:
        raise DegenerateInput(
            f"No filled bands were produced at levels {levels.min():g} to "
            f"{levels.max():g}. Every level fell outside the surface's range, "
            f"so there are no intervals to fill."
        )
    return bands


async def write_band_dataset(
    conn: AsyncConnection,
    principal: Principal,
    context: JobContext,
    request: ContourRequest,
    source: GridSource,
    bands: list[Any],
    levels: Any,
    *,
    store: Any,
    bucket: str,
) -> UUID:
    """Write the filled bands as their own polygon layer.

    A separate dataset rather than extra columns on the contour layer: they
    are different geometry, they are styled differently, and a geologist turns
    the fill off to read the lines. One layer that is both cannot do that.
    """
    import tempfile
    from pathlib import Path

    from webmap_io.parquet import write_features
    from webmap_io.storage import feature_key, put_bytes

    dataset_id = uuid4()
    key = feature_key(str(dataset_id), 1)

    geometry = np.asarray([band.geometry for band in bands], dtype=object)
    props: list[dict[str, Any]] = [
        {
            "lower": band.lower,
            "upper": band.upper,
            # The value one colour stands for, so a legend swatch has a number
            # rather than a range to place on a ramp.
            "midpoint": band.midpoint,
            # The outermost bands mean "below" and "above", not a closed
            # interval — a legend that labels them as one claims a floor and a
            # ceiling the data does not have.
            "is_open_ended": band.is_open_ended,
            # Precomputed because it is the question a filled map is drawn to
            # answer, and because recomputing it in the browser would be in
            # web-mercator metres rather than the analysis frame's units.
            "area": band.area,
        }
        for band in bands
    ]

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bands.parquet"
        write_features(path, geometry=geometry, props=props, srid=source.storage_srid)
        put_bytes(store, bucket, key, path.read_bytes())

    name = request.output_name or f"{source.name} contours"
    await create_dataset(
        conn,
        principal,
        name=f"{name} (filled)",
        kind=DatasetKind.VECTOR,
        geometry_kind=GeometryKind.POLYGON,
        storage_srid=source.storage_srid,
        connector="derived",
        project_id=request.project_id or source.project_id,
        parquet_key=key,
        feature_count=len(bands),
        bbox_4326=source.bbox_4326,
        attribute_schema=[
            {"name": "lower", "type": "double"},
            {"name": "upper", "type": "double"},
            {"name": "midpoint", "type": "double"},
            {"name": "is_open_ended", "type": "boolean"},
            {"name": "area", "type": "double"},
        ],
        owner_team_id=request.owner_team_id,
        visibility=request.visibility,
        caption=band_caption(source, bands, levels),
        dataset_id=dataset_id,
    )

    await record_lineage(
        conn,
        principal,
        output_dataset_id=dataset_id,
        operation="contour_bands",
        parameters={
            "levels": [float(level) for level in levels],
            "interval": interval_of(levels),
            "smoothing": request.smoothing,
            "source_srid": source.storage_srid,
        },
        input_dataset_ids=[source.dataset_id],
        job_id=context.job_id,
    )
    return dataset_id


def band_caption(source: GridSource, bands: list[Any], levels: Any) -> str:
    """Names the interval, as the line caption does, so the pair read alike."""
    interval = interval_of(levels)
    return (
        f"{len(bands):,} filled bands of {source.name} at {interval:g} intervals, "
        f"{float(np.min(levels)):g} to {float(np.max(levels)):g}"
    )


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


@dataclass(frozen=True)
class ContourOutput:
    """What a contour run produced.

    Three counts rather than one, because they answer different questions and
    a single "feature count" answers none of them well: a geologist asks how
    many contours, the map asks how many features it must draw, and a support
    question about a missing label asks how many labels were placed.
    """

    dataset_id: UUID
    #: Rows written — line pieces and label points together.
    feature_count: int
    #: Contours, as a reader counts them: one per traced line.
    contour_count: int
    label_count: int


#: Pixels across a map pane on a workstation, for the default label scale.
#: `07-frontend.md` §5.1 targets 1440 px wide with panels either side, so the
#: map itself is about this. It only has to be right to within a factor of two:
#: it sets how long a gap is, and a gap half a character out is invisible.
REFERENCE_VIEW_PX = 1000.0


def label_text(value: float, levels: Any) -> str:
    """The string drawn on the contour, formatted here rather than in the style.

    **The gap was cut for this exact string** (`adr/0015`), so the string has to
    travel with the geometry — a style that formatted the number itself could
    render "-12800.0" into a gap cut for "-12,800" and overflow it.

    Decimals come from the levels: a 25 ft interval reads as integers and a
    0.25 ft interval does not, and showing "-12,800.00" on a structure map is
    four characters of noise on every contour.
    """
    decimals = 0
    for level in levels:
        remainder = abs(float(level) - round(float(level)))
        if remainder > 1e-9:
            decimals = 2
            break
    return f"{float(value):,.{decimals}f}"


def contour_features(
    lines: list[Any],
    levels: Any,
    *,
    grid: Any,
    request: ContourRequest,
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Contour geometry with label gaps cut into it, and the labels.

    Only **index** contours are labelled and therefore only index contours are
    cut — `05` §7 already says the intermediates carry no label, and cutting an
    unlabelled line would be a break with nothing in it.

    Returns geometry and properties in step, ready for `write_features`.
    """
    from webmap_geo.contour.labels import gap_length, label_contour

    width = grid.nx * grid.cell_size
    # The scale the map is *read* at: the whole grid across a map pane. Half a
    # cell per pixel was the first guess and it is far too fine — on a 4,600 ft
    # grid it puts the whole surface in 200 pixels and asks for a gap 144,000 ft
    # long, wider than the spacing between labels. A gap is only meaningful at
    # the scale somebody looks at the map (`adr/0015`).
    scale = request.label_scale or width / REFERENCE_VIEW_PX
    spacing = request.label_spacing or width / 4.0

    geometry: list[Any] = []
    props: list[dict[str, Any]] = []

    for line in lines:
        base = {
            "value": float(line.value),
            # The flag a style reads to draw every fifth line heavier. Without
            # it a dense structure map is a field of identical hairlines.
            "is_index": bool(line.is_index),
            "closed": bool(line.closed),
        }

        if not line.is_index:
            geometry.append(line.geometry)
            props.append({**base, "kind": "contour", "bearing": None, "label": None})
            continue

        text = label_text(line.value, levels)
        gap = gap_length(text, metres_per_pixel=scale)
        labelled = label_contour(
            line.geometry,
            float(line.value),
            gap=gap,
            # Never closer together than three gaps, whatever the default or
            # the caller worked out: labels packed tighter than that leave a
            # contour that is more break than line.
            spacing=max(spacing, 3.0 * gap),
        )

        for piece in labelled.pieces:
            geometry.append(piece)
            props.append({**base, "kind": "contour", "bearing": None, "label": None})
        for label in labelled.labels:
            geometry.append(label.point)
            props.append(
                {
                    **base,
                    "kind": "label",
                    # `text-rotate` degrees, so the label follows the contour.
                    "bearing": float(label.bearing),
                    "label": text,
                }
            )

    return geometry, props


async def write_contour_dataset(
    conn: AsyncConnection,
    principal: Principal,
    context: JobContext,
    request: ContourRequest,
    source: GridSource,
    lines: list[Any],
    levels: Any,
    *,
    grid: Any,
    store: Any,
    bucket: str,
) -> ContourOutput:
    """Write the GeoParquet object, then register it.

    The grid comes in because the label gaps are sized from its cell size and
    spaced across its width (`adr/0015`); nothing else here needs it.

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

    features, props = contour_features(lines, levels, grid=grid, request=request)
    geometry = np.asarray(features, dtype=object)
    label_count = sum(1 for entry in props if entry["kind"] == "label")

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
        # Lines and their label anchors travel together: the gap and the label
        # that sits in it are one decision, and splitting them across two
        # datasets is how they come to disagree (`adr/0015`).
        geometry_kind=GeometryKind.MIXED,
        storage_srid=source.storage_srid,
        connector="derived",
        project_id=request.project_id or source.project_id,
        parquet_key=key,
        feature_count=len(features),
        # Inherited from the grid rather than recomputed: contours are a
        # subset of the surface's extent by construction, and reprojecting a
        # planar bound to 4326 here would be a second, avoidable conversion.
        bbox_4326=source.bbox_4326,
        attribute_schema=[
            {"name": "value", "type": "double"},
            {"name": "is_index", "type": "boolean"},
            {"name": "closed", "type": "boolean"},
            # 'contour' or 'label'. The style draws the first as lines and the
            # second as point-placed text rotated by `bearing`.
            {"name": "kind", "type": "text"},
            {"name": "bearing", "type": "double"},
            {"name": "label", "type": "text"},
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
    return ContourOutput(
        dataset_id=dataset_id,
        feature_count=len(features),
        contour_count=len(lines),
        label_count=label_count,
    )


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
    "band_caption",
    "caption",
    "choose_levels",
    "contours_of",
    "fill_bands",
    "index_values",
    "interval_of",
    "read_grid",
    "resolve_grid",
    "trace",
    "write_band_dataset",
    "write_contour_dataset",
]
