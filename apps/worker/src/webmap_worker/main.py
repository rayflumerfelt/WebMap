"""arq worker settings. `10-jobs-async.md` §3."""

from typing import Any, ClassVar

from arq.connections import RedisSettings

from webmap_core.db.session import create_engine
from webmap_core.logging import configure_logging, get_logger
from webmap_core.settings import Environment, get_settings
from webmap_geo.dataplane import ObjectStore
from webmap_worker.tasks.health import ping
from webmap_worker.tasks.interpolate import interpolate_task

log = get_logger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    """Build the clients every task shares.

    Created once per worker process rather than per job: a gridding job that
    opened its own engine would pay connection setup on every run and, worse,
    would be a second place where a session could be opened without a
    principal (`CLAUDE.md` §3.2).

    `redis` is arq's own connection, already on the context under that key
    when the worker runs. It is read rather than created so the cancellation
    flag the API sets is the one a task polls.
    """
    from webmap_io.storage import StorageConfig, client

    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.environment is Environment.PROD)

    ctx["settings"] = settings
    ctx["engine"] = create_engine(settings.database_url)
    ctx["storage"] = client(
        StorageConfig(
            endpoint=settings.s3_endpoint,
            bucket=settings.s3_bucket,
            access_key=settings.s3_access_key.get_secret_value(),
            secret_key=settings.s3_secret_key.get_secret_value(),
            region=settings.s3_region,
        )
    )
    ctx["bucket"] = settings.s3_bucket
    ctx["object_store"] = ObjectStore(
        # DuckDB's httpfs wants a host:port, not a URL scheme.
        endpoint=settings.s3_endpoint.removeprefix("http://").removeprefix("https://"),
        access_key=settings.s3_access_key.get_secret_value(),
        secret_key=settings.s3_secret_key.get_secret_value(),
        region=settings.s3_region,
        use_ssl=settings.s3_use_ssl,
    )
    log.info("worker_started")


async def shutdown(ctx: dict[str, Any]) -> None:
    engine = ctx.get("engine")
    if engine is not None:
        await engine.dispose()
    log.info("worker_stopped")


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)

    # `ping` is not a placeholder: it is the round-trip smoke test for the
    # queue path, and arq refuses to start a worker whose function list is
    # empty — which is how this container silently crashlooped the first time
    # the stack came up. Phase 4 still owes aggregate, ingest, sync and export.
    functions: ClassVar[list[Any]] = [ping, interpolate_task]
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
