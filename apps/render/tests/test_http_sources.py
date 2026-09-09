"""Rendering from a real HTTP source. `03-auth-security.md` §7.2.

**The gap that let a real bug through.** Every test in `test_render.py` uses a
`data:` source, which never touches the network — so the route handler's
allow/continue path was exercised and its *fetch* path was not. A render
against the live API then failed every tile with "Failed to fetch (0)" and drew
a blank map.

The cause: the shell is loaded from `file://`, so its origin is `null` and every
request to the API is cross-origin. Attaching an Authorization header to a
continued request makes it preflighted; the API answers no OPTIONS; the browser
blocks it. The handler now fetches the resource itself and fulfils the route,
which sidesteps CORS entirely and keeps the credential out of anything the page
could observe.

So this module serves real HTTP on localhost and asserts a render comes back
complete. It is slow and it is worth it: the failure it guards is invisible to
every faster test.
"""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
import pytest_asyncio

from webmap_render.pool import BrowserPool
from webmap_render.service import RenderSpec, render

pytestmark = [pytest.mark.asyncio(loop_scope="module"), pytest.mark.slow]

SHELL = Path(__file__).resolve().parents[1] / "shell" / "index.html"

MIDLAND = (-103.0, 31.0, -101.5, 32.5)

FEATURES = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"name": "A"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [-102.3, 31.8],
                        [-101.8, 31.8],
                        [-101.8, 32.2],
                        [-102.3, 32.2],
                        [-102.3, 31.8],
                    ]
                ],
            },
        }
    ],
}


class _Handler(http.server.BaseHTTPRequestHandler):
    """Serves the GeoJSON, and **records what it was asked for**.

    The Authorization header is the assertion that matters: the guard must
    forward the caller's token to an allowlisted host, or every real tile
    request would be anonymous and the API would refuse it.
    """

    # A class attribute on purpose: the server constructs a handler per
    # request, so anything recorded on an instance is gone before the test
    # can read it.
    seen: ClassVar[list[dict[str, str]]] = []

    def do_GET(self) -> None:
        type(self).seen.append(
            {"path": self.path, "authorization": self.headers.get("Authorization", "")}
        )

        if self.path == "/redirect-to-evil":
            # An allowlisted host answering 302 to somewhere it should not.
            # This is the SSRF a validator cannot see, because the URL it
            # validated is this one.
            self.send_response(302)
            self.send_header("Location", "http://metadata.example.com/secrets.json")
            self.end_headers()
            return

        body = json.dumps(FEATURES).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        """Silence. The default writes to stderr and buries the test output."""


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    _Handler.seen = []
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        # `localhost`, not `127.0.0.1`: a literal address in a blocked network
        # is refused by `_is_allowed` even when allowlisted, which is the
        # correct behaviour and would make this test assert the wrong thing.
        yield f"http://localhost:{port}"
    finally:
        httpd.shutdown()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def pool() -> AsyncIterator[BrowserPool]:
    for asset in ("maplibre-gl.js", "overlay.js"):
        if not (SHELL.parent / asset).is_file():
            pytest.skip(f"The render shell is not built ({asset}). Run: make render-shell")

    pool = BrowserPool(max_concurrent=1)
    try:
        await pool.start()
    except Exception as exc:
        pytest.skip(f"No Chromium for Playwright ({type(exc).__name__}).")
    yield pool
    await pool.stop()


def style_for(base: str) -> dict[str, Any]:
    return {
        "version": 8,
        "sources": {"data": {"type": "geojson", "data": f"{base}/features.geojson"}},
        "layers": [
            {"id": "bg", "type": "background", "paint": {"background-color": "#f5f7f9"}},
            {
                "id": "fill",
                "type": "fill",
                "source": "data",
                "paint": {"fill-color": "#2b93b3"},
            },
        ],
    }


async def test_a_render_from_an_http_source_completes(pool: BrowserPool, server: str) -> None:
    """**The regression.** Before the route handler fetched resources itself,
    this failed every request and drew a blank map — while every `data:`-based
    test passed."""
    output = await render(
        pool,
        RenderSpec(style=style_for(server), size_preset="thumbnail", bounds=MIDLAND),
        "test-token",
        allowed_hosts=frozenset({"localhost"}),
        shell_url=SHELL.as_uri(),
    )

    assert output.failed_requests == [], "the HTTP source was not fetched successfully"
    assert output.is_complete


async def test_the_source_actually_drew(pool: BrowserPool, server: str) -> None:
    """A complete render of nothing would satisfy the test above."""
    import io

    from PIL import Image

    output = await render(
        pool,
        RenderSpec(style=style_for(server), size_preset="thumbnail", bounds=MIDLAND),
        "test-token",
        allowed_hosts=frozenset({"localhost"}),
        shell_url=SHELL.as_uri(),
    )

    image = Image.open(io.BytesIO(output.image)).convert("RGB")
    colours = {image.getpixel((x, y)) for x in range(0, 640, 4) for y in range(0, 360, 4)}
    assert len(colours) > 1, "the polygon from the HTTP source is not in the image"


async def test_the_caller_token_reaches_the_allowlisted_host(
    pool: BrowserPool, server: str
) -> None:
    """`03` §7.2: the token is injected in the route handler.

    A tile request that arrived anonymous would be refused by the real API, and
    the render would come back as a map with holes — which looks like sparse
    data.
    """
    _Handler.seen = []

    await render(
        pool,
        RenderSpec(style=style_for(server), size_preset="thumbnail", bounds=MIDLAND),
        "a-specific-token",
        allowed_hosts=frozenset({"localhost"}),
        shell_url=SHELL.as_uri(),
    )

    assert _Handler.seen, "the source was never requested"
    assert all(entry["authorization"] == "Bearer a-specific-token" for entry in _Handler.seen)


async def test_a_redirect_to_a_host_outside_the_allowlist_is_blocked(
    pool: BrowserPool, server: str
) -> None:
    """**The case a validator structurally cannot catch.** `03` §7.2.

    The style is clean: it names `localhost`, which the allowlist permits, so
    validation passes. The server then answers with a 302 to somewhere else.
    Only the route handler sees that second host — and only because the fetch
    does not follow redirects itself, which would resolve the target without
    this handler ever being asked about it.
    """
    _Handler.seen = []
    style = style_for(server)
    style["sources"]["data"]["data"] = f"{server}/redirect-to-evil"

    output = await render(
        pool,
        RenderSpec(style=style, size_preset="thumbnail", bounds=MIDLAND),
        "test-token",
        allowed_hosts=frozenset({"localhost"}),
        shell_url=SHELL.as_uri(),
    )

    assert any(
        f.get("reason") == "blocked_host" and "metadata.example.com" in str(f.get("url"))
        for f in output.failed_requests
    ), f"the redirect target was not blocked: {output.failed_requests}"


async def test_a_style_naming_a_host_outside_the_allowlist_never_reaches_a_browser(
    pool: BrowserPool, server: str
) -> None:
    """Layer one, checked from this side too: refused before dispatch, so the
    server records nothing at all."""
    from webmap_render.security import StyleRejected

    _Handler.seen = []

    with pytest.raises(StyleRejected, match="localhost"):
        await render(
            pool,
            RenderSpec(style=style_for(server), size_preset="thumbnail", bounds=MIDLAND),
            "test-token",
            allowed_hosts=frozenset({"tiles.webmap.internal"}),
            shell_url=SHELL.as_uri(),
        )

    assert _Handler.seen == [], "a refused style still reached the network"
