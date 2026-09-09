"""The render pipeline. `06-rendering.md` §5.

Takes a validated style and a view, drives the shell, and returns a PNG plus
everything that went wrong on the way.

**Failures are quiet** (§5.1), and that is the main operational annoyance this
module exists to answer. A 404 on a tile produces a map with a hole, not an
exception. Without `failed_requests` a geologist cannot tell "the data really
is sparse there" from "the tile server was down" — and they will believe the
first, because it is the one that looks like data.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any

from PIL import Image
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from webmap_core.logging import get_logger
from webmap_render.guards import install_guards
from webmap_render.pool import BrowserPool
from webmap_render.security import StyleRejected, validate_style

log = get_logger(__name__)

#: `06-rendering.md` §5. Logical size and the device scale applied to it, so a
#: slide gets a 2x image without the map being drawn at half the label size.
SIZE_PRESETS: dict[str, tuple[int, int, int]] = {
    "slide_full": (1280, 720, 2),
    "slide_half": (640, 720, 2),
    "slide_quarter": (640, 360, 2),
    "square": (800, 800, 2),
    "thumbnail": (640, 360, 1),
}

#: A map that has not settled in thirty seconds is not going to. The render
#: proceeds anyway (see below) rather than failing.
RENDER_TIMEOUT_MS = 30_000

#: `adr/0006-render-image-delivery.md`. The preview is what reaches Claude in
#: an MCP response; the master is what goes on a slide.
PREVIEW_LONGEST_EDGE = 1600


@dataclass(frozen=True)
class RenderSpec:
    """What to draw.

    **The auth token is deliberately not here.** It is passed separately to
    `install_guards` and lives only in that closure — putting it on the spec
    would carry it into page JavaScript, where the shell has no use for it and
    any script the style manages to load could read it
    (`03-auth-security.md` §7.2).
    """

    style: dict[str, Any]
    size_preset: str = "slide_full"
    bounds: tuple[float, float, float, float] | None = None
    center: tuple[float, float] | None = None
    zoom: float | None = None
    bearing: float = 0.0
    pitch: float = 0.0
    padding: int = 24
    overlay: dict[str, Any] | None = None
    transparent: bool = False

    def to_page_spec(self) -> dict[str, Any]:
        """The object handed to the shell. Carries no credential."""
        spec: dict[str, Any] = {
            "style": self.style,
            "padding": self.padding,
            "bearing": self.bearing,
            "pitch": self.pitch,
        }
        if self.bounds is not None:
            # MapLibre wants [[west, south], [east, north]].
            west, south, east, north = self.bounds
            spec["bounds"] = [[west, south], [east, north]]
        if self.center is not None:
            spec["center"] = list(self.center)
            spec["zoom"] = self.zoom if self.zoom is not None else 9
        if self.overlay is not None:
            spec["overlay"] = self.overlay
        return spec


@dataclass(frozen=True)
class RenderOutput:
    image: bytes
    preview: bytes
    width: int
    height: int
    failed_requests: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return not self.failed_requests


async def render(
    pool: BrowserPool,
    spec: RenderSpec,
    auth_token: str,
    *,
    allowed_hosts: frozenset[str],
    shell_url: str,
) -> RenderOutput:
    """Render one map.

    Validation happens before a browser is touched: a style naming a host the
    renderer will not fetch is refused rather than partially drawn, because a
    map with the internal endpoint's response painted into one corner is worse
    than no map (`03-auth-security.md` §7.1).
    """
    validate_style(spec.style, allowed_hosts)

    if spec.size_preset not in SIZE_PRESETS:
        raise StyleRejected(
            f"Unknown size preset '{spec.size_preset}'. Available presets: "
            f"{', '.join(sorted(SIZE_PRESETS))}."
        )
    width, height, scale = SIZE_PRESETS[spec.size_preset]

    failed: list[dict[str, Any]] = []

    async with pool.context(width, height, scale) as ctx:
        page = await ctx.new_page()
        await install_guards(page, allowed_hosts, auth_token, failed)
        await page.goto(shell_url)

        # **Started, not awaited.** `renderMap` returns a promise that resolves
        # when the map settles; awaiting it here would block `page.evaluate`
        # indefinitely for a map that never settles — before the bounded wait
        # below ever runs. That turned a thirty-second timeout into an
        # unbounded hang, which presents as a render request that never
        # returns. The braces are what discard the promise.
        await page.evaluate("(s) => { window.renderMap(s); }", spec.to_page_spec())

        try:
            await page.wait_for_function(
                "window.__mapReady === true", timeout=RENDER_TIMEOUT_MS
            )
        except PlaywrightTimeout:
            # Screenshot anyway. A partial map with a warning is more useful to
            # a geologist than an error string — they can see which part is
            # missing, which an exception never tells them.
            failed.append({"reason": "render_timeout", "url": None})
            log.warning("render_timeout", preset=spec.size_preset)

        try:
            shell_error = await page.evaluate("window.__renderError")
            if shell_error:
                failed.append({"reason": "shell_error", "message": shell_error})
            failed.extend(await page.evaluate("window.__failedRequests") or [])
        except PlaywrightError:
            # The page died outright. What was captured before that still
            # describes the failure better than nothing.
            failed.append({"reason": "page_lost", "url": None})

        png = await page.screenshot(
            type="png", omit_background=spec.transparent, full_page=False
        )

    return RenderOutput(
        image=png,
        preview=downscale_png(png, PREVIEW_LONGEST_EDGE),
        width=width * scale,
        height=height * scale,
        failed_requests=failed,
    )


def downscale_png(png: bytes, longest_edge: int) -> bytes:
    """A smaller copy for the MCP response.

    One screenshot, two encodes (`adr/0006`). Returns the original bytes
    unchanged when it is already small enough, rather than re-encoding — a
    round trip through PIL at the same size is lossless in content and still
    changes the bytes, which would make two identical renders differ.
    """
    with Image.open(io.BytesIO(png)) as image:
        if max(image.size) <= longest_edge:
            return png

        ratio = longest_edge / max(image.size)
        size = (max(1, round(image.width * ratio)), max(1, round(image.height * ratio)))
        # LANCZOS: a map is full of thin lines and small text, and a cheaper
        # filter turns a 1px contour into a grey smear.
        resized = image.resize(size, Image.Resampling.LANCZOS)

        buffer = io.BytesIO()
        resized.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()


__all__ = [
    "PREVIEW_LONGEST_EDGE",
    "RENDER_TIMEOUT_MS",
    "SIZE_PRESETS",
    "RenderOutput",
    "RenderSpec",
    "downscale_png",
    "render",
]
