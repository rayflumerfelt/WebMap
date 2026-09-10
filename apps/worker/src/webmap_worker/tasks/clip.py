"""The grid-clipping job. `08` §5.2, `10-jobs-async.md` §2.

Reads a stored grid and a boundary, blanks the cells outside it, and registers
the result as a **derived grid with lineage to both inputs**. Not an edit of
the source: `CLAUDE.md` §3.4 forbids writing in place, and both surfaces are
legitimately wanted — the unclipped one is what you re-clip when the acreage
changes.

Two clients for one bucket, as everywhere in this package: `object_store`
configures DuckDB's httpfs for reading the boundary features, and `store` is
the boto3 client that reads and writes the COG.
"""

from __future__ import annotations

from typing import Any

from webmap_core.crs import CrsContext
from webmap_core.db.session import principal_session
from webmap_core.jobs import JobCancelled, JobContext
from webmap_core.logging import get_logger
from webmap_core.services import clipping, jobs
from webmap_worker.progress import ProgressReporter
from webmap_worker.runtime import check_cancelled, classify, resources

log = get_logger(__name__)


async def clip_task(ctx: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Clip a stored grid to a polygon layer, or to its own control."""
    context = JobContext.from_payload(payload["context"])
    request = clipping.ClipRequest.from_parameters(payload["parameters"])
    principal = context.principal()
    engine, store, object_store, bucket, redis = resources(ctx)

    async def write(fraction: float, message: str) -> None:
        async with principal_session(engine, principal) as conn:
            await jobs.report_progress(conn, context.job_id, fraction, message)

    reporter = ProgressReporter(write, context.job_id, "clip")

    async with principal_session(engine, principal) as conn:
        await jobs.mark_running(conn, context.job_id)

    try:
        return await _run(
            context, request, principal, reporter, engine, store, object_store, bucket, redis
        )
    except JobCancelled:
        async with principal_session(engine, principal) as conn:
            await jobs.mark_cancelled(conn, context.job_id)
        log.info("clip_cancelled", job_id=str(context.job_id))
        return {"cancelled": True}
    except Exception as error:
        kind = classify(error)
        async with principal_session(engine, principal) as conn:
            await jobs.mark_failed(conn, context.job_id, str(error), kind)
        log.warning(
            "clip_failed",
            job_id=str(context.job_id),
            error_kind=kind.value,
            error=str(error),
        )
        raise


async def _run(
    context: JobContext,
    request: clipping.ClipRequest,
    principal: Any,
    reporter: ProgressReporter,
    engine: Any,
    store: Any,
    object_store: Any,
    bucket: str,
    redis: Any,
) -> dict[str, Any]:
    from webmap_geo.clip import clip

    await reporter.phase("Loading grid")
    async with principal_session(engine, principal) as conn:
        inputs = await clipping.resolve_inputs(conn, principal, request)
    surface, grid = clipping.read_grid(inputs.grid, store, bucket)
    await check_cancelled(redis, context.job_id)

    await reporter.phase("Loading boundary")
    # The boundary layer may be stored in a different CRS from the grid, and
    # `webmap_geo.clip` never transforms (`adr/0003`). One `CrsContext` for
    # both boundary paths, built from the grid's frame because that is the
    # frame the clip has to happen in.
    source_srid = (
        inputs.boundary_storage_srid
        if inputs.boundary_parquet_key is not None
        else inputs.control_storage_srid
    )
    crs = CrsContext(
        storage_srid=int(source_srid or inputs.grid.storage_srid),
        analysis_srid=inputs.grid.storage_srid,
    )
    if inputs.boundary_parquet_key is not None:
        boundary = clipping.read_boundary(inputs, request, object_store, bucket, crs)
        control_points = None
    else:
        boundary = clipping.read_control_boundary(inputs, request, object_store, bucket, crs)
        control_points = _boundary_points(boundary)
    await check_cancelled(redis, context.job_id)

    await reporter.phase("Clipping")
    result = clip(
        surface,
        grid,
        boundary,
        invert=request.invert,
        control_points=control_points,
    )
    await check_cancelled(redis, context.job_id)

    await reporter.phase("Writing grid")
    async with principal_session(engine, principal) as conn:
        dataset_id = await clipping.write_clipped_dataset(
            conn,
            principal,
            context,
            request,
            inputs,
            result,
            grid,
            store=store,
            bucket=bucket,
        )
        document = {
            "dataset_id": str(dataset_id),
            "clipped_fraction": result.clipped_fraction,
            "display_range": (list(result.display_range) if result.display_range else None),
            "extrapolated_fraction": result.extrapolated_fraction,
            "caption": clipping.caption(request, inputs, result),
        }
        await jobs.mark_succeeded(conn, context.job_id, document)

    log.info(
        "clip_succeeded",
        job_id=str(context.job_id),
        dataset_id=str(dataset_id),
        clipped_fraction=result.clipped_fraction,
    )
    return document


def _boundary_points(boundary: Any) -> Any:
    """The control-boundary vertices, for recomputing the extrapolation figure.

    Only used on the "clip to the control" path, and only because that is the
    path where the number is the whole point of the operation. Using the
    boundary's vertices rather than re-reading the point layer is deliberate:
    the vertices *are* the outer control points for a hull, and re-reading
    would be a second permission-checked round trip for the same information.

    A radius boundary's vertices are on the buffer arcs rather than at the
    wells, which pushes the measured radius out by one buffer distance. The
    figure is therefore slightly optimistic on that path — stated here rather
    than silently wrong, and it is the path where the boundary already *is*
    the supported area, so the number it reports is near zero either way.
    """
    import shapely

    return shapely.get_coordinates(boundary)


__all__ = ["clip_task"]
