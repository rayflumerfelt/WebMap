"""Job lifecycle. `10-jobs-async.md` §2, §7, §9, §10.

The row is the source of truth about a job's state, not the queue. arq knows
whether a task is running; only this table knows whether the *work* succeeded,
what it produced, and who asked for it — and that is what a status poll, an
audit query and a retry decision all read.

Three behaviours here are worth more than they look:

- **Identity travels in the payload** (§2.1). The request is gone by the time
  a job runs, so a job that resolves datasets without a `JobContext` is a
  security bug rather than a style issue.
- **Idempotency** (§10). Claude retries a tool call after a timeout, and
  without protection that submits a second gridding job — two workers doing
  identical work, two datasets registered, and a conversation that cannot say
  which is which.
- **Cancellation is cooperative** (§9). A flag the worker polls, and partial
  outputs are discarded. Never leave a half-written COG registered.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from webmap_core.exceptions import NotFound, PermissionDenied
from webmap_core.jobs import ErrorKind, JobContext
from webmap_core.logging import get_logger
from webmap_core.models import JobState
from webmap_core.permissions import Principal
from webmap_core.quota import DEFAULT_POLICY, QuotaPolicy, Usage, check_concurrency

log = get_logger(__name__)

#: `10` §10. Long enough to cover a retry after a client timeout, short enough
#: that a deliberate re-run an hour later is honoured rather than deduplicated.
IDEMPOTENCY_WINDOW_SECONDS = 3600

#: Redis key prefixes, named here so the worker and the API cannot disagree.
CANCEL_KEY = "job:cancel:"
IDEMPOTENCY_KEY = "job:idem:"


@dataclass(frozen=True)
class Enqueued:
    """What `enqueue` produced, and whether it was new.

    `was_created` is not bookkeeping — `10` §10 requires the MCP response to
    say which happened: "Already running as job 7c2e… (started 40 s ago)"
    tells Claude to poll rather than to resubmit.
    """

    job_id: UUID
    was_created: bool


def idempotency_key(kind: str, principal: Principal, parameters: dict[str, Any]) -> str:
    """A stable hash of (kind, user, canonical parameters).

    Canonical: sorted keys and no whitespace, so two payloads that differ only
    in key order hash the same. Without that the dedupe would miss the exact
    case it exists for — a client retrying its own serialised request.

    Keyed by user as well as parameters, because two geologists gridding the
    same dataset with the same settings are doing two pieces of work: they own
    different outputs and each expects their own job.
    """
    canonical = json.dumps(
        {"kind": kind, "user": str(principal.user_id), "params": parameters},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


async def enqueue(
    conn: AsyncConnection,
    principal: Principal,
    *,
    kind: str,
    parameters: dict[str, Any],
    redis: Any = None,
    policy: QuotaPolicy = DEFAULT_POLICY,
) -> Enqueued:
    """Create a job row, subject to quota and idempotency.

    Returns the existing job when an identical request from the same user is
    still inside the window (§10). The queue submission itself is the caller's
    to make, after this returns — so a job row always exists before anything
    can pick it up, and a worker never finds a task with no row.
    """
    key = idempotency_key(kind, principal, parameters)

    if redis is not None:
        existing = await redis.get(f"{IDEMPOTENCY_KEY}{key}")
        if existing:
            job_id = UUID(existing.decode() if isinstance(existing, bytes) else str(existing))
            log.info("job_deduplicated", kind=kind, job_id=str(job_id))
            return Enqueued(job_id=job_id, was_created=False)

    await _check_quota(conn, principal, policy)

    result = await conn.execute(
        text(
            """
            INSERT INTO job (kind, state, parameters, requested_by)
            VALUES (:kind, 'queued', CAST(:parameters AS jsonb), :requested_by)
            RETURNING id
            """
        ),
        {
            "kind": kind,
            "parameters": json.dumps(parameters, default=str),
            "requested_by": principal.user_id,
        },
    )
    job_id = UUID(str(result.scalar_one()))

    if redis is not None:
        await redis.set(f"{IDEMPOTENCY_KEY}{key}", str(job_id), ex=IDEMPOTENCY_WINDOW_SECONDS)

    log.info("job_enqueued", kind=kind, job_id=str(job_id))
    return Enqueued(job_id=job_id, was_created=True)


async def _check_quota(
    conn: AsyncConnection, principal: Principal, policy: QuotaPolicy
) -> None:
    """Measure this principal's usage and hand it to `quota.check_concurrency`.

    The limits and their messages live in `webmap_core.quota`; this function's
    only job is the measurement. Refusing at submission rather than queueing
    indefinitely is the deliberate part: a job sitting behind five of your own
    is indistinguishable from a stuck one, and queue depth is invisible from a
    conversation.

    `oldest_running_job_age_seconds` is measured rather than omitted because
    it is what turns the refusal into a decision — "the oldest started 40
    minutes ago" is how someone chooses between waiting and cancelling.
    """
    result = await conn.execute(
        text(
            """
            SELECT
                count(*) FILTER (
                    WHERE requested_by = :user AND state = 'running') AS running,
                count(*) FILTER (
                    WHERE requested_by = :user AND state = 'queued') AS queued,
                count(*) FILTER (
                    WHERE state = 'running'
                      AND requested_by = ANY(:teammates)) AS team_running,
                max(EXTRACT(EPOCH FROM (now() - started_at))) FILTER (
                    WHERE requested_by = :user AND state = 'running'
                ) AS oldest_age,
                coalesce(sum(EXTRACT(EPOCH FROM (finished_at - started_at))) FILTER (
                    WHERE requested_by = :user
                      AND finished_at >= date_trunc('day', now() AT TIME ZONE 'UTC')
                ), 0) AS compute_today
            FROM job
            """
        ),
        {"user": principal.user_id, "teammates": await _teammates(conn, principal)},
    )
    row = result.one()

    check_concurrency(
        Usage(
            running_jobs=int(row.running),
            queued_jobs=int(row.queued),
            team_running_jobs=int(row.team_running),
            compute_seconds_today=int(row.compute_today),
            oldest_running_job_age_seconds=(
                int(row.oldest_age) if row.oldest_age is not None else None
            ),
        ),
        policy,
    )


async def _teammates(conn: AsyncConnection, principal: Principal) -> list[UUID]:
    """Everyone sharing a team with this principal, including them.

    `job` carries no team column, so the team's usage is resolved through
    membership. Counting *every* running job instead would be simpler and
    would produce "your team has 8 jobs running" about strangers, which is a
    refusal someone cannot act on — they would go and ask their team, and
    their team would not be running anything.
    """
    if not principal.team_ids:
        return [principal.user_id]

    result = await conn.execute(
        text("SELECT DISTINCT user_id FROM team_member WHERE team_id = ANY(:teams)"),
        {"teams": sorted(principal.team_ids)},
    )
    return sorted({principal.user_id, *(row.user_id for row in result)})


async def get_job(conn: AsyncConnection, principal: Principal, job_id: UUID) -> dict[str, Any]:
    """One job. Visible to the person who asked for it.

    Not an ownable object with grants: a job is an action rather than an
    artefact, and its *output* is what gets shared. Someone else's job status
    would leak what they are working on.
    """
    result = await conn.execute(
        text(
            """
            SELECT id, kind, state, parameters, progress, progress_message,
                   result, error, error_kind, requested_by, queued_at,
                   started_at, finished_at
            FROM job WHERE id = :id
            """
        ),
        {"id": job_id},
    )
    row = result.one_or_none()
    if row is None:
        raise NotFound(
            f"No job {job_id}. It may have been cleaned up — job rows are kept "
            f"for a limited time after they finish."
        )

    job = dict(row._mapping)
    if job["requested_by"] != principal.user_id:
        raise PermissionDenied(
            "That job belongs to someone else. A job's status says what another "
            "person is working on, so it is visible only to whoever requested "
            "it — the dataset it produces is what gets shared."
        )

    for key in ("parameters", "result"):
        if isinstance(job.get(key), str):
            job[key] = json.loads(job[key])
    return job


async def list_jobs(
    conn: AsyncConnection, principal: Principal, *, active_only: bool = False, limit: int = 25
) -> list[dict[str, Any]]:
    clause = "AND state IN ('queued', 'running')" if active_only else ""
    result = await conn.execute(
        text(
            f"""
            SELECT id, kind, state, progress, progress_message, queued_at,
                   started_at, finished_at
            FROM job
            WHERE requested_by = :user {clause}
            ORDER BY queued_at DESC
            LIMIT :limit
            """
        ),
        {"user": principal.user_id, "limit": max(1, min(limit, 100))},
    )
    return [dict(row._mapping) for row in result]


async def mark_running(conn: AsyncConnection, job_id: UUID) -> None:
    await conn.execute(
        text(
            "UPDATE job SET state = 'running', started_at = now() "
            "WHERE id = :id AND state = 'queued'"
        ),
        {"id": job_id},
    )


async def report_progress(
    conn: AsyncConnection, job_id: UUID, fraction: float, message: str | None = None
) -> None:
    """`10` §4. Progress is a fraction and a sentence.

    The sentence matters more than the number: "Fitting variogram (2,000 of
    18,000 points)" tells someone the job is alive and where it is, and 0.34
    on its own does not.
    """
    await conn.execute(
        text(
            "UPDATE job SET progress = :progress, progress_message = :message "
            "WHERE id = :id AND state = 'running'"
        ),
        {"id": job_id, "progress": max(0.0, min(1.0, fraction)), "message": message},
    )


async def mark_succeeded(conn: AsyncConnection, job_id: UUID, result: dict[str, Any]) -> None:
    await conn.execute(
        text(
            """
            UPDATE job SET state = 'succeeded', progress = 1.0,
                           result = CAST(:result AS jsonb), finished_at = now()
            WHERE id = :id
            """
        ),
        {"id": job_id, "result": json.dumps(result, default=str)},
    )


async def mark_failed(
    conn: AsyncConnection,
    job_id: UUID,
    error: str,
    error_kind: ErrorKind = ErrorKind.INTERNAL,
) -> None:
    """Record a failure with its class.

    `error_kind` drives retry policy (§8): retrying an INPUT error just fails
    four times slower and buries the message that would have told the user what
    to fix.
    """
    await conn.execute(
        text(
            """
            UPDATE job SET state = 'failed', error = :error,
                           error_kind = :error_kind, finished_at = now()
            WHERE id = :id
            """
        ),
        {"id": job_id, "error": error[:4000], "error_kind": error_kind.value},
    )


async def request_cancel(
    conn: AsyncConnection, principal: Principal, job_id: UUID, redis: Any = None
) -> str:
    """Ask a job to stop. `10` §9.

    A queued job is aborted outright. A running one is *asked*: the flag is set
    and the worker checks it at phase boundaries and inside the solver's
    iteration loop, so a job in the middle of a triangulation call finishes
    that call first. Cancellation is cooperative, and saying so is what stops
    someone concluding it did not work.
    """
    job = await get_job(conn, principal, job_id)
    state = JobState(job["state"])

    if state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED):
        return f"Job {job_id} already finished ({state.value}); nothing to cancel."

    if redis is not None:
        await redis.set(f"{CANCEL_KEY}{job_id}", "1", ex=IDEMPOTENCY_WINDOW_SECONDS)

    if state is JobState.QUEUED:
        await conn.execute(
            text(
                "UPDATE job SET state = 'cancelled', finished_at = now() "
                "WHERE id = :id AND state = 'queued'"
            ),
            {"id": job_id},
        )
        return f"Job {job_id} was queued and has been cancelled."

    return (
        f"Cancellation requested for job {job_id}. It is running, so it will stop "
        f"at its next checkpoint — within a few seconds for most steps, longer if "
        f"it is inside a triangulation. Nothing partial will be registered."
    )


async def is_cancelled(redis: Any, job_id: UUID) -> bool:
    """Polled by the worker inside long loops."""
    if redis is None:
        return False
    return bool(await redis.get(f"{CANCEL_KEY}{job_id}"))


async def mark_cancelled(conn: AsyncConnection, job_id: UUID) -> None:
    await conn.execute(
        text("UPDATE job SET state = 'cancelled', finished_at = now() WHERE id = :id"),
        {"id": job_id},
    )


def context_for(principal: Principal, job_id: UUID) -> JobContext:
    """The identity that travels with the payload (§2.1)."""
    return JobContext(
        job_id=job_id, requested_by=principal.user_id, team_ids=frozenset(principal.team_ids)
    )


__all__ = [
    "CANCEL_KEY",
    "IDEMPOTENCY_KEY",
    "IDEMPOTENCY_WINDOW_SECONDS",
    "Enqueued",
    "context_for",
    "enqueue",
    "get_job",
    "idempotency_key",
    "is_cancelled",
    "list_jobs",
    "mark_cancelled",
    "mark_failed",
    "mark_running",
    "mark_succeeded",
    "report_progress",
    "request_cancel",
]
