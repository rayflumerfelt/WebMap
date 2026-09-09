"""arq worker settings. `10-jobs-async.md` §3."""

from typing import Any, ClassVar

from arq.connections import RedisSettings

from webmap_core.logging import configure_logging, get_logger
from webmap_core.settings import Environment, get_settings

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

    # Phase 4 fills this in: interpolate, contour, aggregate, ingest, sync,
    # export. Registering none yet is deliberate — an arq worker with a task
    # that raises NotImplementedError still accepts and burns the job.
    functions: ClassVar[list[Any]] = []
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
