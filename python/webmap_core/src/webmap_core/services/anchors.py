"""Label anchors as a job. `08` §2.4, `05-geoprocessing.md` §7.2.

`webmap_geo.label` computes an anchor from a geometry. This is what turns that
into a **dataset**: one point per polygon, carrying the label columns across so
the anchor layer can be styled and labelled on its own, plus how the anchor was
placed and how much room it has.

Being a dataset rather than a render-time calculation is the whole design.
MapLibre places polygon labels against the *tile-clipped* geometry, so a lease
crossing a tile boundary gets a different anchor in each tile and the label
moves while you pan. A precomputed anchor is computed once against the whole
geometry, is stable at every zoom, and — because it is a dataset — can be
inspected, corrected by hand, and exported with the map.

**One anchor per feature, including for a multipolygon.** Labelling every part
is what MapLibre already does, and it is what puts a lease name on each of its
slivers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from webmap_core.exceptions import LimitExceeded, NotFound
from webmap_core.jobs import JobContext
from webmap_core.logging import get_logger
from webmap_core.models import DatasetKind, GeometryKind, Visibility
from webmap_core.permissions import Principal
from webmap_core.services.aggregation import LayerSource
from webmap_core.services.datasets import create_dataset, resolve_feature_object
from webmap_core.services.gridding import record_lineage

log = get_logger(__name__)

#: The same bound aggregation uses, and for the same reason: reading a whole
#: layer into Python has to be bounded, and a layer past this is the wrong
#: layer rather than a limit set too low.
MAX_INPUT_FEATURES = 250_000

#: Columns the anchor layer always carries, on top of whatever label columns
#: were copied across.
ANCHOR_COLUMNS = ("source_feature_id", "anchor_method", "clearance")


@dataclass(frozen=True)
class AnchorRequest:
    """Which layer to anchor, and which of its columns the labels need."""

    dataset_id: UUID
    #: Columns to copy onto the anchor points. Empty copies **none**, which is
    #: the honest default: an anchor layer is for placing a label, and copying
    #: forty attributes to place one is a duplicate of the source layer that
    #: then drifts out of date with it.
    label_columns: tuple[str, ...] = ()
    output_name: str | None = None
    project_id: UUID | None = None
    visibility: Visibility = Visibility.TEAM
    owner_team_id: UUID | None = None

    def to_parameters(self) -> dict[str, Any]:
        return {
            "dataset_id": str(self.dataset_id),
            "label_columns": list(self.label_columns),
            "output_name": self.output_name,
            "project_id": str(self.project_id) if self.project_id else None,
            "visibility": self.visibility.value,
            "owner_team_id": str(self.owner_team_id) if self.owner_team_id else None,
        }

    @classmethod
    def from_parameters(cls, parameters: dict[str, Any]) -> AnchorRequest:
        return cls(
            dataset_id=UUID(parameters["dataset_id"]),
            label_columns=tuple(parameters.get("label_columns") or ()),
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
class AnchorResult:
    geometry: Any
    #: One record per anchor, in the shape `write_features` takes — a list of
    #: dicts rather than a dict of columns. The columnar form reads better and
    #: is the wrong shape at the boundary, and converting at the last moment is
    #: exactly where a length mismatch hides.
    props: list[dict[str, Any]]
    n_features: int
    n_anchored: int
    n_centroid: int
    n_pole: int

    @property
    def n_skipped(self) -> int:
        return self.n_features - self.n_anchored


async def resolve_source(
    conn: AsyncConnection, principal: Principal, request: AnchorRequest
) -> LayerSource:
    """Permission-checked lookup of the layer to anchor."""
    parquet_key, _version = await resolve_feature_object(conn, principal, request.dataset_id)
    row = (
        await conn.execute(
            text(
                "SELECT name, storage_srid, feature_count, bbox_4326, project_id, "
                "geometry_kind FROM dataset WHERE id = :id"
            ),
            {"id": request.dataset_id},
        )
    ).one()

    kind = str(row.geometry_kind or "").lower()
    if kind and "polygon" not in kind:
        raise NotFound(
            f"'{row.name}' holds {kind} features, and label anchors are for "
            f"polygons. Line labels use MapLibre's `symbol-placement: "
            f"'line-center'`, which needs no anchor layer, and a point layer is "
            f"already its own anchor."
        )
    if row.feature_count and int(row.feature_count) > MAX_INPUT_FEATURES:
        raise LimitExceeded(
            f"'{row.name}' has {int(row.feature_count):,} features (limit "
            f"{MAX_INPUT_FEATURES:,}). Anchor the subset you intend to label — "
            f"a quarter of a million labels is not a map anybody reads."
        )

    return LayerSource(
        dataset_id=request.dataset_id,
        parquet_key=parquet_key,
        storage_srid=int(row.storage_srid),
        name=str(row.name),
        feature_count=int(row.feature_count) if row.feature_count else None,
        bbox_4326=list(row.bbox_4326) if row.bbox_4326 else None,
        project_id=row.project_id,
    )


def compute(
    source: LayerSource, request: AnchorRequest, object_store: Any, bucket: str
) -> AnchorResult:
    """Read the layer, anchor every polygon, and build the point layer.

    **No reprojection.** Anchors are computed in the layer's own storage frame
    and written back in it, so the anchor layer and the polygons it labels are
    the same coordinates — which is what makes an anchor correctable by hand
    against the feature it belongs to.

    Features with no polygonal area are **dropped, and counted**. `label_anchors`
    returns `None` in their place rather than a shorter list precisely so this
    zip stays aligned; the count then goes into the caption, because a lease
    layer that produced 900 anchors from 1,000 features has 100 empty geometries
    in it and that is worth knowing.
    """
    import json

    import shapely

    from webmap_geo.crs import frame_for
    from webmap_geo.dataplane import connect
    from webmap_geo.label import CENTROID, POLE, label_anchors

    with connect(object_store) as conn:
        rows = conn.execute(
            "SELECT feature_id, geometry, props FROM read_parquet($key)",
            {"key": f"s3://{bucket}/{source.parquet_key}"},
        ).fetchall()

    if len(rows) > MAX_INPUT_FEATURES:
        raise LimitExceeded(
            f"'{source.name}' has {len(rows):,} features (limit "
            f"{MAX_INPUT_FEATURES:,}); the registered count was stale."
        )

    geometries = [shapely.from_wkb(bytes(wkb)) for _fid, wkb, _props in rows]
    anchors = label_anchors(geometries, frame_for(source.storage_srid))

    points: list[Any] = []
    props: list[dict[str, Any]] = []

    n_centroid = n_pole = 0
    for (feature_id, _wkb, raw_props), anchor in zip(rows, anchors, strict=True):
        if anchor is None:
            continue
        points.append(anchor.point)

        attributes = raw_props
        if isinstance(attributes, str):
            attributes = json.loads(attributes)
        if not isinstance(attributes, dict):
            attributes = {}

        record: dict[str, Any] = {
            "source_feature_id": str(feature_id),
            "anchor_method": anchor.method,
            "clearance": float(anchor.clearance),
        }
        # `.get`, not `[...]`: a column named in the request that the layer
        # does not have becomes a null rather than a KeyError halfway through
        # the job, and the null is visible in the output where a failed job
        # after four minutes of reading is not.
        record.update({column: attributes.get(column) for column in request.label_columns})
        props.append(record)

        n_centroid += anchor.method == CENTROID
        n_pole += anchor.method == POLE

    if not points:
        raise NotFound(
            f"'{source.name}' produced no anchors — none of its {len(rows):,} "
            f"features has any polygonal area. Check the layer is polygons and "
            f"not an empty geometry column."
        )

    return AnchorResult(
        geometry=shapely.points([shapely.get_coordinates(p)[0] for p in points]),
        props=props,
        n_features=len(rows),
        n_anchored=len(points),
        n_centroid=n_centroid,
        n_pole=n_pole,
    )


async def write_result(
    conn: AsyncConnection,
    principal: Principal,
    context: JobContext,
    request: AnchorRequest,
    source: LayerSource,
    result: AnchorResult,
    *,
    store: Any,
    bucket: str,
) -> UUID:
    """Write the GeoParquet object, then register it. Object first, row second."""
    import tempfile
    from pathlib import Path

    from webmap_io.parquet import write_features
    from webmap_io.storage import feature_key, put_bytes

    dataset_id = uuid4()
    key = feature_key(str(dataset_id), 1)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "anchors.parquet"
        write_features(
            path, geometry=result.geometry, props=result.props, srid=source.storage_srid
        )
        put_bytes(store, bucket, key, path.read_bytes())

    await create_dataset(
        conn,
        principal,
        name=request.output_name or f"{source.name} — label anchors",
        kind=DatasetKind.VECTOR,
        geometry_kind=GeometryKind.POINT,
        storage_srid=source.storage_srid,
        connector="derived",
        project_id=request.project_id or source.project_id,
        parquet_key=key,
        feature_count=result.n_anchored,
        # An anchor lies inside its polygon, so the source's bounds are a valid
        # superset and reprojecting planar bounds to 4326 would be a second
        # avoidable conversion.
        bbox_4326=source.bbox_4326,
        attribute_schema=_schema(request, result),
        owner_team_id=request.owner_team_id,
        visibility=request.visibility,
        caption=caption(source, result),
        description=(
            "Precomputed label anchors, one per polygon. Style this layer as a "
            "symbol layer with no icon; the polygons keep their own fill."
        ),
        dataset_id=dataset_id,
    )

    await record_lineage(
        conn,
        principal,
        output_dataset_id=dataset_id,
        operation="label_anchors",
        parameters={
            "label_columns": list(request.label_columns),
            "n_features": result.n_features,
            "n_anchored": result.n_anchored,
            "n_centroid": result.n_centroid,
            "n_pole": result.n_pole,
        },
        input_dataset_ids=[source.dataset_id],
        job_id=context.job_id,
    )
    log.info(
        "anchors_written",
        dataset_id=str(dataset_id),
        anchored=result.n_anchored,
        skipped=result.n_skipped,
    )
    return dataset_id


def _schema(request: AnchorRequest, result: AnchorResult) -> list[dict[str, str]]:
    """The output schema, in the `{name, type}` shape the registry stores.

    The copied columns' types are inferred from the values actually written
    rather than declared: the source schema is not read here, and guessing
    'text' for a numeric label column would make the styling UI offer the wrong
    controls for it.
    """
    types: dict[str, str] = {
        "source_feature_id": "text",
        "anchor_method": "text",
        "clearance": "double",
    }
    for record in result.props:
        for column in request.label_columns:
            value = record.get(column)
            if value is None or column in types:
                continue
            if isinstance(value, bool):
                types[column] = "boolean"
            elif isinstance(value, int | float):
                types[column] = "double"
            else:
                types[column] = "text"
    # A column that was null in every feature still belongs in the schema —
    # absent, it looks like the request asked for a column that does not exist.
    for column in request.label_columns:
        types.setdefault(column, "text")
    return [{"name": name, "type": types[name]} for name in sorted(types)]


def caption(source: LayerSource, result: AnchorResult) -> str:
    """The line Claude and the layer list read (`04` §6.1).

    Names the pole-of-inaccessibility count, because that is the interesting
    number: a layer where most anchors fell back to the pole is a layer of
    crescents and doughnuts, and that is exactly where a hand correction is
    likely to be wanted.
    """
    parts = [f"{result.n_anchored:,} label anchors from {source.name}"]
    if result.n_pole:
        parts.append(f"{result.n_pole:,} placed by pole of inaccessibility")
    if result.n_skipped:
        parts.append(f"{result.n_skipped:,} features had no polygonal area")
    return "; ".join(parts)


__all__ = [
    "ANCHOR_COLUMNS",
    "MAX_INPUT_FEATURES",
    "AnchorRequest",
    "AnchorResult",
    "caption",
    "compute",
    "resolve_source",
    "write_result",
]
