"""arq worker settings. `10-jobs-async.md` §3."""

from typing import Any, ClassVar

from arq.connections import RedisSettings

from webmap_core.logging import configure_logging, get_logger
from webmap_core.settings import Environment, get_settings
from webmap_worker.tasks.health import ping

log = get_logger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.environment is Environment.PROD)
    ctx["settings"] = settings
    log.info("worker_started")


async def shutdown(ctx: dict[str, Any]) -> None:
    log.info("worker_stopped")


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)

    # Phase 4 adds interpolate, contour, aggregate, ingest, sync and export.
    # `ping` is not a placeholder for those: it is the round-trip smoke test
    # for the queue path, and arq refuses to start a worker whose function
    # list is empty — which is how this container silently crashlooped the
    # first time the stack came up.
    functions: ClassVar[list[Any]] = [ping]
    cron_jobs: ClassVar[list[Any]] = []

    on_startup = startup
    on_shutdown = shutdown

    # One at a time. Geoprocessing is CPU- and memory-bound; two concurrent
    # krige jobs on the same worker contend for cores and can exhaust memory
    # on large grids. Scale by adding workers, not concurrency.
    max_jobs = 1

    # Jobs are long. arq's default is far too short.
    job_timeout = 3600
    keep_result = 86400
