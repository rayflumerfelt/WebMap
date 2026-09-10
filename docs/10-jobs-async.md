# 10 — Jobs and Async Processing

Queue: `arq` over Redis. Chosen for native asyncio (matching FastAPI) and operational
simplicity over Celery.

---

## 1. What must be async

| Operation | Typical | Worst case | Async? |
|---|---|---|---|
| Interpolation, no faults | 10–60 s | 5 min | **Yes** |
| Interpolation, with faults | 60 s–5 min | 20 min | **Yes** |
| Variogram fitting | 2–5 s | 15 s | No — synchronous |
| Contouring | 2–10 s | 60 s | **Yes** |
| Rendering | 1–2 s | 5 s | No — synchronous |
| Dataset ingest, small | 1–5 s | 30 s | **Yes** (uniform path) |
| Dataset sync from share | 10 s–10 min | 1 hr | **Yes** |
| Spatial aggregation | 0.1–30 s | 10 min | Conditional (§6) |
| Export | 1–60 s | 10 min | **Yes** |

**Rendering stays synchronous.** At 1–2 s with a warm browser it fits comfortably inside an
MCP tool timeout, and making Claude poll for an image it will display immediately adds a turn
for nothing.

---

## 2. Job contract

```python
# python/webmap_core/src/webmap_core/jobs.py

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class JobRecord:
    id: UUID
    kind: str
    state: JobState
    parameters: dict[str, Any]
    progress: float                    # 0.0 .. 1.0
    progress_message: str | None
    result: dict[str, Any] | None
    error: str | None
    error_kind: str | None             # machine-readable, drives retry logic
    requested_by: UUID
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @property
    def is_terminal(self) -> bool:
        return self.state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED)
```

### 2.1 Identity travels with the job

Restated from `03-auth-security.md` §5.1 because it is the easiest thing to get wrong here.
The request is gone by the time the job runs; the identity must be in the payload.

```python
@dataclass(frozen=True)
class JobContext:
    job_id: UUID
    requested_by: UUID
    team_ids: frozenset[UUID]
```

> A job that resolves datasets without a `JobContext` is a security bug. Reject in review.

---

## 3. Worker

> **Built today: `ping`, `interpolate_task`, `contour_task`, `aggregate_task`,
> `clip_task`, `anchor_task`, and no cron jobs.** The `ingest`, `sync` and `export` tasks
> below are still owed — Phase 4 for the first, Phase 6 for the other two — and the four cron
> entries with them.
>
> Every registered kind has an entry in `webmap_worker.progress.PHASES`, and
> `ProgressReporter` refuses to construct without one. A job with no phases reports nothing
> for its whole run, which reads as a hang; making that a construction error rather than a
> silent zero is why the two lists cannot drift.

```python
# apps/worker/src/webmap_worker/main.py

from arq import cron
from arq.connections import RedisSettings

from webmap_core.settings import settings


async def startup(ctx: dict) -> None:
    ctx["db"] = await create_engine(settings.database_url)
    ctx["storage"] = create_storage_client(settings)


async def shutdown(ctx: dict) -> None:
    await ctx["db"].dispose()


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    functions = [
        interpolate_task, contour_task, aggregate_task,
        ingest_task, sync_task, export_task,
    ]
    cron_jobs = [
        cron(reconcile_stale_datasets, hour=2, minute=0),
        cron(purge_expired_sessions,   hour=3, minute=0),
        cron(hard_delete_soft_deleted, hour=4, minute=0),
        # Copy-on-write grows storage with edit count. Every version for
        # 30 days, then thinned to daily. See adr/0005 and 09 §5.3.
        cron(thin_feature_versions,    hour=5, minute=0),
    ]
    on_startup = startup
    on_shutdown = shutdown

    # One at a time. Geoprocessing is CPU- and memory-bound; two concurrent
    # krige jobs on the same worker contend for cores and can exhaust memory
    # on large grids. Scale by adding workers, not concurrency.
    max_jobs = 1

    # Jobs are long. arq's default is far too short.
    job_timeout = 3600
    keep_result = 86400
```

---

## 4. Progress reporting

Gridding jobs run for minutes. A frozen progress bar is indistinguishable from a hung worker.

```python
# apps/worker/src/webmap_worker/progress.py

class ProgressReporter:
    """Reports progress with named phases and throttled writes.

    Phase weights are deliberate: the geologist should see the bar move at a
    roughly constant rate, which means weighting phases by their real
    duration, not by their conceptual importance.
    """

    PHASES = {
        "interpolate": [
            ("Loading control points",      0.05),
            ("Fitting variogram",           0.10),
            ("Validating fault network",    0.05),
            ("Building constrained mesh",   0.15),
            ("Solving",                     0.55),
            ("Writing grid",                0.10),
        ],
        # …and one entry per registered kind: `contour`, `contour_filled`,
        # `aggregate`, `clip`, `label_anchors`. Weights are measured
        # proportions of a real run, not guesses about which step sounds like
        # the work — which is why `clip` gives its own operation 10% and the
        # two reads and the COG write the other 90%.
    }

    def __init__(self, db, job_id: UUID, kind: str, min_interval: float = 1.0):
        self._phases = self.PHASES[kind]
        self._min_interval = min_interval   # throttle DB writes
        self._last_write = 0.0

    async def phase(self, name: str) -> None: ...

    async def within_phase(self, fraction: float, detail: str | None = None) -> None:
        """Sub-progress inside the current phase. Used by the solver, which
        knows its iteration count."""
```

Wire it into the solver:

```python
async def interpolate_task(ctx: dict, payload: dict) -> dict:
    jc = JobContext(**payload["context"])
    reporter = ProgressReporter(ctx["db"], jc.job_id, "interpolate")

    async with run_with_identity(jc, load_points, payload["dataset_id"]) as pts:
        await reporter.phase("Fitting variogram")
        variogram = fit_variogram(pts, payload["variogram"])

        await reporter.phase("Building constrained mesh")
        mesh = build_mesh(pts.xy, constraints, bbox, max_area)

        await reporter.phase("Solving")
        grid = ordinary_kriging(
            pts.xy, pts.values, grid_def, variogram, constraints, mesh=mesh,
            on_progress=lambda f: reporter.within_phase(f),
        )
```

---

## 5. Status API

### 5.1 Polling (default)

```
GET /api/v1/jobs/{job_id}
```

```json
{
  "id": "7c2e...", "kind": "interpolate", "state": "running",
  "progress": 0.62, "progress_message": "Solving on constrained mesh (1.2M cells)",
  "queued_at": "2026-09-02T14:22:01Z", "started_at": "2026-09-02T14:22:03Z",
  "estimated_remaining_seconds": 21,
  "poll_after_seconds": 3
}
```

**`poll_after_seconds` is the backoff, and the server owns it.** It varies by state — a queued
job is worth checking less often than a running one — and having the server say so means every
client backs off the same way. The MCP job status already reads it; `07` §7's web client should
too, rather than computing a second curve that drifts from it.

Claude's polling guidance is in the `webmap_get_job` tool description, which restates the same
number in prose.

### 5.2 WebSocket (progressive enhancement)

```
WS /api/v1/jobs/{job_id}/stream
```

For the SPA only. Falls back to polling on failure. Not used by MCP — Claude polls.

---

## 6. Conditional async

Small aggregations should not force a poll cycle. Estimate first.

```python
async def aggregate(principal, request) -> AggregateResponse:
    """Run inline when cheap, enqueue when not.

    The threshold is deliberately conservative. Blocking an API worker for
    5 seconds is acceptable; blocking it for 30 is not, because it starves
    other requests on the same process.
    """
    estimate = await estimate_cost(principal, request)

    if estimate.seconds < 5.0:
        result = await run_aggregation(principal, request)
        return AggregateResponse(mode="inline", dataset_id=result.dataset_id)

    job_id = await enqueue("aggregate_task", principal, request)
    return AggregateResponse(
        mode="job", job_id=job_id,
        estimated_seconds=estimate.seconds,
    )
```

The MCP tool response states which happened, so Claude knows whether to poll.

> **What was built, and where it differs.** The threshold is applied *per operation kind*
> rather than by estimating each request:
>
> - **`POST /api/v1/datasets/{id}/variogram` runs inline.** `05` §10 budgets a fit at under
>   five seconds on a 20k subsample, and the question it answers — "is this layer worth
>   kriging?" — is asked while deciding whether to grid at all. Behind a poll cycle it is an
>   answer nobody waits for.
> - **`POST /api/v1/jobs/aggregate` is always a job**, even for a buffer that finishes in
>   200 ms. `estimate_cost` above does not exist, and the honest reason is that a useful
>   estimate for a dissolve needs the feature count *and* the geometry complexity, which means
>   reading the layer — at which point the estimate costs what the operation costs. One path
>   is simpler than a threshold that guesses, and the cost is a poll on work that was already
>   done. Revisit if the latency is ever a complaint; the shape above is still the right one.

---

## 7. Quotas

Business units share infrastructure. One geologist kriging 500k points should not starve
another team.

```python
# python/webmap_core/src/webmap_core/quota.py

@dataclass(frozen=True)
class QuotaPolicy:
    max_concurrent_jobs_per_user: int = 3
    max_concurrent_jobs_per_team: int = 8
    max_queued_jobs_per_user: int = 20
    max_grid_cells: int = 16_000_000
    max_interpolation_points: int = 2_000_000
    daily_compute_seconds_per_user: int = 14_400   # 4 hours


async def check_quota(db, principal: Principal, kind: str, params: dict) -> None:
    """Raise QuotaExceeded with a message naming the limit and when it resets.

    'Quota exceeded' alone is useless. 'You have 3 jobs running (limit 3);
    the oldest started 2 minutes ago' lets someone decide whether to wait or
    cancel.
    """
```

Quota errors are surfaced to Claude with the same actionable-message discipline as everything
else.

---

## 8. Failure and retry

```python
class ErrorKind(StrEnum):
    TRANSIENT = "transient"        # retry automatically
    RESOURCE = "resource"          # OOM, timeout — retry once on a bigger worker
    INPUT = "input"                # bad data — never retry, tell the user
    PERMISSION = "permission"      # never retry
    INTERNAL = "internal"          # bug — never retry, alert
```

Retry policy:

| Kind | Retries | Backoff |
|---|---|---|
| `transient` | 3 | exponential, 2 s base |
| `resource` | 1 | immediate, routed to a high-memory queue |
| `input`, `permission`, `internal` | 0 | — |

**Input errors must be specific.** The fault-validation failure example in `04-mcp-server.md`
§8.1 is the standard: name each problem, give its location, offer both a fix and a fallback,
and state the consequence of the fallback.

---

## 9. Cancellation

```python
async def cancel_job(db, redis, principal: Principal, job_id: UUID) -> None:
    """Cancel a queued or running job.

    Queued: aborted before it starts.
    Running: a cancellation flag is set; the worker checks it at phase
             boundaries and inside the solver's iteration loop. Cancellation
             is cooperative — a job in the middle of a triangulation call
             will finish that call first, which can take seconds.
    """
```

Long-running loops must poll the flag:

```python
for iteration in range(max_iterations):
    if iteration % 50 == 0 and await reporter.is_cancelled():
        raise JobCancelled()
    ...
```

Partial outputs are discarded on cancel. Never leave a half-written COG registered as a
dataset.

---

## 10. Idempotency

Claude may retry a tool call after a timeout. Without protection that submits duplicate
gridding jobs.

```python
async def enqueue_idempotent(
    db, redis, principal: Principal, kind: str, params: dict
) -> tuple[UUID, bool]:
    """Returns (job_id, was_created).

    Key is a hash of (kind, requested_by, canonical params). An identical
    request within the dedupe window returns the existing job rather than
    creating a new one.
    """
    key = hashlib.sha256(
        json.dumps(
            {"kind": kind, "user": str(principal.user_id), "params": params},
            sort_keys=True, separators=(",", ":"),
        ).encode()
    ).hexdigest()

    existing = await redis.get(f"idem:{key}")
    if existing:
        return UUID(existing.decode()), False

    job_id = await _create_and_enqueue(db, redis, principal, kind, params)
    await redis.set(f"idem:{key}", str(job_id), ex=3600)
    return job_id, True
```

The MCP response says which occurred: "Already running as job `7c2e…` (started 40 s ago)."

---

## 11. Observability

Metrics to alert on:

| Metric | Alert threshold |
|---|---|
| `webmap_job_queue_depth` | > 50 for 5 min |
| `webmap_job_duration_seconds{kind}` p95 | > 2× the target in `05-geoprocessing.md` §10 |
| `webmap_job_failures_total{error_kind="internal"}` | any |
| `webmap_worker_oom_total` | any |
| `webmap_render_pool_saturation` | > 0.9 for 5 min |

Every job log line carries `job_id`, `kind`, and `requested_by`. The OpenTelemetry trace spans
MCP call → API → worker, which is the only way to debug "the map Claude gave me is wrong"
after the fact.
