"""Browser pool. `06-rendering.md` §3.

**Never launch a browser per request** — that is two to four seconds of pure
startup, which is most of the five-second p95 budget spent before any map
exists. Launch once; create a `BrowserContext` per job.

Contexts are cheap (tens of milliseconds) and fully isolated: no cookie, cache
or storage bleed between renders. The isolation is not a nicety. A style that
manages to poison one context must not reach the next render, and the renders
either side of it may belong to different people.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

from webmap_core.logging import get_logger

log = get_logger(__name__)

#: `06-rendering.md` §3. SwiftShader is a CPU rasteriser: there is no GPU in
#: the container and asking for one gets a blank canvas rather than an error.
CHROMIUM_ARGS = [
    "--use-gl=angle",
    "--use-angle=swiftshader",
    # Permits CPU rasterisation. Chromium has renamed this flag more than once
    # — if renders come back blank after a Playwright upgrade, check here
    # first, because a rejected flag is ignored silently.
    "--enable-unsafe-swiftshader",
    # /dev/shm is 64 MB in most container runtimes, which Chromium exhausts on
    # a large viewport and then crashes in a way that looks like a timeout.
    "--disable-dev-shm-usage",
    "--no-sandbox",
    # No audio device exists, and its absence produces a stream of errors that
    # bury the ones worth reading.
    "--mute-audio",
]

#: Two to four per worker (§3). SwiftShader is CPU-bound, so higher
#: concurrency makes contexts contend rather than finishing sooner; each is
#: 100-200 MB besides. Scale horizontally.
DEFAULT_CONCURRENCY = 3

#: Chromium leaks slowly under sustained load — tens of megabytes per hundred
#: renders. Restarting on a count is cruder than watching RSS and does not
#: need a second measurement to be wrong about.
DEFAULT_RECYCLE_AFTER = 300


class BrowserPool:
    """One Chromium process, many contexts."""

    def __init__(
        self,
        max_concurrent: int = DEFAULT_CONCURRENCY,
        recycle_after: int = DEFAULT_RECYCLE_AFTER,
    ) -> None:
        self._sem = asyncio.Semaphore(max_concurrent)
        self._recycle_after = recycle_after
        self._render_count = 0
        self._browser: Browser | None = None
        self._pw: Playwright | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(args=CHROMIUM_ARGS)
        log.info("browser_pool_started", concurrency=self._sem._value)

    async def stop(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None

    @property
    def render_count(self) -> int:
        return self._render_count

    @asynccontextmanager
    async def context(
        self, width: int, height: int, scale: int
    ) -> AsyncIterator[BrowserContext]:
        async with self._sem:
            await self._maybe_recycle()
            browser = self._browser
            if browser is None:
                raise RuntimeError(
                    "The browser pool was not started. Call `await pool.start()` "
                    "during application startup — a render cannot launch one on "
                    "demand without paying the startup cost this pool exists to "
                    "avoid."
                )
            ctx = await browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=scale,
                # The style's own CSP is honoured. Bypassing it would let a
                # crafted style load script the page would otherwise refuse.
                bypass_csp=False,
                # No service workers: they outlive the page and could serve a
                # cached response into the next render.
                service_workers="block",
            )
            try:
                yield ctx
            finally:
                await ctx.close()
                self._render_count += 1

    async def _maybe_recycle(self) -> None:
        if self._render_count < self._recycle_after:
            return
        async with self._lock:
            # Re-checked inside the lock: several renders can queue on it, and
            # without the second check each would restart the browser in turn.
            if self._render_count < self._recycle_after:
                return
            log.info("browser_pool_recycling", after=self._render_count)
            if self._browser is not None:
                await self._browser.close()
            if self._pw is None:
                raise RuntimeError("Cannot recycle a pool that was never started.")
            self._browser = await self._pw.chromium.launch(args=CHROMIUM_ARGS)
            self._render_count = 0


__all__ = ["CHROMIUM_ARGS", "DEFAULT_CONCURRENCY", "DEFAULT_RECYCLE_AFTER", "BrowserPool"]
