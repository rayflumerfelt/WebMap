"""What every task needs and no task should re-derive.

Cancellation checks, failure classification, and the arq context's shared
clients. Each of these is a decision rather than a utility, and having two
tasks make it two different ways is how a job ends up retrying a bad column
name four times while another leaves a half-written object behind.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from webmap_core.exceptions import NotFound, PermissionDenied, QuotaExceeded, WebMapError
from webmap_core.jobs import ErrorKind, JobCancelled
from webmap_core.services import jobs


async def check_cancelled(redis: Any, job_id: UUID) -> None:
    """`10` §9. Polled at phase boundaries; raises so the caller unwinds.

    Raising rather than returning a flag because every caller would have to
    check it and one would forget — and the one that forgot would be the one
    that writes the COG.
    """
    if await jobs.is_cancelled(redis, job_id):
        raise JobCancelled(f"Job {job_id} was cancelled before its output was written.")


def classify(error: Exception) -> ErrorKind:
    """`10` §8. Which failures are worth retrying.

    A bad value column will be bad again in thirty seconds; a dropped S3
    connection will not. Getting this wrong in the retryable direction turns a
    clear message into four identical failures and a much later one.
    """
    from webmap_geo.exceptions import DegenerateInput, GeoError, NotProjected, UnknownCrs

    if isinstance(error, PermissionDenied):
        return ErrorKind.PERMISSION
    if isinstance(error, QuotaExceeded):
        return ErrorKind.RESOURCE
    if isinstance(error, DegenerateInput | NotProjected | UnknownCrs | NotFound):
        return ErrorKind.INPUT
    if isinstance(error, MemoryError):
        return ErrorKind.RESOURCE
    if isinstance(error, GeoError | WebMapError):
        return ErrorKind.INPUT
    return ErrorKind.INTERNAL


def resources(ctx: dict[str, Any]) -> tuple[Any, Any, Any, str, Any]:
    """Pull the worker's shared clients out of the arq context.

    Named here rather than indexed inline so a missing key fails with
    something a person can act on: an arq context is a plain dict, and a
    KeyError from deep inside a solve says nothing about worker startup.
    """
    missing = [key for key in ("engine", "storage", "object_store", "bucket") if key not in ctx]
    if missing:
        raise RuntimeError(
            f"The worker context is missing {', '.join(missing)}. These are "
            f"created in webmap_worker.main.startup — a task cannot run without "
            f"them, and a KeyError from inside a solve would not say so."
        )
    return (
        ctx["engine"],
        ctx["storage"],
        ctx["object_store"],
        ctx["bucket"],
        ctx.get("redis"),
    )


__all__ = ["check_cancelled", "classify", "resources"]
