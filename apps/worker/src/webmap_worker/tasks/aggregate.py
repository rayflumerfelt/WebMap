"""The spatial aggregation job. `05-geoprocessing.md` §8, `10-jobs-async.md` §2.

Reads one or two stored layers, runs an operation from the catalog, and
registers the result as a vector layer.

The same three rules as the gridding and contouring jobs — runs as the
requester, checks cancellation before anything becomes visible, classifies its
failures — and one specific to this job: **every input is permission-checked**,
not just the first. A two-layer overlay that checked only the left operand
would let a clip read a layer the caller cannot see through the geometry of one
they can.
"""

from __future__ import annotations

from typing import Any

from webmap_core.db.session import principal_session
from webmap_core.jobs import JobCancelled, JobContext
from webmap_core.logging import get_logger
from webmap_core.services import aggregation, jobs
from webmap_worker.progress import ProgressReporter
from webmap_worker.runtime import check_cancelled, classify, resources

log = get_logger(__name__)


async def aggregate_task(ctx: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Aggregate stored layers and register the result."""
    context = JobContext.from_payload(payload["context"])
    request = aggregation.AggregateRequest.from_parameters(payload["parameters"])
    principal = context.principal()
    # Two clients for one bucket: `object_store` configures DuckDB's httpfs
    # for reads, `store` is boto3 for the write. See `aggregation.run`.
    engine, store, object_store, bucket, redis = resources(ctx)

    async def write(fraction: float, message: str) -> None:
        async with principal_session(engine, principal) as conn:
            await jobs.report_progress(conn, context.job_id, fraction, message)

    reporter = ProgressReporter(write, context.job_id, "aggregate")

    async with principal_session(engine, principal) as conn:
        await jobs.mark_running(conn, context.job_id)

    try:
        return await _run(
            context, request, principal, reporter, engine, store, object_store, bucket, redis
        )
    except JobCancelled:
        async with principal_session(engine, principal) as conn:
            await jobs.mark_cancelled(conn, context.job_id)
        log.info("aggregate_cancelled", job_id=str(context.job_id))
        return {"cancelled": True}
    except Exception as error:
        kind = classify(error)
        async with principal_session(engine, principal) as conn:
            await jobs.mark_failed(conn, context.job_id, str(error), kind)
        log.warning(
            "aggregate_failed",
            job_id=str(context.job_id),
            op=request.op,
            error_kind=kind.value,
            error=str(error),
        )
        raise


async def _run(
    context: JobContext,
    request: aggregation.AggregateRequest,
    principal: Any,
    reporter: ProgressReporter,
    engine: Any,
    store: Any,
    object_store: Any,
    bucket: str,
    redis: Any,
) -> dict[str, Any]:
    await reporter.phase("Loading layers")
    async with principal_session(engine, principal) as conn:
        sources = await aggregation.resolve_inputs(conn, principal, request)
    # Refused before anything is read, so the message names the layer rather
    # than arriving after a minute of loading.
    aggregation.check_inputs(request, sources)
    await check_cancelled(redis, context.job_id)

    await reporter.phase("Aggregating")
    result = aggregation.run(request, sources, object_store, bucket)
    await check_cancelled(redis, context.job_id)

    await reporter.phase("Writing features")
    async with principal_session(engine, principal) as conn:
        dataset_id = await aggregation.write_result(
            conn, principal, context, request, sources, result, store=store, bucket=bucket
        )
        document = {
            "dataset_id": str(dataset_id),
            "op": request.op,
            "feature_count": len(result),
            "geometry_kind": aggregation.geometry_kind_of(result).value,
            "caption": aggregation.caption(request, sources, result),
            "inputs": [source.name for source in sources],
        }
        await jobs.mark_succeeded(conn, context.job_id, document)

    log.info(
        "aggregate_succeeded",
        job_id=str(context.job_id),
        op=request.op,
        dataset_id=str(dataset_id),
        features=len(result),
    )
    return document


__all__ = ["aggregate_task"]
