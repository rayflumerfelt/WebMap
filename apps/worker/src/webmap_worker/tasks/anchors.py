"""The label-anchor job. `08` §2.4, `10-jobs-async.md` §2.

Reads a polygon layer, computes one anchor per feature, and registers the
points as a dataset. Short — the work is `polylabel` on the polygons that need
it — but a job rather than an inline call for the same reason the rest are: a
township grid is 40 polygons and a county parcel layer is 200,000, and the
request looks the same from the API's side.
"""

from __future__ import annotations

from typing import Any

from webmap_core.db.session import principal_session
from webmap_core.jobs import JobCancelled, JobContext
from webmap_core.logging import get_logger
from webmap_core.services import anchors, jobs
from webmap_worker.progress import ProgressReporter
from webmap_worker.runtime import check_cancelled, classify, resources

log = get_logger(__name__)


async def anchor_task(ctx: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Precompute label anchors for a polygon layer."""
    context = JobContext.from_payload(payload["context"])
    request = anchors.AnchorRequest.from_parameters(payload["parameters"])
    principal = context.principal()
    engine, store, object_store, bucket, redis = resources(ctx)

    async def write(fraction: float, message: str) -> None:
        async with principal_session(engine, principal) as conn:
            await jobs.report_progress(conn, context.job_id, fraction, message)

    reporter = ProgressReporter(write, context.job_id, "label_anchors")

    async with principal_session(engine, principal) as conn:
        await jobs.mark_running(conn, context.job_id)

    try:
        return await _run(
            context, request, principal, reporter, engine, store, object_store, bucket, redis
        )
    except JobCancelled:
        async with principal_session(engine, principal) as conn:
            await jobs.mark_cancelled(conn, context.job_id)
        log.info("anchors_cancelled", job_id=str(context.job_id))
        return {"cancelled": True}
    except Exception as error:
        kind = classify(error)
        async with principal_session(engine, principal) as conn:
            await jobs.mark_failed(conn, context.job_id, str(error), kind)
        log.warning(
            "anchors_failed",
            job_id=str(context.job_id),
            error_kind=kind.value,
            error=str(error),
        )
        raise


async def _run(
    context: JobContext,
    request: anchors.AnchorRequest,
    principal: Any,
    reporter: ProgressReporter,
    engine: Any,
    store: Any,
    object_store: Any,
    bucket: str,
    redis: Any,
) -> dict[str, Any]:
    await reporter.phase("Loading polygons")
    async with principal_session(engine, principal) as conn:
        source = await anchors.resolve_source(conn, principal, request)
    await check_cancelled(redis, context.job_id)

    await reporter.phase("Placing anchors")
    result = anchors.compute(source, request, object_store, bucket)
    await check_cancelled(redis, context.job_id)

    await reporter.phase("Writing features")
    async with principal_session(engine, principal) as conn:
        dataset_id = await anchors.write_result(
            conn, principal, context, request, source, result, store=store, bucket=bucket
        )
        document = {
            "dataset_id": str(dataset_id),
            "feature_count": result.n_anchored,
            "skipped": result.n_skipped,
            "centroid": result.n_centroid,
            "pole_of_inaccessibility": result.n_pole,
            "caption": anchors.caption(source, result),
        }
        await jobs.mark_succeeded(conn, context.job_id, document)

    log.info(
        "anchors_succeeded",
        job_id=str(context.job_id),
        dataset_id=str(dataset_id),
        anchored=result.n_anchored,
    )
    return document


__all__ = ["anchor_task"]
