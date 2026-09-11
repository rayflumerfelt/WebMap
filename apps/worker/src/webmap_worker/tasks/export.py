"""The export job. `11-file-io.md` §7, `10-jobs-async.md` §2.

A job rather than a synchronous download because the work is unbounded: a
half-million lease polygons to shapefile is a read of the whole object, a
format conversion and a zip, and `10` §6 puts anything whose duration the caller
cannot predict on the queue.

**The download URL is minted when the job is collected, not here.** A job result
lives for a day (`keep_result`), and a fifteen-minute link inside it would be
expired for most of that — so the job records the object key and the API signs a
link at collection time, after re-checking the permission. That also means a
link cannot outlive the access that produced it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from webmap_core.db.session import principal_session
from webmap_core.jobs import JobCancelled, JobContext
from webmap_core.logging import get_logger
from webmap_core.services import exports, jobs
from webmap_worker.progress import ProgressReporter
from webmap_worker.runtime import check_cancelled, classify, resources

log = get_logger(__name__)


async def export_task(ctx: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Export one dataset to a downloadable object."""
    context = JobContext.from_payload(payload["context"])
    request = exports.ExportRequest.from_parameters(payload["parameters"])
    principal = context.principal()
    engine, store, object_store, bucket, redis = resources(ctx)

    async def write(fraction: float, message: str) -> None:
        async with principal_session(engine, principal) as conn:
            await jobs.report_progress(conn, context.job_id, fraction, message)

    reporter = ProgressReporter(write, context.job_id, "export")

    async with principal_session(engine, principal) as conn:
        await jobs.mark_running(conn, context.job_id)

    try:
        await reporter.phase("Reading the layer")
        async with principal_session(engine, principal) as conn:
            source = await exports.resolve_export(conn, principal, request)
        await check_cancelled(redis, context.job_id)

        await reporter.phase("Writing the file")
        # Off the event loop: reading a large GeoParquet and writing a
        # shapefile are both blocking, and a worker that stalls its loop stops
        # answering cancellation while it does.
        result = await asyncio.to_thread(
            exports.run_export,
            source,
            request,
            store=store,
            object_store=object_store,
            bucket=bucket,
        )

        await reporter.phase("Recording")
        document = {
            "dataset_id": str(source.dataset_id),
            "object_key": result.key,
            "filename": result.filename,
            "format": result.fmt,
            "size_bytes": result.size_bytes,
            "feature_count": result.feature_count,
            # Travels with the result, per §4.2: the confirmation dialog is one
            # audience and the MCP response is the other.
            "warnings": result.warnings,
        }
        async with principal_session(engine, principal) as conn:
            await exports.record_export(conn, principal, source, result)
            await jobs.mark_succeeded(conn, context.job_id, document)

        log.info(
            "export_succeeded",
            job_id=str(context.job_id),
            dataset_id=str(source.dataset_id),
            format=result.fmt,
            features=result.feature_count,
            bytes=result.size_bytes,
        )
        return document

    except JobCancelled:
        async with principal_session(engine, principal) as conn:
            await jobs.mark_cancelled(conn, context.job_id)
        log.info("export_cancelled", job_id=str(context.job_id))
        return {"cancelled": True}
    except Exception as error:
        kind = classify(error)
        async with principal_session(engine, principal) as conn:
            await jobs.mark_failed(conn, context.job_id, str(error), kind)
        log.warning(
            "export_failed",
            job_id=str(context.job_id),
            error_kind=kind.value,
            error=str(error),
        )
        raise


__all__ = ["export_task"]
