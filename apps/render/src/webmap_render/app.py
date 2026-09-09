"""The render service's HTTP surface. `06-rendering.md` §2.

A single endpoint behind a browser pool. It is deliberately dumb: it renders a
style it is given and returns a PNG. It holds no database connection, resolves
no permissions and knows nothing about datasets — the API has already decided
who may see what and has assembled the style from validated layer references
(`06` §6, `03-auth-security.md` §7.3).

That division is what makes the network isolation in §7.3 meaningful. A render
worker that could reach the database would be a second, unaudited path to every
dataset, reachable by anything that could make it render.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from webmap_core.logging import configure_logging, get_logger
from webmap_render.pool import BrowserPool
from webmap_render.security import DEFAULT_ALLOWED_HOSTS, StyleRejected
from webmap_render.service import SIZE_PRESETS, RenderSpec, render

log = get_logger(__name__)

SHELL_PATH = Path(__file__).resolve().parents[2] / "shell" / "index.html"


class RenderRequest(BaseModel):
    """What the API asks for.

    `style` arrives assembled — the render service never builds one, and never
    accepts source URLs from a browser. `03-auth-security.md` §7.3: "never
    render a style document that arrived from a client verbatim."
    """

    model_config = {"extra": "forbid"}

    style: dict[str, Any]
    size_preset: str = "slide_full"
    bounds: tuple[float, float, float, float] | None = None
    center: tuple[float, float] | None = None
    zoom: float | None = None
    bearing: float = 0.0
    pitch: float = Field(default=0.0, ge=0.0, le=85.0)
    padding: int = Field(default=24, ge=0, le=200)
    overlay: dict[str, Any] | None = None
    transparent: bool = False


class RenderResponse(BaseModel):
    image_base64: str
    preview_base64: str
    width: int
    height: int
    failed_requests: list[dict[str, Any]]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging("INFO", json_output=True)
    pool = BrowserPool()
    await pool.start()
    app.state.pool = pool
    app.state.allowed_hosts = DEFAULT_ALLOWED_HOSTS
    app.state.shell_url = SHELL_PATH.as_uri()
    log.info("render_service_ready", shell=str(SHELL_PATH))
    try:
        yield
    finally:
        await pool.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="WebMap render", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(StyleRejected)
    async def _rejected(_: Request, exc: StyleRejected) -> JSONResponse:
        # 400, not 500: a refused style is the caller's document, and the
        # message names the host so whoever assembled it can fix it.
        log.warning("style_rejected", detail=str(exc))
        return JSONResponse(
            status_code=400, content={"error": "style_rejected", "detail": str(exc)}
        )

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        pool: BrowserPool = request.app.state.pool
        return {
            "status": "ok",
            "renders_since_recycle": pool.render_count,
            "presets": sorted(SIZE_PRESETS),
        }

    @app.post("/render", response_model=RenderResponse)
    async def render_endpoint(
        request: Request,
        body: RenderRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> RenderResponse:
        """Render a map.

        The bearer token is forwarded to tile requests by the route guards and
        **never enters page JavaScript** (`03` §7.2). It is the caller's own
        token: a render can therefore never reach a layer its requester could
        not, which is what makes one authorization code path rather than two.
        """
        token = (authorization or "").removeprefix("Bearer ").strip()

        output = await render(
            request.app.state.pool,
            RenderSpec(**body.model_dump()),
            token,
            allowed_hosts=request.app.state.allowed_hosts,
            shell_url=request.app.state.shell_url,
        )

        if output.failed_requests:
            log.warning("render_incomplete", failures=len(output.failed_requests))

        return RenderResponse(
            image_base64=base64.b64encode(output.image).decode(),
            preview_base64=base64.b64encode(output.preview).decode(),
            width=output.width,
            height=output.height,
            failed_requests=output.failed_requests,
        )

    return app


app = create_app()

__all__ = ["RenderRequest", "RenderResponse", "app", "create_app"]
