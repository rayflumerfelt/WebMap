"""Gridding as a job. `05-geoprocessing.md` §6, `10-jobs-async.md` §2.

The orchestration between a job payload and `webmap_geo.interpolate`: resolve
the inputs as the requester, put the arrays in the analysis frame, solve,
write a COG, register a dataset, and record lineage.

Three rules shape every step, and none of them is optional:

- **Identity travels in the payload** (`03` §5.1). The request is gone by the
  time this runs, so every read goes through a service function taking the
  job's `Principal`. There is no service-account path.
- **Nothing is registered until it is whole** (`10` §9). The COG is uploaded
  and the dataset row written last, after cancellation has been checked for
  the final time. A half-written grid registered as a dataset is worse than a
  failed job, because it looks finished.
- **Reprojection happens here or not at all** (`05` §2.2). Control points come
  out of storage in the storage CRS and are transformed once, at this
  boundary, before `webmap_geo` sees them. The `AnalysisFrame` handed onward
  declares what the arrays already are.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from webmap_core.crs import CrsContext
from webmap_core.exceptions import NotFound
from webmap_core.jobs import JobContext
from webmap_core.logging import get_logger
from webmap_core.models import DatasetKind, Visibility
from webmap_core.permissions import Permission, Principal
from webmap_core.quota import check_grid_size, check_point_count
from webmap_core.services.datasets import create_dataset, resolve_feature_object
from webmap_core.services.ownable import load_and_require

log = get_logger(__name__)

#: Padding around the control points when no extent is given, as a fraction of
#: the control's own span. A grid clipped exactly to the data has contours
#: running off every edge, and the outermost wells sit on the boundary where a
#: minimum-curvature surface is least constrained.
DEFAULT_MARGIN = 0.05

#: Grid cells across the longer axis when no cell size is given. 200 gives a
#: surface fine enough to contour and coarse enough to solve in seconds; the
#: caller almost always overrides it with a number a geologist chose.
DEFAULT_CELLS_ACROSS = 200


@dataclass(frozen=True)
class GridRequest:
    """Everything a gridding job needs, resolved from the job payload.

    A frozen value object rather than loose keyword arguments so the lineage
    record and the idempotency key see the same parameters the solver did.
    """

    dataset_id: UUID
    value_column: str
    project_id: UUID | None = None
    method: str = "ordinary_kriging"
    cell_size: float | None = None
    #: [west, south, east, north] in EPSG:4326, as `GridSpec.bbox` is
    #: documented (`02` §5). Converted to analysis bounds at this boundary.
    bbox_4326: list[float] | None = None
    fault_dataset_id: UUID | None = None
    n_neighbors: int = 48
    max_radius: float | None = None
    tension: float = 0.0
    idw_power: float = 2.0
    where: str | None = None
    output_name: str | None = None
    visibility: Visibility = Visibility.TEAM
    owner_team_id: UUID | None = None

    def to_parameters(self) -> dict[str, Any]:
        """The job's stored parameters, and the idempotency key's input."""
        return {
            "dataset_id": str(self.dataset_id),
            "value_column": self.value_column,
            "project_id": str(self.project_id) if self.project_id else None,
            "method": self.method,
            "cell_size": self.cell_size,
            "bbox_4326": self.bbox_4326,
            "fault_dataset_id": (str(self.fault_dataset_id) if self.fault_dataset_id else None),
            "n_neighbors": self.n_neighbors,
            "max_radius": self.max_radius,
            "tension": self.tension,
            "idw_power": self.idw_power,
            "where": self.where,
            "output_name": self.output_name,
            "visibility": self.visibility.value,
            "owner_team_id": str(self.owner_team_id) if self.owner_team_id else None,
        }

    @classmethod
    def from_parameters(cls, parameters: dict[str, Any]) -> GridRequest:
        return cls(
            dataset_id=UUID(parameters["dataset_id"]),
            value_column=parameters["value_column"],
            project_id=(
                UUID(parameters["project_id"]) if parameters.get("project_id") else None
            ),
            method=parameters.get("method", "ordinary_kriging"),
            cell_size=parameters.get("cell_size"),
            bbox_4326=parameters.get("bbox_4326"),
            fault_dataset_id=(
                UUID(parameters["fault_dataset_id"])
                if parameters.get("fault_dataset_id")
                else None
            ),
            n_neighbors=parameters.get("n_neighbors", 48),
            max_radius=parameters.get("max_radius"),
            tension=parameters.get("tension", 0.0),
            idw_power=parameters.get("idw_power", 2.0),
            where=parameters.get("where"),
            output_name=parameters.get("output_name"),
            visibility=Visibility(parameters.get("visibility", "team")),
            owner_team_id=(
                UUID(parameters["owner_team_id"]) if parameters.get("owner_team_id") else None
            ),
        )


@dataclass(frozen=True)
class ResolvedInputs:
    """What the control plane knows, gathered before any geometry is read."""

    parquet_key: str
    storage_srid: int
    analysis_srid: int
    source_name: str
    feature_count: int | None
    bbox_4326: list[float] | None
    fault_parquet_key: str | None
    fault_storage_srid: int | None


async def resolve_inputs(
    conn: AsyncConnection, principal: Principal, request: GridRequest
) -> ResolvedInputs:
    """Permission-checked lookup of everything the solve needs.

    `resolve_feature_object` is the single enforcement point for feature
    content (`02` §4.1) — RLS protects the registry row and has no reach into
    object storage, so reading `parquet_key` off a row loaded some other way
    is the bug that function exists to prevent.
    """
    parquet_key, _ = await resolve_feature_object(conn, principal, request.dataset_id)

    row = (
        await conn.execute(
            text(
                "SELECT name, kind, storage_srid, feature_count, bbox_4326 "
                "FROM dataset WHERE id = :id"
            ),
            {"id": request.dataset_id},
        )
    ).one()

    if row.kind not in (DatasetKind.POINTSET, DatasetKind.VECTOR):
        raise NotFound(
            f"'{row.name}' is a {row.kind} and cannot be interpolated. Gridding "
            f"needs point control — a layer of well picks or measurements. A "
            f"grid is already a surface; to re-grid one, contour or resample it "
            f"instead."
        )

    analysis_srid = await _analysis_srid(conn, principal, request, int(row.storage_srid))

    fault_key: str | None = None
    fault_srid: int | None = None
    if request.fault_dataset_id is not None:
        fault_key, _ = await resolve_feature_object(conn, principal, request.fault_dataset_id)
        fault_srid = int(
            (
                await conn.execute(
                    text("SELECT storage_srid FROM dataset WHERE id = :id"),
                    {"id": request.fault_dataset_id},
                )
            ).scalar_one()
        )

    return ResolvedInputs(
        parquet_key=parquet_key,
        storage_srid=int(row.storage_srid),
        analysis_srid=analysis_srid,
        source_name=str(row.name),
        feature_count=int(row.feature_count) if row.feature_count is not None else None,
        bbox_4326=list(row.bbox_4326) if row.bbox_4326 else None,
        fault_parquet_key=fault_key,
        fault_storage_srid=fault_srid,
    )


async def _analysis_srid(
    conn: AsyncConnection,
    principal: Principal,
    request: GridRequest,
    storage_srid: int,
) -> int:
    """The project's analysis CRS, or the dataset's own if there is no project.

    **Never inferred and never defaulted to something plausible** (`CLAUDE.md`
    §3.1). Falling back to the storage CRS is not an inference — it is the
    frame the coordinates are already in, so the fallback transforms nothing.
    A geographic storage CRS with no project is refused rather than silently
    gridded in degrees, where a variogram range is meaningless.
    """
    if request.project_id is None:
        return storage_srid

    await load_and_require(conn, "project", request.project_id, principal, Permission.VIEWER)
    srid = (
        await conn.execute(
            text("SELECT analysis_srid FROM project WHERE id = :id"),
            {"id": request.project_id},
        )
    ).scalar_one()
    return int(srid)


def build_grid(
    control_bounds: tuple[float, float, float, float],
    crs: CrsContext,
    request: GridRequest,
    *,
    unit: str,
) -> Any:
    """The output grid definition, in the analysis frame.

    Extent comes from the request's EPSG:4326 bbox when given — the one
    conversion `05` §2.2 sanctions — and otherwise from the control points
    themselves with a margin. A grid clipped exactly to the data has contours
    running off every edge and puts the outermost wells on the boundary, which
    is where a minimum-curvature surface is least constrained.
    """
    from webmap_geo.grid import GridDefinition

    if request.bbox_4326 is not None:
        bounds = crs.bbox_4326_to_analysis(
            (
                request.bbox_4326[0],
                request.bbox_4326[1],
                request.bbox_4326[2],
                request.bbox_4326[3],
            )
        )
    else:
        xmin, ymin, xmax, ymax = control_bounds
        margin_x = max((xmax - xmin) * DEFAULT_MARGIN, 1e-9)
        margin_y = max((ymax - ymin) * DEFAULT_MARGIN, 1e-9)
        bounds = (xmin - margin_x, ymin - margin_y, xmax + margin_x, ymax + margin_y)

    span = max(bounds[2] - bounds[0], bounds[3] - bounds[1])
    cell_size = request.cell_size or _nice_cell_size(span / DEFAULT_CELLS_ACROSS)

    # `covering` rounds outward, so the requested extent is fully inside — a
    # grid that stopped short would leave control points outside the surface
    # they produced. Its cell count is what the quota check needs, so the
    # definition is built first and checked second.
    grid = GridDefinition.covering(bounds, cell_size, crs.frame)

    # Checked against the policy as well as GridDefinition's own soft limit,
    # because this message names a cell size that would fit rather than only
    # the count (`CLAUDE.md` §8).
    check_grid_size(grid.nx, grid.ny, cell_size, unit)
    return grid


def _nice_cell_size(value: float) -> float:
    """Round a derived cell size to something a geologist would have typed.

    An automatic 263.4 ft cell appears in the lineage record and in the
    caption, and it invites the reader to wonder what was special about 263.4.
    Nothing was.
    """
    from math import floor, log10

    if value <= 0:
        return 1.0
    magnitude = 10.0 ** floor(log10(value))
    for step in (1.0, 2.0, 2.5, 5.0, 10.0):
        if value <= step * magnitude:
            return step * magnitude
    return 10.0 * magnitude


async def load_control(
    inputs: ResolvedInputs,
    request: GridRequest,
    crs: CrsContext,
    object_store: Any,
    bucket: str,
) -> Any:
    """Read the control points and put them in the analysis frame.

    `object_store` is DuckDB's httpfs configuration, **not** the boto3 client
    that writes objects. They are two clients for the same bucket: reads go
    through DuckDB so the Parquet predicate pushdown happens in the engine,
    and writes go through boto3 because a COG arrives as a file. Passing one
    where the other belongs fails with an AttributeError deep inside a solve.

    **The one reprojection in this pipeline.** Points come out of storage in
    the storage CRS; `webmap_geo` receives an `AnalysisFrame` declaring what
    they already are, and never transforms anything itself.
    """
    import numpy as np

    from webmap_geo.control import ControlPoints, read_control_points

    control = read_control_points(
        f"s3://{bucket}/{inputs.parquet_key}",
        request.value_column,
        # Declared as storage for now: the arrays are in the storage CRS at
        # this point, and mislabelling them here would put the wrong srid in
        # the lineage record if the transform below turns out to be a no-op.
        crs_frame(inputs.storage_srid),
        object_store,
        where=request.where,
    )
    check_point_count(len(control))

    if inputs.storage_srid == inputs.analysis_srid:
        return control

    x, y = crs.to_analysis(control.coords[:, 0], control.coords[:, 1])
    return ControlPoints(
        coords=np.column_stack([x, y]),
        values=control.values,
        frame=crs.frame,
        value_column=control.value_column,
        n_dropped_no_value=control.n_dropped_no_value,
        n_dropped_not_a_point=control.n_dropped_not_a_point,
        n_coincident=control.n_coincident,
    )


def crs_frame(srid: int) -> Any:
    """An `AnalysisFrame` for a srid, without importing `webmap_geo.crs` here."""
    from webmap_geo.crs import frame_for

    return frame_for(srid)


async def write_grid_dataset(
    conn: AsyncConnection,
    principal: Principal,
    context: JobContext,
    request: GridRequest,
    inputs: ResolvedInputs,
    result: Any,
    *,
    store: Any,
    bucket: str,
) -> UUID:
    """Upload the COG and register it, in that order.

    **Object first, row second** — the same rule ingest follows. An orphaned
    object is recoverable by a sweep; a dataset row pointing at a key that was
    never written is a layer that 404s forever and looks like a permission
    problem.

    The dataset id is minted here because the storage key embeds it.
    """
    from uuid import uuid4

    import numpy as np

    from webmap_io.raster import Affine, write_cog
    from webmap_io.storage import grid_key, put_file

    dataset_id = uuid4()
    key = grid_key(str(dataset_id))
    grid = result.grid

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "grid.tif"
        write_cog(
            np.asarray(result.surface, dtype=np.float64),
            # The grid's own transform, written from the extent's *corner*.
            # Recomputing it from xmin/ymax here would reintroduce the
            # half-cell offset that makes a grid disagree with the points
            # that produced it.
            Affine(*grid.transform()),
            inputs.analysis_srid,
            path,
        )
        put_file(store, bucket, key, path)

    name = request.output_name or (
        f"{inputs.source_name} — {request.value_column} "
        f"({result.method.value.replace('_', ' ')})"
    )

    await create_dataset(
        conn,
        principal,
        name=name,
        kind=DatasetKind.GRID,
        storage_srid=inputs.analysis_srid,
        connector="derived",
        project_id=request.project_id,
        cog_key=key,
        bbox_4326=inputs.bbox_4326,
        owner_team_id=request.owner_team_id,
        visibility=request.visibility,
        caption=_caption(result, inputs),
        description=result.describe(),
        dataset_id=dataset_id,
    )

    await record_lineage(
        conn,
        principal,
        output_dataset_id=dataset_id,
        operation="interpolate",
        parameters=result.lineage,
        input_dataset_ids=[
            i for i in (request.dataset_id, request.fault_dataset_id) if i is not None
        ],
        job_id=context.job_id,
    )
    return dataset_id


def _caption(result: Any, inputs: ResolvedInputs) -> str:
    """The one line Claude reads about this grid (`04-mcp-server.md` §6.1).

    Leads with what the surface is, then with the number most likely to change
    someone's mind about trusting it. An extrapolated fraction belongs in a
    caption rather than only in diagnostics, because the caption is what
    travels with the layer into a conversation.
    """
    parts = [f"{result.describe()}, from {inputs.source_name}"]
    fraction = float(result.diagnostics.get("extrapolated_fraction", 0.0))
    if fraction > 0.01:
        parts.append(f"{fraction:.0%} extrapolated")
    validation = result.diagnostics.get("cross_validation")
    if validation and validation.get("rmse") is not None:
        parts.append(f"cross-validation RMSE {validation['rmse']:.4g}")
    return "; ".join(parts)


async def record_lineage(
    conn: AsyncConnection,
    principal: Principal,
    *,
    output_dataset_id: UUID,
    operation: str,
    parameters: dict[str, Any],
    input_dataset_ids: list[UUID],
    job_id: UUID | None = None,
) -> UUID:
    """`02-data-model.md` §3.11. Enough to re-run and reproduce the identical grid.

    `webmap_geo_version` is a column rather than a parameter because it is the
    one input nobody supplies and everybody forgets: a grid re-run under a
    different solver version is not the same grid, and saying so is cheaper
    than discovering it from a map that no longer matches a partner deck.
    """
    import json

    from webmap_geo import __version__

    result = await conn.execute(
        text(
            """
            INSERT INTO lineage (
                output_dataset_id, operation, parameters, input_dataset_ids,
                webmap_geo_version, job_id, created_by)
            VALUES (
                :output, :operation, CAST(:parameters AS jsonb), :inputs,
                :version, :job_id, :by)
            RETURNING id
            """
        ),
        {
            "output": output_dataset_id,
            "operation": operation,
            "parameters": json.dumps(parameters, default=str),
            "inputs": input_dataset_ids,
            "version": __version__,
            "job_id": job_id,
            "by": principal.user_id,
        },
    )
    return UUID(str(result.scalar_one()))


async def get_lineage(
    conn: AsyncConnection, principal: Principal, dataset_id: UUID
) -> dict[str, Any] | None:
    """How this dataset was made, if it was derived.

    Permission-checked on the *output*: lineage names the inputs, and being
    able to see a grid is not the same as being able to see the wells behind
    it — but the names of those inputs are part of understanding the grid, so
    they are reported while their contents remain protected.
    """
    await load_and_require(conn, "dataset", dataset_id, principal, Permission.VIEWER)

    row = (
        await conn.execute(
            text(
                """
                SELECT operation, parameters, input_dataset_ids,
                       webmap_geo_version, job_id, created_at
                FROM lineage WHERE output_dataset_id = :id
                ORDER BY created_at DESC LIMIT 1
                """
            ),
            {"id": dataset_id},
        )
    ).one_or_none()

    if row is None:
        return None

    record = dict(row._mapping)
    if isinstance(record.get("parameters"), str):
        import json

        record["parameters"] = json.loads(record["parameters"])
    return record


__all__ = [
    "DEFAULT_CELLS_ACROSS",
    "DEFAULT_MARGIN",
    "GridRequest",
    "ResolvedInputs",
    "build_grid",
    "get_lineage",
    "load_control",
    "record_lineage",
    "resolve_inputs",
    "write_grid_dataset",
]
