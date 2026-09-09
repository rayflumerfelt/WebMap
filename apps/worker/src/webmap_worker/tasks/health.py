"""A queue round-trip task.

arq refuses to start a worker with no registered functions, so the container
needs at least one before Phase 4 delivers the real ones. Rather than a
placeholder, this is the smoke test for the whole async path: enqueue on the
API side, execute in the worker container, read the result back. That path
crosses Redis, the arq serialisation, and the worker image, and until Phase 4
nothing else exercises it.

**This is the only task that does not take a `JobContext`**, and the exception
is narrow on purpose: it resolves no dataset, reads no object, and touches no
owned row, so there is no identity for it to run as. Every task that reaches
data takes one — a job that resolves datasets without a `JobContext` is a
security bug, not a style issue (`03-auth-security.md` §5.1). If this function
ever grows a dataset argument, it needs the context first.
"""

from datetime import UTC, datetime
from typing import Any

from webmap_core.logging import get_logger

log = get_logger(__name__)


async def ping(ctx: dict[str, Any], note: str = "") -> dict[str, Any]:
    """Return worker liveness. Touches no data.

    `note` is echoed back so a caller can correlate a specific enqueue with
    its result, which is what makes this usable as a round-trip check rather
    than merely a liveness bit.
    """
    settings = ctx.get("settings")
    log.info("worker_ping", note=note or None)
    return {
        "ok": True,
        "note": note,
        "job_id": str(ctx.get("job_id", "")),
        "environment": getattr(settings, "environment", "unknown"),
        "at": datetime.now(UTC).isoformat(),
    }
