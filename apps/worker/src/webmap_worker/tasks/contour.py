"""The contouring job. `05-geoprocessing.md` §7, `10-jobs-async.md` §2.

Reads a stored grid, traces contours, and registers them as a vector layer.

The same three rules as the gridding job — runs as the requester, checks
cancellation before anything becomes visible, classifies its failures — and
one that is specific to contours: **the interval is chosen for a reader, not
for a solver.** `auto_levels` picks a round number a geologist would choose,
and the job carries that choice into the layer's caption so the map says what
its interval is.
"""

from __future__ import annotations

from typing import Any

from webmap_core.db.session import principal_session
from webmap_core.jobs import JobCancelled, JobContext
from webmap_core.logging import get_logger
from webmap_core.services import contours, jobs
from webmap_worker.progress import ProgressReporter
from webmap_worker.runtime import check_cancelled, classify, resources

log = get_logger(__name__)


async def contour_task(ctx: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Contour a stored grid and register the lines as a dataset."""
    context = JobContext.from_payload(payload["context"])
    request = contours.ContourRequest.from_parameters(payload["parameters"])
    principal = context.principal()
    # No DuckDB store: contouring reads a COG through rasterio and writes
    # GeoParquet through pyarrow, so the query engine is not involved.
    engine, store, _object_store, bucket, redis = resources(ctx)

    async def write(fraction: float, message: str) -> None:
        async with principal_session(engine, principal) as conn:
            await jobs.report_progress(conn, context.job_id, fraction, message)

    reporter = ProgressReporter(
        write, context.job_id, "contour_filled" if request.fill else "contour"
    )

    async with principal_session(engine, principal) as conn:
        await jobs.mark_running(conn, context.job_id)

    try:
        return await _run(context, request, principal, reporter, engine, store, bucket, redis)
    except JobCancelled:
        async with principal_session(engine, principal) as conn:
            await jobs.mark_cancelled(conn, context.job_id)
        log.info("contour_cancelled", job_id=str(context.job_id))
        return {"cancelled": True}
    except Exception as error:
        kind = classify(error)
        async with principal_session(engine, principal) as conn:
            await jobs.mark_failed(conn, context.job_id, str(error), kind)
        log.warning(
            "contour_failed",
            job_id=str(context.job_id),
            error_kind=kind.value,
            error=str(error),
        )
        raise


async def _run(
    context: JobContext,
    request: contours.ContourRequest,
    principal: Any,
    reporter: ProgressReporter,
    engine: Any,
    store: Any,
    bucket: str,
    redis: Any,
) -> dict[str, Any]:
    await reporter.phase("Loading grid")
    async with principal_session(engine, principal) as conn:
        source = await contours.resolve_grid(conn, principal, request)

    surface, grid = contours.read_grid(source, store, bucket)
    await check_cancelled(redis, context.job_id)

    await reporter.phase("Choosing levels")
    levels = contours.choose_levels(surface, request)

    await reporter.phase("Tracing contours")
    lines = contours.trace(surface, grid, levels, request)
    await check_cancelled(redis, context.job_id)

    bands: list[Any] = []
    if request.fill:
        await reporter.phase("Filling bands")
        bands = contours.fill_bands(surface, grid, levels, request)
        await check_cancelled(redis, context.job_id)

    await reporter.phase("Writing features")
    async with principal_session(engine, principal) as conn:
        dataset_id = await contours.write_contour_dataset(
            conn, principal, context, request, source, lines, levels, store=store, bucket=bucket
        )
        document = {
            "dataset_id": str(dataset_id),
            "interval": contours.interval_of(levels),
            "levels": [float(level) for level in levels],
            "index_levels": contours.index_values(lines),
            "feature_count": len(lines),
            "caption": contours.caption(source, lines, levels),
        }
        if bands:
            # Written inside the same transaction as the lines. The two share a
            # level list and are only meaningful together, so a run that
            # registered one and not the other would leave a filled map with no
            # contours on it, or contours with a fill that does not match.
            band_dataset_id = await contours.write_band_dataset(
                conn,
                principal,
                context,
                request,
                source,
                bands,
                levels,
                store=store,
                bucket=bucket,
            )
            document["band_dataset_id"] = str(band_dataset_id)
            document["band_count"] = len(bands)
        await jobs.mark_succeeded(conn, context.job_id, document)

    log.info(
        "contour_succeeded",
        job_id=str(context.job_id),
        dataset_id=str(dataset_id),
        lines=len(lines),
        bands=len(bands),
    )
    return document


__all__ = ["contour_task"]
