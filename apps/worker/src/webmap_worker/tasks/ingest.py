"""The ingest job. `11-file-io.md` §6, `10-jobs-async.md` §6.

Small files are ingested inline by the upload route, because forcing a poll
cycle on a 200 kB CSV makes the common case worse to use. This is the other
half: a file large enough that reading it would block an API worker for the
thirty seconds §6 says is unacceptable.

**The upload is spooled to object storage first, and the job reads it from
there.** Not from a shared temp directory: the API and the worker are separate
containers, and a path that exists in one of them is a file the other cannot
open. That the object then *is* the source — `s3://bucket/uploads/…` — also
makes an uploaded dataset re-readable by the same connector a share-sourced one
uses, which is what `11` §2.1 means by the upload being a connector like any
other.

**A failed ingest leaves no dataset.** The registration is the last thing that
happens, after the read has succeeded and the GeoParquet object is written, so
there is no half-registered layer to clean up — the orphan is an object, which a
sweep collects, and Phase 4's "cancelling a running job leaves no partial dataset
registered" is the same property from the other side.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from webmap_core.db.session import principal_session
from webmap_core.jobs import JobCancelled, JobContext
from webmap_core.logging import get_logger
from webmap_core.models import DatasetKind, Visibility
from webmap_core.services import ingest as ingest_service
from webmap_core.services import jobs
from webmap_worker.progress import ProgressReporter
from webmap_worker.runtime import check_cancelled, classify, resources

log = get_logger(__name__)


def options_from(parameters: dict[str, Any]) -> ingest_service.IngestOptions:
    """Rebuild the caller's stated options from the job payload.

    Stated, never inferred — the CRS and the column mapping especially. A job
    payload that lost them would put the pipeline back to guessing, which is
    what `11` §3 exists to prevent.
    """
    return ingest_service.IngestOptions(
        name=str(parameters["name"]),
        project_id=parameters.get("project_id"),
        visibility=Visibility(parameters.get("visibility", Visibility.TEAM.value)),
        owner_team_id=parameters.get("owner_team_id"),
        kind=DatasetKind(parameters["kind"]) if parameters.get("kind") else None,
        description=parameters.get("description"),
        srid_override=parameters.get("srid_override"),
        encoding=parameters.get("encoding"),
        x_column=parameters.get("x_column"),
        y_column=parameters.get("y_column"),
        z_column=parameters.get("z_column"),
    )


async def ingest_task(ctx: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Read a spooled upload and register it as a dataset."""
    context = JobContext.from_payload(payload["context"])
    parameters = payload["parameters"]
    principal = context.principal()
    engine, store, _object_store, bucket, redis = resources(ctx)

    async def write(fraction: float, message: str) -> None:
        async with principal_session(engine, principal) as conn:
            await jobs.report_progress(conn, context.job_id, fraction, message)

    reporter = ProgressReporter(write, context.job_id, "ingest")

    async with principal_session(engine, principal) as conn:
        await jobs.mark_running(conn, context.job_id)

    object_key = str(parameters["object_key"])

    try:
        await reporter.phase("Fetching the upload")
        with tempfile.TemporaryDirectory(prefix="webmap-ingest-") as workdir:
            local = await asyncio.to_thread(_fetch, store, bucket, object_key, Path(workdir))
            await check_cancelled(redis, context.job_id)

            await reporter.phase("Reading and registering")
            async with principal_session(engine, principal) as conn:
                result = await ingest_service.ingest_upload(
                    conn,
                    principal,
                    local,
                    options_from(parameters),
                    storage=store,
                    bucket=bucket,
                )

        document = {
            "dataset_id": str(result.dataset_id),
            "name": result.name,
            "kind": result.kind.value,
            "geometry_kind": result.geometry_kind.value,
            "srid": result.srid,
            "feature_count": result.feature_count,
            "bbox_4326": result.bbox_4326,
            # Warnings are part of the result, not a log line: a layer that
            # lost forty null geometries on the way in has to say so where the
            # person who uploaded it will see it.
            "warnings": result.warnings,
        }
        async with principal_session(engine, principal) as conn:
            await jobs.mark_succeeded(conn, context.job_id, document)

        log.info(
            "ingest_succeeded",
            job_id=str(context.job_id),
            dataset_id=str(result.dataset_id),
            features=result.feature_count,
            warnings=len(result.warnings),
        )
        return document

    except JobCancelled:
        async with principal_session(engine, principal) as conn:
            await jobs.mark_cancelled(conn, context.job_id)
        log.info("ingest_cancelled", job_id=str(context.job_id))
        return {"cancelled": True}
    except Exception as error:
        kind = classify(error)
        async with principal_session(engine, principal) as conn:
            await jobs.mark_failed(conn, context.job_id, str(error), kind)
        log.warning(
            "ingest_failed",
            job_id=str(context.job_id),
            object_key=object_key,
            error_kind=kind.value,
            error=str(error),
        )
        raise


def _fetch(store: Any, bucket: str, key: str, workdir: Path) -> Path:
    """Bring the spooled upload down to local disk.

    Blocking, so the caller runs it off the event loop — a multi-gigabyte
    download that stalled the loop would stop the worker answering cancellation
    for the duration of it.
    """
    from webmap_io.storage import get_file

    return get_file(store, bucket, key, workdir / key.rsplit("/", 1)[-1])


__all__ = ["ingest_task", "options_from"]
