"""Job endpoints. `10-jobs-async.md` §5, §9, §10.

Submit an analysis, poll it, cancel it. The API owns the job row and the
queue submission; the worker owns the work.

**The row is written before the task is enqueued.** A worker that picked up a
task with no row would have nowhere to report progress and nothing to fail
into, and the ordering makes that impossible: a queued job with no worker is
recoverable, a running worker with no job row is not.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from arq.connections import ArqRedis
from fastapi import APIRouter, Query, Request
from pydantic import Field

from webmap_api.dependencies import CurrentPrincipal, ScopedConn
from webmap_core.logging import get_logger
from webmap_core.models import Visibility, WebMapModel
from webmap_core.services import aggregation as aggregation_service
from webmap_core.services import anchors as anchor_service
from webmap_core.services import clipping as clip_service
from webmap_core.services import contours as contour_service
from webmap_core.services import exports as export_service
from webmap_core.services import gridding as grid_service
from webmap_core.services import jobs as service
from webmap_core.services import sync as sync_service

log = get_logger(__name__)
router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])

#: Poll interval hints, in seconds, by state. Returned with the job so a
#: client does not have to invent a backoff — and so Claude's polling and the
#: SPA's agree rather than drifting apart (`07-frontend.md` §7).
POLL_AFTER = {"queued": 3, "running": 3, "succeeded": 0, "failed": 0, "cancelled": 0}


class JobSummary(WebMapModel):
    id: UUID
    kind: str
    state: str
    progress: float
    progress_message: str | None = None
    queued_at: Any = None
    started_at: Any = None
    finished_at: Any = None


class JobDetail(JobSummary):
    parameters: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    error_kind: str | None = None
    estimated_remaining_seconds: int | None = None
    poll_after_seconds: int = 3


class Submitted(WebMapModel):
    """What a submission returns.

    `already_running` is not bookkeeping: `10` §10 requires the response to
    say whether this created a job or matched an existing one, so a client
    that retried after a timeout polls rather than resubmitting.
    """

    job_id: UUID
    state: str
    already_running: bool = False
    poll_after_seconds: int = 3


class InterpolateRequest(WebMapModel):
    dataset_id: UUID
    value_column: str
    project_id: UUID | None = None
    method: Literal[
        "ordinary_kriging",
        "universal_kriging",
        "minimum_curvature",
        "cubic_spline",
        "idw",
        "nearest",
    ] = "ordinary_kriging"
    drift_order: int = Field(1, ge=0, le=2)
    kernel: Literal["thin_plate_spline", "cubic", "quintic", "linear"] = "thin_plate_spline"
    smoothing: float = Field(0.0, ge=0.0)
    cell_size: float | None = Field(default=None, gt=0)
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)
    fault_dataset_id: UUID | None = None
    n_neighbors: int = Field(default=48, ge=4, le=256)
    max_radius: float | None = Field(default=None, gt=0)
    tension: float = Field(default=0.0, ge=0.0, le=1.0)
    idw_power: float = Field(default=2.0, gt=0)
    output_name: str | None = None
    visibility: Visibility = Visibility.TEAM
    owner_team_id: UUID | None = None

    def to_request(self) -> grid_service.GridRequest:
        return grid_service.GridRequest(
            dataset_id=self.dataset_id,
            value_column=self.value_column,
            project_id=self.project_id,
            method=self.method,
            cell_size=self.cell_size,
            bbox_4326=self.bbox,
            fault_dataset_id=self.fault_dataset_id,
            n_neighbors=self.n_neighbors,
            max_radius=self.max_radius,
            tension=self.tension,
            idw_power=self.idw_power,
            drift_order=self.drift_order,
            kernel=self.kernel,
            smoothing=self.smoothing,
            output_name=self.output_name,
            visibility=self.visibility,
            owner_team_id=self.owner_team_id,
        )


class ContourRequestModel(WebMapModel):
    dataset_id: UUID
    interval: float | None = Field(default=None, gt=0)
    target_count: int = Field(default=15, ge=2, le=100)
    levels: list[float] | None = None
    smoothing: float = Field(default=0.0, ge=0.0, le=0.5)
    index_every: int = Field(default=5, ge=2, le=20)
    fill: bool = False
    output_name: str | None = None
    project_id: UUID | None = None
    visibility: Visibility = Visibility.TEAM
    owner_team_id: UUID | None = None

    def to_request(self) -> contour_service.ContourRequest:
        return contour_service.ContourRequest(
            dataset_id=self.dataset_id,
            interval=self.interval,
            target_count=self.target_count,
            levels=self.levels,
            smoothing=self.smoothing,
            index_every=self.index_every,
            fill=self.fill,
            output_name=self.output_name,
            project_id=self.project_id,
            visibility=self.visibility,
            owner_team_id=self.owner_team_id,
        )


class ClipRequestModel(WebMapModel):
    """Clip a grid to a polygon layer, to selected features of one, or to its
    own control (`08` §5.2).

    Exactly one boundary: `boundary_dataset_id` or `to_control`. Both, or
    neither, is refused rather than resolved by precedence — which one the map
    was cut to is not recoverable from the result, so it cannot be guessed at
    submission time either.
    """

    dataset_id: UUID
    boundary_dataset_id: UUID | None = None
    feature_ids: list[str] = Field(
        default_factory=list,
        description="Clip to these features of the boundary layer. Empty means all of it.",
    )
    invert: bool = Field(
        default=False,
        description="Exclude the boundary instead of keeping it — a lease to leave out.",
    )
    to_control: Literal["convex_hull", "concave_hull", "radius"] | None = Field(
        default=None,
        description=(
            "Clip to the control instead of to a layer. convex_hull is "
            "conservative; concave_hull follows the outline of the control and "
            "removes bays no well has touched; radius keeps only what `05` §6.5 "
            "calls supported, holes included."
        ),
    )
    control_dataset_id: UUID | None = Field(
        default=None,
        description=(
            "The point layer to_control draws around. Required with to_control, "
            "and asked for rather than read from the grid's lineage."
        ),
    )
    output_name: str | None = None
    project_id: UUID | None = None
    visibility: Visibility = Visibility.TEAM
    owner_team_id: UUID | None = None

    def to_request(self) -> clip_service.ClipRequest:
        return clip_service.ClipRequest(
            dataset_id=self.dataset_id,
            boundary_dataset_id=self.boundary_dataset_id,
            feature_ids=tuple(self.feature_ids),
            invert=self.invert,
            to_control=self.to_control,
            control_dataset_id=self.control_dataset_id,
            output_name=self.output_name,
            project_id=self.project_id,
            visibility=self.visibility,
            owner_team_id=self.owner_team_id,
        )


class AnchorRequestModel(WebMapModel):
    """Precompute label anchors for a polygon layer (`08` §2.4)."""

    dataset_id: UUID
    label_columns: list[str] = Field(
        default_factory=list,
        max_length=8,
        description=(
            "Columns to copy onto the anchor points so the layer can be "
            "labelled on its own. Empty copies none — an anchor layer carrying "
            "forty attributes is a duplicate of the source that then drifts out "
            "of date with it."
        ),
    )
    output_name: str | None = None
    project_id: UUID | None = None
    visibility: Visibility = Visibility.TEAM
    owner_team_id: UUID | None = None

    def to_request(self) -> anchor_service.AnchorRequest:
        return anchor_service.AnchorRequest(
            dataset_id=self.dataset_id,
            label_columns=tuple(self.label_columns),
            output_name=self.output_name,
            project_id=self.project_id,
            visibility=self.visibility,
            owner_team_id=self.owner_team_id,
        )


class StatModel(WebMapModel):
    """One output column of a summarisation (`05` §8)."""

    op: Literal["count", "sum", "mean", "min", "max"]
    name: str = Field(min_length=1, max_length=64)
    field: str | None = None


class AggregateRequestModel(WebMapModel):
    """A spatial aggregation over one or two stored layers.

    `params` is deliberately open: the fifteen operations in the catalog take
    different arguments — a distance, a ratio, a predicate, a cell size — and a
    union of every one of them as optional top-level fields would be a schema
    where most fields are meaningless for any given call. It is validated by
    the operation itself, which is where the knowledge of what each takes
    lives, and a bad argument comes back as a 400 naming the operation.
    """

    op: str = Field(min_length=1, max_length=32)
    dataset_ids: list[UUID] = Field(min_length=1, max_length=2)
    params: dict[str, Any] = Field(default_factory=dict)
    stats: list[StatModel] | None = None
    output_name: str | None = None
    project_id: UUID | None = None
    visibility: Visibility = Visibility.TEAM
    owner_team_id: UUID | None = None

    def to_request(self) -> aggregation_service.AggregateRequest:
        params = dict(self.params)
        if self.stats is not None:
            params["stats"] = [stat.model_dump() for stat in self.stats]
        return aggregation_service.AggregateRequest(
            op=self.op,
            dataset_ids=self.dataset_ids,
            params=params,
            output_name=self.output_name,
            project_id=self.project_id,
            visibility=self.visibility,
            owner_team_id=self.owner_team_id,
        )


def queue(request: Request) -> ArqRedis:
    """The arq pool, created at startup.

    Named rather than indexed inline so a missing pool fails with something
    actionable: an AttributeError on app.state says nothing about why a job
    was accepted and never ran.
    """
    # Annotated rather than asserted: app.state is untyped, and an assert
    # would be stripped under -O while doing nothing at runtime anyway.
    pool: ArqRedis | None = getattr(request.app.state, "queue", None)
    if pool is None:
        raise RuntimeError(
            "The job queue is not connected. It is created in the app's "
            "lifespan from WEBMAP_REDIS_URL; without it a submitted job would "
            "get a row and never be picked up."
        )
    return pool


async def _submit(
    conn: Any,
    principal: Any,
    pool: ArqRedis,
    *,
    kind: str,
    task: str,
    parameters: dict[str, Any],
) -> Submitted:
    """Row first, then the queue.

    A queued job whose task was never submitted shows as queued and can be
    resubmitted or cancelled. A task with no row has nowhere to report
    progress and nothing to fail into, and nobody can see it exists.
    """
    enqueued = await service.enqueue(
        conn, principal, kind=kind, parameters=parameters, redis=pool
    )

    if enqueued.was_created:
        context = service.context_for(principal, enqueued.job_id)
        await pool.enqueue_job(
            task,
            {"context": context.to_payload(), "parameters": parameters},
            _job_id=str(enqueued.job_id),
        )

    return Submitted(
        job_id=enqueued.job_id,
        state="queued",
        already_running=not enqueued.was_created,
    )


@router.post("/interpolate", response_model=Submitted, status_code=202)
async def submit_interpolate(
    body: InterpolateRequest,
    principal: CurrentPrincipal,
    conn: ScopedConn,
    request: Request,
) -> Submitted:
    """Grid a point layer. Always a job.

    Unlike aggregation (§6), gridding is never run inline: even a small grid
    is seconds of CPU on a shared API worker, and the whole point of §3's
    `max_jobs = 1` is to keep that off the request path.
    """
    return await _submit(
        conn,
        principal,
        queue(request),
        kind="interpolate",
        task="interpolate_task",
        parameters=body.to_request().to_parameters(),
    )


@router.post("/contour", response_model=Submitted, status_code=202)
async def submit_contour(
    body: ContourRequestModel,
    principal: CurrentPrincipal,
    conn: ScopedConn,
    request: Request,
) -> Submitted:
    return await _submit(
        conn,
        principal,
        queue(request),
        kind="contour",
        task="contour_task",
        parameters=body.to_request().to_parameters(),
    )


@router.post("/clip", response_model=Submitted, status_code=202)
async def submit_clip(
    body: ClipRequestModel,
    principal: CurrentPrincipal,
    conn: ScopedConn,
    request: Request,
) -> Submitted:
    """Clip a grid, producing a derived grid with lineage to both inputs.

    A job rather than an inline call for the same reason gridding is: the mask
    is cheap and the COG write is not, and a 4-million-cell grid is minutes of
    it. `10` §6's rule is about the tail, and the caller cannot tell from the
    request which size they asked for.
    """
    return await _submit(
        conn,
        principal,
        queue(request),
        kind="clip",
        task="clip_task",
        parameters=body.to_request().to_parameters(),
    )


class ExportRequestModel(WebMapModel):
    dataset_id: UUID
    fmt: str = "gpkg"
    columns: list[str] = Field(default_factory=list)
    target_srid: int | None = None
    #: Proceed even where the format loses information. The warnings come back
    #: either way; this decides whether they stop the export (`11` §4.2).
    accept_loss: bool = False

    def to_request(self) -> export_service.ExportRequest:
        return export_service.ExportRequest(
            dataset_id=self.dataset_id,
            fmt=self.fmt,
            columns=self.columns,
            target_srid=self.target_srid,
            accept_loss=self.accept_loss,
        )


@router.post("/export", response_model=Submitted, status_code=202)
async def submit_export(
    body: ExportRequestModel,
    principal: CurrentPrincipal,
    conn: ScopedConn,
    request: Request,
) -> Submitted:
    """Export a dataset to a downloadable file.

    A job because the duration is the caller's to discover, not to predict: a
    half-million lease polygons to shapefile is a whole-object read, a format
    conversion and a zip.

    The result carries the object key rather than a URL. A job result lives a
    day and a download link lives fifteen minutes, so the link is minted when
    the result is collected — after re-checking the permission, which means a
    link cannot outlive the access that produced it.
    """
    return await _submit(
        conn,
        principal,
        queue(request),
        kind="export",
        task="export_task",
        parameters=body.to_request().to_parameters(),
    )


class SyncRequestModel(WebMapModel):
    dataset_id: UUID
    #: Re-read even when the checksum matches. Rare, and worth having for the
    #: case where somebody has reason to doubt the checksum.
    force: bool = False

    def to_request(self) -> sync_service.SyncRequest:
        return sync_service.SyncRequest(dataset_id=self.dataset_id, force=self.force)


@router.post("/sync", response_model=Submitted, status_code=202)
async def submit_sync(
    body: SyncRequestModel,
    principal: CurrentPrincipal,
    conn: ScopedConn,
    request: Request,
) -> Submitted:
    """Refresh a share- or database-sourced dataset from its upstream.

    A job rather than an inline call because the work is a network fetch plus a
    full re-read of a file that may be gigabytes — and because the caller
    cannot tell which of those they are asking for, which is exactly the tail
    `10` §6 puts on the queue.

    Editor, not viewer: a sync advances the dataset's version and replaces what
    every reader of it sees.
    """
    return await _submit(
        conn,
        principal,
        queue(request),
        kind="sync",
        task="sync_task",
        parameters=body.to_request().to_parameters(),
    )


@router.post("/label-anchors", response_model=Submitted, status_code=202)
async def submit_label_anchors(
    body: AnchorRequestModel,
    principal: CurrentPrincipal,
    conn: ScopedConn,
    request: Request,
) -> Submitted:
    """Precompute one label anchor per polygon, as a point dataset.

    The anchor is computed once against the whole geometry rather than per
    tile, which is what stops a label moving while you pan and appearing twice
    on a polygon that crosses a tile boundary (`08` §2.4).
    """
    return await _submit(
        conn,
        principal,
        queue(request),
        kind="label_anchors",
        task="anchor_task",
        parameters=body.to_request().to_parameters(),
    )


@router.post("/aggregate", response_model=Submitted, status_code=202)
async def submit_aggregate(
    body: AggregateRequestModel,
    principal: CurrentPrincipal,
    conn: ScopedConn,
    request: Request,
) -> Submitted:
    """Run a spatial aggregation over one or two layers.

    A job rather than an inline call. Most of these finish in under a second,
    but `10-jobs-async.md` §6's rule is about the tail: a dissolve over a
    50,000-feature coverage is minutes, and the caller cannot tell which they
    asked for from the request. One path, always asynchronous, is simpler than
    a threshold that guesses.
    """
    return await _submit(
        conn,
        principal,
        queue(request),
        kind="aggregate",
        task="aggregate_task",
        parameters=body.to_request().to_parameters(),
    )


@router.get("", response_model=list[JobSummary])
async def list_jobs(
    principal: CurrentPrincipal,
    conn: ScopedConn,
    active_only: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> list[JobSummary]:
    """This principal's jobs. Never anyone else's — a job's parameters name
    the dataset and the method, which is what somebody is working on."""
    found = await service.list_jobs(conn, principal, active_only=active_only, limit=limit)
    return [JobSummary(**row) for row in found]


@router.get("/{job_id}", response_model=JobDetail)
async def get_job(job_id: UUID, principal: CurrentPrincipal, conn: ScopedConn) -> JobDetail:
    job = await service.get_job(conn, principal, job_id)
    # The service returns `requested_by` because its permission check needs
    # it. It is always the caller's own id here, so it says nothing a client
    # does not know, and `extra="forbid"` refuses it rather than passing it on.
    job.pop("requested_by", None)
    return JobDetail(
        **job,
        estimated_remaining_seconds=_estimate_remaining(job),
        poll_after_seconds=POLL_AFTER.get(str(job["state"]), 3),
    )


@router.post("/{job_id}/cancel")
async def cancel_job(
    job_id: UUID, principal: CurrentPrincipal, conn: ScopedConn, request: Request
) -> dict[str, str]:
    """Ask a job to stop. `10` §9.

    The message is the response, not a status code: a queued job is aborted
    outright while a running one is *asked*, and a caller who sees the job
    still running for ten seconds after a 200 would conclude cancellation did
    not work.
    """
    message = await service.request_cancel(conn, principal, job_id, queue(request))
    return {"message": message}


def _estimate_remaining(job: dict[str, Any]) -> int | None:
    """Seconds left, from elapsed time and progress. `10` §5.1.

    Linear extrapolation, which is wrong in detail — the phases have different
    rates — but right in the only way that matters: it tells someone whether
    to wait or to go and do something else. Withheld below 5% progress, where
    dividing by a tiny fraction produces a confident-looking hour.
    """
    from datetime import UTC, datetime

    if job["state"] != "running" or job.get("started_at") is None:
        return None

    progress = float(job.get("progress") or 0.0)
    if progress < 0.05:
        return None

    started = job["started_at"]
    elapsed = (datetime.now(UTC) - started).total_seconds()
    return max(0, int(elapsed * (1.0 - progress) / progress))
