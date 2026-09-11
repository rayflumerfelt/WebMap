"""The dataset sync job. `11-file-io.md` §2.4, `10-jobs-async.md` §2.

Refreshes a share- or database-sourced dataset from its upstream. A job rather
than an API call because the work is a network fetch plus a full re-read of a
file that may be gigabytes, and `10` §2 puts anything unbounded on the queue.

**The failure path writes a state, not just a job record.** A sync that fails
leaves `sync_state = 'failed'` on the dataset, because the question a geologist
asks is "is this layer current?" and they ask it of the layer, not of a job
they never saw. `'stale'` is kept for the narrower case where the upstream has
gone — the data is still readable and is simply no longer being refreshed,
which needs a different sentence in the UI than "the last sync failed".
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID

from webmap_core.db.session import principal_session
from webmap_core.jobs import JobCancelled, JobContext
from webmap_core.logging import get_logger
from webmap_core.models import SyncState
from webmap_core.services import jobs, sync
from webmap_io.connectors.base import ConnectorError, UnknownSource
from webmap_worker.progress import ProgressReporter
from webmap_worker.runtime import check_cancelled, classify, resources

log = get_logger(__name__)


def connector_for(kind: str, ctx: dict[str, Any]) -> Any:
    """The connector a dataset's `connector` column names.

    Built here rather than injected because the worker is the only caller and
    the share map is deployment configuration: a connector assembled anywhere
    else would need the same configuration passed to it, which is how two
    copies of a share map come to disagree about which team owns what.
    """
    from webmap_io.connectors.fileshare import FileShareConnector
    from webmap_io.connectors.upload import UploadConnector

    if kind == "fileshare":
        return FileShareConnector(ctx.get("shares") or {})
    if kind == "upload":
        return UploadConnector(ctx["store"])
    raise ConnectorError(
        f"No connector is configured for '{kind}'. Datasets sourced this way "
        f"cannot be refreshed from here; re-import the file if it has changed."
    )


async def sync_task(ctx: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Refresh one dataset from its source."""
    context = JobContext.from_payload(payload["context"])
    request = sync.SyncRequest.from_parameters(payload["parameters"])
    principal = context.principal()
    engine, store, _object_store, bucket, redis = resources(ctx)

    async def write(fraction: float, message: str) -> None:
        async with principal_session(engine, principal) as conn:
            await jobs.report_progress(conn, context.job_id, fraction, message)

    reporter = ProgressReporter(write, context.job_id, "sync")

    async with principal_session(engine, principal) as conn:
        await jobs.mark_running(conn, context.job_id)

    try:
        return await _run(context, request, principal, reporter, engine, store, bucket, redis)
    except JobCancelled:
        async with principal_session(engine, principal) as conn:
            await jobs.mark_cancelled(conn, context.job_id)
            # Back to ready, not failed: a cancelled sync changed nothing, and
            # the stored copy is exactly as current as it was before.
            await sync.mark_state(conn, request.dataset_id, SyncState.READY)
        log.info("sync_cancelled", job_id=str(context.job_id))
        return {"cancelled": True}
    except Exception as error:
        kind = classify(error)
        state = SyncState.STALE if isinstance(error, UnknownSource) else SyncState.FAILED
        async with principal_session(engine, principal) as conn:
            await jobs.mark_failed(conn, context.job_id, str(error), kind)
            await sync.mark_state(conn, request.dataset_id, state)
        log.warning(
            "sync_failed",
            job_id=str(context.job_id),
            dataset_id=str(request.dataset_id),
            sync_state=state.value,
            error_kind=kind.value,
            error=str(error),
        )
        raise


async def _run(
    context: JobContext,
    request: sync.SyncRequest,
    principal: Any,
    reporter: ProgressReporter,
    engine: Any,
    store: Any,
    bucket: str,
    redis: Any,
) -> dict[str, Any]:
    await reporter.phase("Checking the source")
    async with principal_session(engine, principal) as conn:
        source = await sync.resolve_source(conn, principal, request)

    connector = connector_for(source.connector, {"store": store, "shares": {}})

    if await sync.is_current(connector, source, force=request.force):
        # Early, and it records the time: "checked, unchanged" is a successful
        # sync and the layer's tooltip should say when it was last confirmed,
        # not when it last changed.
        async with principal_session(engine, principal) as conn:
            await sync.mark_state(conn, source.dataset_id, SyncState.READY, synced=True)
        outcome = sync.SyncOutcome(
            dataset_id=source.dataset_id,
            changed=False,
            version=source.version,
            feature_count=None,
            reason=f"'{source.name}' is unchanged upstream; nothing was re-read.",
        )
        return await _finish(context, principal, engine, outcome)

    async with principal_session(engine, principal) as conn:
        await sync.mark_state(conn, source.dataset_id, SyncState.SYNCING)

    await reporter.phase("Fetching")
    descriptor = await connector.describe(source.source_uri)
    with tempfile.TemporaryDirectory() as tmp:
        fetched = await connector.fetch(source.source_uri, Path(tmp))
        await check_cancelled(redis, context.job_id)

        await reporter.phase("Reading")
        result = sync.read_source(fetched, source)
        await check_cancelled(redis, context.job_id)

        await reporter.phase("Writing features")
        async with principal_session(engine, principal) as conn:
            version, count = await sync.write_version(
                conn, source, result, descriptor.checksum, store=store, bucket=bucket
            )
            await sync.record_sync_lineage(
                conn, principal, context, source, version, descriptor.checksum
            )

    outcome = sync.SyncOutcome(
        dataset_id=source.dataset_id,
        changed=True,
        version=version,
        feature_count=count,
        reason=(
            f"'{source.name}' was refreshed from {source.source_uri}: "
            f"{count:,} features, now at version {version}."
        ),
    )
    return await _finish(context, principal, engine, outcome)


async def _finish(
    context: JobContext, principal: Any, engine: Any, outcome: sync.SyncOutcome
) -> dict[str, Any]:
    document = {
        "dataset_id": str(outcome.dataset_id),
        "changed": outcome.changed,
        "version": outcome.version,
        "feature_count": outcome.feature_count,
        "reason": outcome.reason,
    }
    async with principal_session(engine, principal) as conn:
        await jobs.mark_succeeded(conn, context.job_id, document)

    log.info(
        "sync_succeeded",
        job_id=str(context.job_id),
        dataset_id=str(outcome.dataset_id),
        changed=outcome.changed,
        version=outcome.version,
    )
    return document


def datasets_due(rows: list[dict[str, Any]], *, now: Any, interval_hours: float) -> list[UUID]:
    """Which datasets a scheduled sweep should refresh.

    Pure, so the scheduling rule is testable without a clock or a queue. Two
    rules, and the second is the one that matters:

    - a dataset never synced is due;
    - a dataset whose last sync **failed** is due again, but no sooner than one
      whose last sync succeeded. A source that is failing fails fast, and
      retrying it on a tight loop turns one broken share into a worker that
      does nothing else.
    """
    due = []
    for row in rows:
        synced_at = row.get("synced_at")
        if synced_at is None:
            due.append(UUID(str(row["id"])))
            continue
        age_hours = (now - synced_at).total_seconds() / 3600.0
        if age_hours >= interval_hours:
            due.append(UUID(str(row["id"])))
    return due


__all__ = ["connector_for", "datasets_due", "sync_task"]
