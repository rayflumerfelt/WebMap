"""The gridding job. `10-jobs-async.md` §2, `05-geoprocessing.md` §6.

This function is where the three rules that make an async job safe actually
have to be obeyed, rather than merely stated:

- **It runs as the requester.** `JobContext.principal()` is the only identity
  in scope, and every read goes through a service function that takes it.
  There is no service account and no bypass; a job that resolved datasets
  without this would be a security bug (`03-auth-security.md` §5.1).
- **Cancellation is checked at phase boundaries**, and the output is written
  only after the last check. Partial outputs are discarded — never leave a
  half-written COG registered as a dataset (`10` §9).
- **Failures are classified.** Retrying an INPUT error just fails four times
  slower and buries the message that would have told the user what to fix
  (`10` §8), so a bad value column and a dropped connection do not look alike.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from webmap_core.crs import CrsContext
from webmap_core.db.session import principal_session
from webmap_core.jobs import JobCancelled, JobContext
from webmap_core.logging import get_logger
from webmap_core.services import gridding, jobs
from webmap_worker.progress import ProgressReporter
from webmap_worker.runtime import check_cancelled, classify, resources

log = get_logger(__name__)


async def interpolate_task(ctx: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Grid a point layer and register the surface as a dataset.

    Returns the job's `result` document: the new dataset, the diagnostics
    §6.5 requires, and the warnings a reader of the map would otherwise never
    see. Raising is reserved for failures — a cancelled job returns normally
    with its state already set, because a cancellation is not an error.
    """
    context = JobContext.from_payload(payload["context"])
    request = gridding.GridRequest.from_parameters(payload["parameters"])
    principal = context.principal()
    engine, store, object_store, bucket, redis = resources(ctx)

    async def write(fraction: float, message: str) -> None:
        async with principal_session(engine, principal) as conn:
            await jobs.report_progress(conn, context.job_id, fraction, message)

    reporter = ProgressReporter(write, context.job_id, "interpolate")

    async with principal_session(engine, principal) as conn:
        await jobs.mark_running(conn, context.job_id)

    try:
        return await _run(
            context, request, principal, reporter, engine, store, object_store, bucket, redis
        )
    except JobCancelled:
        # Not an error. The output was never uploaded and no dataset row
        # exists, so there is nothing to clean up — which is the point of
        # writing both at the very end.
        async with principal_session(engine, principal) as conn:
            await jobs.mark_cancelled(conn, context.job_id)
        log.info("interpolate_cancelled", job_id=str(context.job_id))
        return {"cancelled": True}
    except Exception as error:
        kind = classify(error)
        async with principal_session(engine, principal) as conn:
            await jobs.mark_failed(conn, context.job_id, str(error), kind)
        log.warning(
            "interpolate_failed",
            job_id=str(context.job_id),
            error_kind=kind.value,
            error=str(error),
        )
        # Re-raised so arq's retry policy sees it. The row already records
        # what happened, so a retry that never comes still leaves a job a
        # person can read.
        raise


async def _run(
    context: JobContext,
    request: gridding.GridRequest,
    principal: Any,
    reporter: ProgressReporter,
    engine: Any,
    store: Any,
    object_store: Any,
    bucket: str,
    redis: Any,
) -> dict[str, Any]:
    from webmap_geo.interpolate.dispatch import interpolate

    await reporter.phase("Loading control points")
    async with principal_session(engine, principal) as conn:
        inputs = await gridding.resolve_inputs(conn, principal, request)

    crs = CrsContext(storage_srid=inputs.storage_srid, analysis_srid=inputs.analysis_srid)
    control = await gridding.load_control(inputs, request, crs, object_store, bucket)
    await check_cancelled(redis, context.job_id)

    grid = gridding.build_grid(control.bounds(), crs, request, unit=crs.frame.units)

    constraints = []
    if inputs.fault_parquet_key is not None:
        await reporter.phase("Validating fault network")
        constraints = await _load_constraints(inputs, crs, object_store, bucket)
        await check_cancelled(redis, context.job_id)

    await reporter.phase("Solving")
    result = interpolate(
        control.coords,
        control.values,
        grid,
        method=request.method,
        constraints=constraints or None,
        n_neighbors=request.n_neighbors,
        max_radius=request.max_radius,
        tension=request.tension,
        idw_power=request.idw_power,
        # `CLAUDE.md` §3.3: an explicit generator, seeded from the job id so
        # the same job re-run reproduces its own cross-validation split rather
        # than a different one that reports a different RMSE.
        rng=_generator_for(context.job_id),
    )

    # **The last checkpoint before anything becomes visible.** After this the
    # COG is uploaded and the dataset registered; a cancellation arriving now
    # is honoured on the next poll of a different job, not by unregistering a
    # dataset somebody may already have opened.
    await check_cancelled(redis, context.job_id)

    await reporter.phase("Writing grid")
    async with principal_session(engine, principal) as conn:
        dataset_id = await gridding.write_grid_dataset(
            conn,
            principal,
            context,
            request,
            inputs,
            result,
            store=store,
            bucket=bucket,
        )

        document = {
            "dataset_id": str(dataset_id),
            "method": result.method.value,
            "grid": {
                "nx": grid.nx,
                "ny": grid.ny,
                "cell_size": grid.cell_size,
                "srid": inputs.analysis_srid,
            },
            "diagnostics": result.diagnostics,
            # The reader's warnings and the solver's, in one list. A control
            # set that lost 200 wells to a null column and a surface that is
            # 40% extrapolated are the same kind of fact to whoever opens the
            # map, and separating them by origin would bury one of them.
            "warnings": [*control.warnings(), *result.warnings],
            "caption": result.describe(),
        }
        await jobs.mark_succeeded(conn, context.job_id, document)

    log.info(
        "interpolate_succeeded",
        job_id=str(context.job_id),
        dataset_id=str(dataset_id),
        cells=grid.n_cells,
        control_points=len(control),
    )
    return document


async def _load_constraints(
    inputs: gridding.ResolvedInputs, crs: CrsContext, object_store: Any, bucket: str
) -> list[Any]:
    """Fault traces, in the analysis frame.

    Read as geometry rather than as control: a fault has no Z of its own —
    "a fault's Z is whatever the surface does on each side" — so only the
    trace matters here.
    """
    import numpy as np
    import shapely
    from shapely.geometry import LineString

    from webmap_geo.dataplane import connect
    from webmap_geo.faults.network import Constraint, ConstraintKind

    with connect(object_store) as conn:
        rows = conn.execute(
            "SELECT geometry, props FROM read_parquet($key)",
            {"key": f"s3://{bucket}/{inputs.fault_parquet_key}"},
        ).fetchall()

    constraints: list[Any] = []
    for wkb, _props in rows:
        geometry = shapely.from_wkb(bytes(wkb))
        if geometry.geom_type != "LineString":
            # A polygon in a fault layer is a compartment outline, not a
            # trace. Skipped rather than refused: fault layers routinely carry
            # both, and refusing the whole network over one polygon would make
            # a usable dataset unusable.
            continue
        coords = np.asarray(geometry.coords, dtype=float)
        if inputs.fault_storage_srid != inputs.analysis_srid:
            x, y = crs.to_analysis(coords[:, 0], coords[:, 1])
            coords = np.column_stack([x, y])
        constraints.append(Constraint(geometry=LineString(coords), kind=ConstraintKind.FAULT))
    return constraints


def _generator_for(job_id: UUID) -> Any:
    """A generator seeded from the job id.

    `CLAUDE.md` §3.3 forbids bare `np.random`, and a fresh unseeded generator
    would make a re-run of the same job report a different cross-validation
    RMSE for an identical surface — which reads as instability in the method
    rather than in the split.
    """
    import numpy as np

    return np.random.default_rng(job_id.int % (2**63))


__all__ = ["interpolate_task"]
