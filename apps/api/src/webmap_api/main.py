"""The FastAPI application.

Phase 0 stands the service up: configuration, logging, tracing, the database
engine, and the startup assertions that make row-level security real. The
routers that serve datasets, sessions, and tiles arrive in Phase 1 and 2.
"""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text

from webmap_api.auth import build_verifier
from webmap_api.db import assert_rls_enforced, create_engine
from webmap_api.db.session import assert_policies_present, unscoped_session
from webmap_api.routes.auth import router as auth_router
from webmap_api.routes.datasets import router as datasets_router
from webmap_api.routes.projects import router as projects_router
from webmap_api.routes.uploads import router as uploads_router
from webmap_core.exceptions import (
    LimitExceeded,
    NotFound,
    PermissionDenied,
    QuotaExceeded,
    VersionConflict,
    WebMapError,
)
from webmap_core.identity import AuthenticationFailed
from webmap_core.logging import bind_request, configure_logging, get_logger
from webmap_core.settings import Environment, Settings, get_settings
from webmap_io.exceptions import (
    MissingCRS,
    PathTraversal,
    UnknownShare,
    UnsupportedFormat,
    WebMapIOError,
)

log = get_logger(__name__)

#: Domain errors mapped to status codes. Anything not listed is a bug and
#: becomes a 500 — deliberately, so an unhandled case is loud rather than
#: quietly returning 400 and looking like the caller's fault.
_STATUS_FOR: dict[type[Exception], int] = {
    AuthenticationFailed: 401,
    PermissionDenied: 403,
    NotFound: 404,
    VersionConflict: 409,
    QuotaExceeded: 429,
    LimitExceeded: 413,
    # I/O failures are the caller's file, not our fault: 422. These carry the
    # messages `11-file-io.md` §8 insists on, and returning 500 for them would
    # replace "your shapefile is missing its .prj" with "internal error".
    MissingCRS: 422,
    UnsupportedFormat: 422,
    PathTraversal: 400,
    UnknownShare: 404,
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.environment is Environment.PROD)

    engine = create_engine(settings.database_url)
    app.state.settings = settings
    app.state.engine = engine
    # Guard 3 of 4 (adr/0009): a non-production verifier announces itself at
    # WARNING, naming the mode and environment.
    app.state.verifier = build_verifier(settings)

    # Both assertions run before the first request is served. A deployment
    # that cannot prove RLS is enforced does not start — see
    # `03-auth-security.md` §3.5 and `02-data-model.md` §4.
    await assert_rls_enforced(engine)
    await assert_policies_present(engine)

    log.info(
        "api_started",
        environment=settings.environment.value,
        auth_mode=settings.auth_mode,
    )
    try:
        yield
    finally:
        await engine.dispose()
        log.info("api_stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(
        title="WebMap API",
        version="0.1.0",
        summary="Geospatial mapping for subsurface geologists.",
        lifespan=lifespan,
    )
    if settings is not None:
        app.state.settings = settings

    @app.middleware("http")
    async def bind_logging_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Give every log line for this request a correlation id and channel.

        `X-WebMap-Channel` is set by the MCP server to 'claude'. It is a
        *label*, not a credential — it drives `audit_event.actor_channel` and
        nothing else. Authorization never reads it, because the header comes
        from a process the user controls.
        """
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        channel = "claude" if request.headers.get("X-WebMap-Channel") == "claude" else "web"
        bind_request(request_id=request_id, channel=channel)

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(WebMapError)
    @app.exception_handler(WebMapIOError)
    async def handle_domain_error(request: Request, exc: Exception) -> JSONResponse:
        """Domain errors keep their message; unmapped ones do not.

        `CLAUDE.md` §8 messages are written to be read by a geologist or by
        Claude, so they are returned verbatim for the mapped cases. An
        unmapped WebMapError is a bug, and its message may carry internals —
        log it, return a generic body.
        """
        status = _STATUS_FOR.get(type(exc))
        if status is None:
            log.exception("unmapped_domain_error", error_type=type(exc).__name__)
            return JSONResponse(
                status_code=500,
                content={"error": "internal_error", "detail": "An internal error occurred."},
            )
        return JSONResponse(
            status_code=status,
            content={
                "error": _snake(type(exc).__name__),
                "detail": str(exc),
            },
        )

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        """Liveness. No database — this answers 'is the process up'."""
        return {"status": "ok", "version": app.version}

    @app.get("/health/ready", tags=["ops"])
    async def ready(request: Request) -> dict[str, Any]:
        """Readiness. Touches the database, because a stateless API with no
        database is not ready to serve anything."""
        async with unscoped_session(request.app.state.engine) as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ready"}

    app.include_router(auth_router)
    app.include_router(projects_router)
    app.include_router(uploads_router)
    app.include_router(datasets_router)

    return app


def _snake(name: str) -> str:
    """Class name to the machine-readable `error` code in the response body.

    Acronyms stay whole: `MissingCRS` is `missing_crs`, not `missing_c_r_s`.
    Clients switch on this string, so it has to be the obvious spelling —
    and the naive character-by-character version produced codes nobody would
    guess.
    """
    import re

    # Split before a capital that follows a lowercase, and before the last
    # capital of a run that is followed by a lowercase. That keeps `CRS`
    # together while still separating `MissingCRS` into two words.
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name)
    return spaced.lower()


app = create_app()
