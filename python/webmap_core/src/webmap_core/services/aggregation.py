"""Spatial aggregation as a job. `05-geoprocessing.md` §8.

The orchestration around `webmap_geo.aggregate`: resolve each input layer as
the requesting principal, read them in the analysis frame, dispatch, and
register the result as a vector layer with lineage to **every** input.

**Lineage names all of them.** A clip has two parents and a dissolve has one,
and a derived layer that records only the first is a layer nobody can
reproduce — which is the whole point of the record (`CLAUDE.md` §3.3).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from webmap_core.jobs import JobContext
from webmap_core.logging import get_logger
from webmap_core.models import DatasetKind, GeometryKind, Visibility
from webmap_core.permissions import Permission, Principal
from webmap_core.services.datasets import create_dataset, resolve_feature_object
from webmap_core.services.gridding import record_lineage
from webmap_core.services.ownable import load_and_require

log = get_logger(__name__)

#: Reading a whole layer into Python is bounded. Above this an operation is
#: not "slow", it is a worker that stops answering — and the number is far
#: enough above a normal working layer that hitting it means the wrong layer
#: was chosen rather than that the limit is too low.
MAX_INPUT_FEATURES = 250_000

#: An aggregation that produces more features than this is not a map. The
#: usual cause is a spatial join fanning out: every left feature repeated once
#: per match, which is what a join means and what surprises people.
MAX_OUTPUT_FEATURES = 500_000


@dataclass(frozen=True)
class AggregateRequest:
    op: str
    dataset_ids: list[UUID]
    params: dict[str, Any] = field(default_factory=dict)
    output_name: str | None = None
    project_id: UUID | None = None
    visibility: Visibility = Visibility.TEAM
    owner_team_id: UUID | None = None

    def to_parameters(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "dataset_ids": [str(value) for value in self.dataset_ids],
            "params": self.params,
            "output_name": self.output_name,
            "project_id": str(self.project_id) if self.project_id else None,
            "visibility": self.visibility.value,
            "owner_team_id": str(self.owner_team_id) if self.owner_team_id else None,
        }

    @classmethod
    def from_parameters(cls, parameters: dict[str, Any]) -> AggregateRequest:
        return cls(
            op=parameters["op"],
            dataset_ids=[UUID(value) for value in parameters["dataset_ids"]],
            params=parameters.get("params") or {},
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
class LayerSource:
    dataset_id: UUID
    parquet_key: str
    storage_srid: int
    name: str
    feature_count: int | None
    bbox_4326: list[float] | None
    project_id: UUID | None


async def resolve_inputs(
    conn: AsyncConnection, principal: Principal, request: AggregateRequest
) -> list[LayerSource]:
    """Permission-checked lookup of every input layer.

    `resolve_feature_object` is the single enforcement point for feature
    content (`02` §4.1). Each input goes through it — a two-layer overlay that
    checked only the first would let a clip read a layer the caller cannot see,
    through the geometry of one they can.
    """
    sources: list[LayerSource] = []
    for dataset_id in request.dataset_ids:
        parquet_key, _version = await resolve_feature_object(conn, principal, dataset_id)
        row = (
            await conn.execute(
                text(
                    "SELECT name, storage_srid, feature_count, bbox_4326, project_id "
                    "FROM dataset WHERE id = :id"
                ),
                {"id": dataset_id},
            )
        ).one()
        sources.append(
            LayerSource(
                dataset_id=dataset_id,
                parquet_key=parquet_key,
                storage_srid=int(row.storage_srid),
                name=str(row.name),
                feature_count=row.feature_count,
                bbox_4326=list(row.bbox_4326) if row.bbox_4326 else None,
                project_id=row.project_id,
            )
        )
    return sources


def check_inputs(request: AggregateRequest, sources: list[LayerSource]) -> None:
    """Refuse before reading anything, so the message names the layer."""
    from webmap_geo.aggregate import ARITY
    from webmap_geo.exceptions import DegenerateInput

    if request.op not in ARITY:
        from webmap_geo.aggregate import operations

        raise DegenerateInput(
            f"'{request.op}' is not a spatial aggregation. Available: "
            f"{', '.join(operations())}."
        )

    for source in sources:
        if source.feature_count and source.feature_count > MAX_INPUT_FEATURES:
            raise DegenerateInput(
                f"'{source.name}' has {source.feature_count:,} features (limit "
                f"{MAX_INPUT_FEATURES:,} for an aggregation input). Filter it to an "
                f"area of interest first, or clip it against a smaller layer."
            )

    # Every operation is a distance, area or containment question, and `05` §8
    # requires all of them to run in the project analysis CRS. Two layers in
    # different storage frames would silently not overlap.
    frames = {source.storage_srid for source in sources}
    if len(frames) > 1:
        named = ", ".join(f"{s.name} (EPSG:{s.storage_srid})" for s in sources)
        raise DegenerateInput(
            f"These layers are stored in different coordinate systems — {named}. "
            f"An overlay between them would return nothing rather than fail, "
            f"because the coordinates do not occupy the same space. Reproject one "
            f"to match before aggregating."
        )


def run(
    request: AggregateRequest,
    sources: list[LayerSource],
    object_store: Any,
    bucket: str,
) -> Any:
    """Read the inputs and dispatch. Pure geoprocessing — no database.

    **`object_store` is DuckDB's httpfs configuration, not the boto3 client**
    that `write_result` uses. They are two clients for one bucket: reads go
    through DuckDB so Parquet predicate pushdown happens in the engine, writes
    go through boto3 because a Parquet file arrives as bytes. Passing one where
    the other belongs raises `AttributeError: 'S3' object has no attribute
    'endpoint'` from inside the read — which is exactly what it did here the
    first time, and is why `gridding.load_control` carries the same warning.
    """
    from webmap_geo.aggregate import Stat, aggregate, read_feature_set
    from webmap_geo.crs import frame_for
    from webmap_geo.exceptions import DegenerateInput

    frame = frame_for(sources[0].storage_srid)
    inputs = [
        read_feature_set(f"s3://{bucket}/{source.parquet_key}", frame, object_store)
        for source in sources
    ]

    params = dict(request.params)
    # `stats` arrives over JSON as a list of objects and has to become the
    # frozen value object before it reaches the catalog, where its validation
    # lives. Doing it here means a bad statistic is a 400 from the job rather
    # than a TypeError inside the aggregation.
    if params.get("stats"):
        params["stats"] = [
            Stat(op=item["op"], name=item["name"], field=item.get("field"))
            for item in params["stats"]
        ]

    result = aggregate(request.op, inputs, **params)

    if len(result) == 0:
        raise DegenerateInput(
            f"'{request.op}' produced no features. "
            + (
                "The layers may not overlap — check they cover the same ground."
                if len(sources) > 1
                else "Every input feature was consumed; check the parameters."
            )
        )
    if len(result) > MAX_OUTPUT_FEATURES:
        raise DegenerateInput(
            f"'{request.op}' produced {len(result):,} features (limit "
            f"{MAX_OUTPUT_FEATURES:,}). A spatial join fans out — every left "
            f"feature is repeated once per match — so a coarse predicate over two "
            f"dense layers multiplies quickly. Narrow the inputs or the predicate."
        )
    return result


def geometry_kind_of(result: Any) -> GeometryKind:
    """The kind to register, from what the operation actually produced.

    Read off the output rather than assumed from the operation: `centroid`
    turns polygons into points, `dissolve` can turn many polygons into one
    multipolygon, and an overlay can produce a mixture. Registering the input's
    kind would mislabel the layer and break the symbology the frontend picks
    for it.
    """
    import shapely

    kinds = {
        shapely.get_type_id(geom)
        for geom in result.geometry
        if geom is not None and not geom.is_empty
    }
    point = {0, 4}
    line = {1, 5}
    polygon = {3, 6}
    if kinds <= point:
        return GeometryKind.POINT
    if kinds <= line:
        return GeometryKind.LINESTRING
    if kinds <= polygon:
        return GeometryKind.POLYGON
    return GeometryKind.MIXED


def caption(request: AggregateRequest, sources: list[LayerSource], result: Any) -> str:
    """`04-mcp-server.md` §6.1. Names the operation and its inputs, because
    "1,204 features" says nothing about where they came from."""
    names = " and ".join(source.name for source in sources)
    return f"{len(result):,} features from {request.op} of {names}"


def attribute_schema(result: Any) -> list[dict[str, str]]:
    """Infer the output schema from the props actually produced.

    A union of keys across features, not the first record's: an overlay's
    left-only and right-only pieces carry different attributes, and a schema
    taken from feature zero would omit half of them.
    """
    types: dict[str, str] = {}
    for record in result.props:
        for key, value in record.items():
            if key in types:
                continue
            if isinstance(value, bool):
                types[key] = "boolean"
            elif isinstance(value, int | float):
                types[key] = "double"
            else:
                types[key] = "text"
    return [{"name": name, "type": types[name]} for name in sorted(types)]


async def write_result(
    conn: AsyncConnection,
    principal: Principal,
    context: JobContext,
    request: AggregateRequest,
    sources: list[LayerSource],
    result: Any,
    *,
    store: Any,
    bucket: str,
) -> UUID:
    """Write the GeoParquet object, then register it.

    Object first, row second — an orphaned object is recoverable by a sweep; a
    row pointing at a key that was never written is a layer that 404s forever
    and looks like a permission problem.
    """
    import tempfile
    from pathlib import Path

    from webmap_io.parquet import write_features
    from webmap_io.storage import feature_key, put_bytes

    dataset_id = uuid4()
    key = feature_key(str(dataset_id), 1)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "aggregate.parquet"
        write_features(
            path,
            geometry=result.geometry,
            props=result.props,
            srid=sources[0].storage_srid,
        )
        put_bytes(store, bucket, key, path.read_bytes())

    name = request.output_name or f"{sources[0].name} {request.op}"
    await create_dataset(
        conn,
        principal,
        name=name,
        kind=DatasetKind.VECTOR,
        geometry_kind=geometry_kind_of(result),
        storage_srid=sources[0].storage_srid,
        connector="derived",
        project_id=request.project_id or sources[0].project_id,
        parquet_key=key,
        feature_count=len(result),
        # Inherited from the first input rather than recomputed: every
        # operation here either shrinks the extent or keeps it, so the input's
        # bounds are a valid superset, and reprojecting a planar bound to 4326
        # would be a second avoidable conversion.
        bbox_4326=sources[0].bbox_4326,
        attribute_schema=attribute_schema(result),
        owner_team_id=request.owner_team_id,
        visibility=request.visibility,
        caption=caption(request, sources, result),
        dataset_id=dataset_id,
    )

    await record_lineage(
        conn,
        principal,
        output_dataset_id=dataset_id,
        operation=f"aggregate.{request.op}",
        parameters={
            "op": request.op,
            "params": _jsonable(request.params),
            "source_srid": sources[0].storage_srid,
            "input_names": [source.name for source in sources],
        },
        input_dataset_ids=[source.dataset_id for source in sources],
        job_id=context.job_id,
    )
    return dataset_id


def _jsonable(params: dict[str, Any]) -> dict[str, Any]:
    """Parameters as they will be stored, so a replay reads what ran."""
    encoded: dict[str, Any] = json.loads(json.dumps(params, default=str))
    return encoded


async def derived_from(
    conn: AsyncConnection, principal: Principal, dataset_id: UUID
) -> list[dict[str, Any]]:
    """Aggregations derived from a layer, newest first.

    So a conversation can say "you already buffered this by 500 ft" rather than
    producing a second identical layer beside the first.
    """
    await load_and_require(conn, "dataset", dataset_id, principal, Permission.VIEWER)

    result = await conn.execute(
        text(
            """
            SELECT d.id, d.name, d.caption, d.feature_count, l.operation,
                   l.parameters, l.created_at
            FROM lineage l
            JOIN dataset d ON d.id = l.output_dataset_id
            WHERE l.operation LIKE 'aggregate.%'
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
    "MAX_INPUT_FEATURES",
    "MAX_OUTPUT_FEATURES",
    "AggregateRequest",
    "LayerSource",
    "attribute_schema",
    "caption",
    "check_inputs",
    "derived_from",
    "geometry_kind_of",
    "resolve_inputs",
    "run",
    "write_result",
]
