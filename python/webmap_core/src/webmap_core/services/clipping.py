"""Clipping a grid to a polygon, as a job. `08` §5.2.

Orchestration only: resolve two datasets under the caller's permissions, read
the grid and the boundary, reproject the boundary into the grid's frame, hand
both to `webmap_geo.clip`, and register the result with lineage to **both**
inputs.

**Both inputs are permission-checked.** Clipping is a read of the polygon layer
as much as of the grid — the output's shape is the boundary's shape, and a
lease outline is recoverable from a clipped grid by looking at it. The same
hole `aggregation.resolve_inputs` closes for operands.

**The boundary is reprojected, once, at this boundary.** `webmap_geo.clip`
takes geometry already in the grid's frame and never transforms
(`adr/0003`), and a clip against a polygon in a different CRS removes the
wrong half of the map without failing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from webmap_core.exceptions import LimitExceeded, NotFound
from webmap_core.jobs import JobContext
from webmap_core.logging import get_logger
from webmap_core.models import DatasetKind, Visibility
from webmap_core.permissions import Principal
from webmap_core.services.contours import GridSource, read_grid
from webmap_core.services.datasets import (
    create_dataset,
    resolve_feature_object,
    resolve_grid_object,
)
from webmap_core.services.gridding import display_range, record_lineage

log = get_logger(__name__)

#: More boundary features than this and the union is the slow part rather than
#: the clip. A clip to 5,000 lease outlines is a question about the lease
#: layer, not about the grid.
MAX_BOUNDARY_FEATURES = 2_000


@dataclass(frozen=True)
class ClipRequest:
    """What to clip, to what, and which way round."""

    dataset_id: UUID
    boundary_dataset_id: UUID | None = None
    #: Clip to selected features of the boundary layer rather than all of it.
    #: `08` §5.2 requires the selection case; empty means the whole layer.
    feature_ids: tuple[str, ...] = ()
    #: Exclude the boundary instead of keeping it — a no-permit block, a lease
    #: to leave out.
    invert: bool = False
    #: The "clip to the control" preset — 'convex_hull', 'concave_hull' or
    #: 'radius'. Mutually exclusive with a boundary layer, and checked rather
    #: than silently preferred.
    to_control: str | None = None
    #: The point dataset whose control the preset draws around. Required with
    #: `to_control` and **not inferred from the grid's lineage**: the control
    #: that made a grid belongs to the source dataset, and re-reading it
    #: through lineage would read a dataset the caller may no longer be able
    #: to see.
    control_dataset_id: UUID | None = None
    output_name: str | None = None
    project_id: UUID | None = None
    visibility: Visibility = Visibility.PRIVATE
    owner_team_id: UUID | None = None

    def __post_init__(self) -> None:
        if (self.boundary_dataset_id is None) == (self.to_control is None):
            raise ValueError(
                "A clip needs exactly one boundary: either a polygon layer "
                "(boundary_dataset_id) or the 'clip to the control' preset "
                "(to_control). Giving both leaves it ambiguous which one the "
                "map was cut to, which is not recoverable from the result."
            )
        if self.to_control is not None and self.control_dataset_id is None:
            raise ValueError(
                "Clipping to the control needs control_dataset_id — the point "
                "layer to draw the boundary around. It is asked for rather than "
                "read from the grid's lineage, because that would read a "
                "dataset you may no longer have access to."
            )

    def to_parameters(self) -> dict[str, Any]:
        return {
            "dataset_id": str(self.dataset_id),
            "boundary_dataset_id": (
                str(self.boundary_dataset_id) if self.boundary_dataset_id else None
            ),
            "feature_ids": list(self.feature_ids),
            "invert": self.invert,
            "to_control": self.to_control,
            "control_dataset_id": (
                str(self.control_dataset_id) if self.control_dataset_id else None
            ),
            "output_name": self.output_name,
            "project_id": str(self.project_id) if self.project_id else None,
            "visibility": self.visibility.value,
            "owner_team_id": str(self.owner_team_id) if self.owner_team_id else None,
        }

    @classmethod
    def from_parameters(cls, parameters: dict[str, Any]) -> ClipRequest:
        return cls(
            dataset_id=UUID(parameters["dataset_id"]),
            boundary_dataset_id=(
                UUID(parameters["boundary_dataset_id"])
                if parameters.get("boundary_dataset_id")
                else None
            ),
            feature_ids=tuple(parameters.get("feature_ids") or ()),
            invert=bool(parameters.get("invert", False)),
            to_control=parameters.get("to_control"),
            control_dataset_id=(
                UUID(parameters["control_dataset_id"])
                if parameters.get("control_dataset_id")
                else None
            ),
            output_name=parameters.get("output_name"),
            project_id=(
                UUID(parameters["project_id"]) if parameters.get("project_id") else None
            ),
            visibility=Visibility(parameters.get("visibility", "private")),
            owner_team_id=(
                UUID(parameters["owner_team_id"]) if parameters.get("owner_team_id") else None
            ),
        )


@dataclass(frozen=True)
class ResolvedClip:
    grid: GridSource
    boundary_parquet_key: str | None
    boundary_storage_srid: int | None
    boundary_name: str | None
    control_parquet_key: str | None = None
    control_storage_srid: int | None = None
    control_name: str | None = None


async def resolve_inputs(
    conn: AsyncConnection, principal: Principal, request: ClipRequest
) -> ResolvedClip:
    """Both datasets, permission-checked, in one place."""
    cog_key = await resolve_grid_object(conn, principal, request.dataset_id)
    row = (
        await conn.execute(
            text(
                "SELECT name, storage_srid, bbox_4326, project_id FROM dataset WHERE id = :id"
            ),
            {"id": request.dataset_id},
        )
    ).one()

    grid = GridSource(
        dataset_id=request.dataset_id,
        cog_key=cog_key,
        storage_srid=int(row.storage_srid),
        name=str(row.name),
        bbox_4326=list(row.bbox_4326) if row.bbox_4326 else None,
        project_id=row.project_id,
    )

    if request.boundary_dataset_id is None:
        assert request.control_dataset_id is not None
        control_key, _ = await resolve_feature_object(
            conn, principal, request.control_dataset_id
        )
        control = (
            await conn.execute(
                text("SELECT name, storage_srid FROM dataset WHERE id = :id"),
                {"id": request.control_dataset_id},
            )
        ).one()
        return ResolvedClip(
            grid,
            None,
            None,
            None,
            control_parquet_key=control_key,
            control_storage_srid=int(control.storage_srid),
            control_name=str(control.name),
        )

    key, _ = await resolve_feature_object(conn, principal, request.boundary_dataset_id)
    boundary = (
        await conn.execute(
            text("SELECT name, storage_srid FROM dataset WHERE id = :id"),
            {"id": request.boundary_dataset_id},
        )
    ).one_or_none()
    if boundary is None:
        raise NotFound(f"No boundary dataset {request.boundary_dataset_id} you can access.")

    return ResolvedClip(
        grid=grid,
        boundary_parquet_key=key,
        boundary_storage_srid=int(boundary.storage_srid),
        boundary_name=str(boundary.name),
    )


def read_boundary(
    inputs: ResolvedClip,
    request: ClipRequest,
    object_store: Any,
    bucket: str,
    crs: Any,
) -> Any:
    """The clip boundary as one geometry, in the grid's frame.

    Polygons only. A line or a point in the layer is **skipped**, not refused:
    a lease layer routinely carries a survey line beside the outlines, and
    refusing the whole clip over one would make an ordinary layer unusable. A
    layer with no polygon at all *is* refused, because then there is nothing
    to clip to and blanking the grid is never what anyone meant.

    Unioned rather than iterated: `08` §5.2's "selected features" case is
    normally several adjacent tracts, and clipping to each in turn would leave
    hairline gaps along the shared edges where a cell centre falls between two
    polygons' boundaries.
    """
    import shapely

    from webmap_geo.dataplane import connect

    assert inputs.boundary_parquet_key is not None

    sql = "SELECT geometry FROM read_parquet($key)"
    params: dict[str, Any] = {"key": f"s3://{bucket}/{inputs.boundary_parquet_key}"}
    if request.feature_ids:
        sql += " WHERE feature_id IN (SELECT unnest($ids))"
        params["ids"] = list(request.feature_ids)

    with connect(object_store) as conn:
        rows = conn.execute(sql, params).fetchall()

    if len(rows) > MAX_BOUNDARY_FEATURES:
        raise LimitExceeded(
            f"The boundary layer has {len(rows):,} features (limit "
            f"{MAX_BOUNDARY_FEATURES:,}). Select the tracts you mean, or "
            f"dissolve the layer first — a clip to thousands of separate "
            f"outlines spends its time on the union rather than on the grid."
        )

    polygons = []
    for (wkb,) in rows:
        geometry = shapely.from_wkb(bytes(wkb))
        if geometry.geom_type in {"Polygon", "MultiPolygon"}:
            polygons.append(geometry)

    if not polygons:
        raise NotFound(
            f"'{inputs.boundary_name}' has no polygon features"
            + (" among the ones selected" if request.feature_ids else "")
            + ". A clip boundary is an area; lines and points in the layer are "
            "skipped, and a layer of only those has nothing to clip to."
        )

    merged = shapely.union_all(polygons)
    if inputs.boundary_storage_srid != inputs.grid.storage_srid:
        merged = _reproject(merged, crs)
    return merged


def _reproject(geometry: Any, crs: Any) -> Any:
    """Transform a polygonal geometry into the analysis frame.

    The one transformation in the clip path, and it happens here rather than
    inside `webmap_geo.clip` because `adr/0003` puts reprojection at defined
    boundaries and never mid-algorithm.
    """
    import shapely

    def transform(coords: Any) -> Any:
        import numpy as np

        xy = np.asarray(coords, dtype=float)
        x, y = crs.to_analysis(xy[:, 0], xy[:, 1])
        return np.column_stack([x, y])

    return shapely.transform(geometry, transform)


def read_control_boundary(
    inputs: ResolvedClip,
    request: ClipRequest,
    object_store: Any,
    bucket: str,
    crs: Any,
) -> Any:
    """The "clip to the control" preset (`08` §5.2), in the grid's frame.

    Reads the point layer, draws the boundary `webmap_geo.control_boundary`
    describes, and reprojects it when the layer is stored in another CRS.
    Removing the unsupported area rather than warning about it is the whole
    point of the preset, so the extrapolation fraction is recomputed against
    the survivors and lands in the caption.
    """
    import numpy as np
    import shapely

    from webmap_geo.clip import control_boundary
    from webmap_geo.dataplane import connect

    assert inputs.control_parquet_key is not None

    with connect(object_store) as conn:
        rows = conn.execute(
            "SELECT geometry FROM read_parquet($key)",
            {"key": f"s3://{bucket}/{inputs.control_parquet_key}"},
        ).fetchall()

    coordinates = []
    for (wkb,) in rows:
        geometry = shapely.from_wkb(bytes(wkb))
        # A representative point, so a layer of well paths or pad outlines
        # still yields one location each rather than being skipped.
        point = geometry if geometry.geom_type == "Point" else geometry.representative_point()
        # `representative_point` is typed as returning BaseGeometry; it returns
        # a Point, and `shapely.get_coordinates` is the accessor that says so
        # without a cast.
        xy_pair = shapely.get_coordinates(point)[0]
        coordinates.append((float(xy_pair[0]), float(xy_pair[1])))

    if len(coordinates) < 3:
        raise NotFound(
            f"'{inputs.control_name}' has {len(coordinates)} feature(s); a "
            f"control boundary needs at least 3. Two points have no interior "
            f"to clip to."
        )

    xy = np.asarray(coordinates, dtype=float)
    if inputs.control_storage_srid != inputs.grid.storage_srid:
        x, y = crs.to_analysis(xy[:, 0], xy[:, 1])
        xy = np.column_stack([x, y])

    assert request.to_control is not None
    return control_boundary(xy, method=request.to_control)  # type: ignore[arg-type]


async def write_clipped_dataset(
    conn: AsyncConnection,
    principal: Principal,
    context: JobContext,
    request: ClipRequest,
    inputs: ResolvedClip,
    result: Any,
    grid: Any,
    *,
    store: Any,
    bucket: str,
) -> UUID:
    """Upload the clipped COG and register it, object first, row second.

    A **derived grid with lineage to both inputs** (`08` §5.2), not an edit of
    the source: `CLAUDE.md` §3.4 forbids writing in place, and the clipped and
    unclipped surfaces are both legitimately wanted — the unclipped one is what
    you re-clip when the acreage changes.
    """
    import numpy as np

    from webmap_io.raster import Affine, write_cog
    from webmap_io.storage import grid_key, put_file

    dataset_id = uuid4()
    key = grid_key(str(dataset_id))

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "grid.tif"
        write_cog(
            np.asarray(result.surface, dtype=np.float64),
            Affine(*grid.transform()),
            inputs.grid.storage_srid,
            path,
        )
        put_file(store, bucket, key, path)

    name = request.output_name or (
        f"{inputs.grid.name} — "
        f"{'excluding' if request.invert else 'clipped to'} "
        f"{inputs.boundary_name or request.to_control}"
    )

    await create_dataset(
        conn,
        principal,
        name=name,
        kind=DatasetKind.GRID,
        storage_srid=inputs.grid.storage_srid,
        connector="derived",
        project_id=request.project_id or inputs.grid.project_id,
        cog_key=key,
        bbox_4326=inputs.grid.bbox_4326,
        # **Recomputed, not inherited.** `08` §5.2: or the legend spans values
        # no longer on the map.
        value_range=display_range(result.surface),
        owner_team_id=request.owner_team_id,
        visibility=request.visibility,
        caption=caption(request, inputs, result),
        description=(
            f"{result.clipped_fraction:.0%} of the source grid's cells are nodata "
            f"after the clip."
        ),
        dataset_id=dataset_id,
    )

    await record_lineage(
        conn,
        principal,
        output_dataset_id=dataset_id,
        operation="clip",
        parameters={
            "invert": request.invert,
            "to_control": request.to_control,
            "n_selected_features": len(request.feature_ids),
            "clipped_fraction": result.clipped_fraction,
            "display_range": list(result.display_range) if result.display_range else None,
            "extrapolated_fraction": result.extrapolated_fraction,
        },
        input_dataset_ids=[
            i for i in (request.dataset_id, request.boundary_dataset_id) if i is not None
        ],
        job_id=context.job_id,
    )
    log.info(
        "grid_clipped",
        dataset_id=str(dataset_id),
        clipped_fraction=result.clipped_fraction,
    )
    return dataset_id


def caption(request: ClipRequest, inputs: ResolvedClip, result: Any) -> str:
    """The one line Claude reads about this grid (`04` §6.1).

    Leads with what was cut away, because that is the difference between this
    grid and the one it came from, and follows with the extrapolation fraction
    when it was recomputed — clipping to the control is done *for* that number,
    so leaving it out would hide the result of the operation.
    """
    verb = "excluding" if request.invert else "clipped to"
    where = inputs.boundary_name or (request.to_control or "the control")
    parts = [f"{inputs.grid.name}, {verb} {where}"]
    parts.append(f"{result.clipped_fraction:.0%} nodata")
    if result.extrapolated_fraction is not None:
        parts.append(f"{result.extrapolated_fraction:.0%} extrapolated")
    return "; ".join(parts)


__all__ = [
    "MAX_BOUNDARY_FEATURES",
    "ClipRequest",
    "ResolvedClip",
    "caption",
    "read_boundary",
    "read_control_boundary",
    "read_grid",
    "resolve_inputs",
    "write_clipped_dataset",
]
