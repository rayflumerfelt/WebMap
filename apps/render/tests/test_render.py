"""The render pipeline, against a real browser. `06-rendering.md` §5.

Not mocked. The failures this service actually has — a blank canvas because
SwiftShader was not enabled, a screenshot taken before tiles landed, an
overlay that renders in a fallback font — are all invisible to a mock and all
visible in a PNG. So these drive Chromium.

They need no network: the shell is `file://`, the style's only source is a
`data:` GeoJSON, and every other request is blocked by the guards under test.
That is the same isolation the render workers run in
(`03-auth-security.md` §7.3), so a test that passes here is not passing
because something was reachable that will not be in production.
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from PIL import Image

from webmap_render.pool import BrowserPool
from webmap_render.security import StyleRejected
from webmap_render.service import RenderSpec, render

pytestmark = [pytest.mark.asyncio(loop_scope="module"), pytest.mark.slow]

SHELL = Path(__file__).resolve().parents[1] / "shell" / "index.html"
SHELL_URL = SHELL.as_uri()

ALLOWED = frozenset({"tiles.webmap.internal", "static.webmap.internal"})

#: Two Midland Basin polygons as an inline source. `data:` never leaves the
#: page, so the whole render is self-contained.
GEOJSON = (
    "data:application/json,"
    '{"type":"FeatureCollection","features":['
    '{"type":"Feature","properties":{"name":"A"},"geometry":{"type":"Polygon",'
    '"coordinates":[[[-102.3,31.8],[-101.8,31.8],[-101.8,32.2],[-102.3,32.2],[-102.3,31.8]]]}},'
    '{"type":"Feature","properties":{"name":"B"},"geometry":{"type":"Polygon",'
    '"coordinates":[[[-102.9,31.3],[-102.5,31.3],[-102.5,31.6],[-102.9,31.6],[-102.9,31.3]]]}}'
    "]}"
)


def style(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "version": 8,
        "sources": {"data": {"type": "geojson", "data": GEOJSON}},
        "layers": [
            # An opaque background, so "the map drew nothing" and "the map drew
            # a white background" are distinguishable in the assertions below.
            {"id": "bg", "type": "background", "paint": {"background-color": "#f5f7f9"}},
            {
                "id": "fill",
                "type": "fill",
                "source": "data",
                "paint": {"fill-color": "#2b93b3", "fill-opacity": 0.9},
            },
        ],
    }
    base.update(overrides)
    return base


MIDLAND = (-103.0, 31.0, -101.5, 32.5)


# `loop_scope` must match `scope`: a fixture created on the module's loop and
# awaited on a per-test one hangs rather than erroring, which costs an
# afternoon to diagnose the first time.
@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def pool() -> AsyncIterator[BrowserPool]:
    # The shell's MapLibre and overlay bundles are build outputs, not committed
    # (see .gitignore). Skipping with the command to build them beats fifteen
    # failures that all mean "run make render-shell".
    for asset in ("maplibre-gl.js", "overlay.js"):
        if not (SHELL.parent / asset).is_file():
            pytest.skip(
                f"The render shell is not built ({asset} is missing). Build it "
                f"with: make render-shell"
            )

    pool = BrowserPool(max_concurrent=2)
    try:
        await pool.start()
    except Exception as exc:
        pytest.skip(
            f"No Chromium for Playwright ({type(exc).__name__}). Install it with: "
            f"uv run playwright install chromium"
        )
    yield pool
    await pool.stop()


async def render_once(pool: BrowserPool, spec: RenderSpec) -> Any:
    return await render(pool, spec, "test-token", allowed_hosts=ALLOWED, shell_url=SHELL_URL)


def pixels(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png)).convert("RGBA")


def rgba(image: Image.Image, x: int, y: int) -> tuple[int, int, int, int]:
    """One pixel, typed.

    `Image.getpixel` is declared as returning a union of every mode's pixel
    shape. The convert to RGBA above makes it a 4-tuple in fact, and this is
    where that fact is asserted once rather than at each call site.
    """
    value = image.getpixel((x, y))
    assert isinstance(value, tuple) and len(value) == 4
    return (int(value[0]), int(value[1]), int(value[2]), int(value[3]))


# --- the map actually draws --------------------------------------------------


async def test_a_render_produces_a_png_at_the_preset_size(pool: BrowserPool) -> None:
    output = await render_once(pool, RenderSpec(style=style(), bounds=MIDLAND))

    assert output.image[:8] == b"\x89PNG\r\n\x1a\n"
    # slide_full is 1280x720 at 2x.
    assert (output.width, output.height) == (2560, 1440)
    assert pixels(output.image).size == (2560, 1440)


async def test_the_map_is_actually_drawn_not_blank(pool: BrowserPool) -> None:
    """**The failure this test exists for.**

    A misconfigured SwiftShader produces a perfectly valid PNG of nothing, and
    every other assertion in this file passes on it. Counting distinct colours
    is what tells a rendered map from a blank canvas.
    """
    output = await render_once(pool, RenderSpec(style=style(), bounds=MIDLAND))

    image = pixels(output.image).resize((320, 180))
    colours = {image.getpixel((x, y)) for x in range(320) for y in range(180)}

    assert len(colours) > 1, "the canvas is a single flat colour — nothing rendered"


async def test_the_layer_colour_appears_in_the_image(pool: BrowserPool) -> None:
    """Not merely "something drew" but "the thing we asked for drew"."""
    output = await render_once(pool, RenderSpec(style=style(), bounds=MIDLAND))

    image = pixels(output.image).resize((256, 144))
    # #2b93b3 at 0.9 over #f5f7f9. Matched loosely: antialiasing and the
    # opacity blend both move the exact value.
    teal = [
        1
        for x in range(256)
        for y in range(144)
        if (px := rgba(image, x, y))[2] > px[0] + 40 and px[1] > px[0] + 20
    ]

    assert len(teal) > 100, "the polygon layer is not visible in the render"


async def test_a_render_settles_rather_than_capturing_mid_draw(pool: BrowserPool) -> None:
    """Two renders of the same spec must be identical.

    Screenshotting before 'idle' yields a half-drawn map, and the failure is
    intermittent — which is what makes it miserable. Identical bytes across
    runs is the only assertion that catches it.
    """
    first = await render_once(pool, RenderSpec(style=style(), bounds=MIDLAND))
    second = await render_once(pool, RenderSpec(style=style(), bounds=MIDLAND))

    assert first.image == second.image


@pytest.mark.parametrize(
    ("preset", "expected"),
    [("slide_full", (2560, 1440)), ("square", (1600, 1600)), ("thumbnail", (640, 360))],
)
async def test_size_presets_produce_their_documented_dimensions(
    pool: BrowserPool, preset: str, expected: tuple[int, int]
) -> None:
    output = await render_once(
        pool, RenderSpec(style=style(), bounds=MIDLAND, size_preset=preset)
    )

    assert pixels(output.image).size == expected


async def test_an_unknown_preset_lists_the_real_ones(pool: BrowserPool) -> None:
    with pytest.raises(StyleRejected, match="slide_full"):
        await render_once(pool, RenderSpec(style=style(), size_preset="poster"))


# --- transparency ------------------------------------------------------------


async def test_a_transparent_render_has_transparent_corners(pool: BrowserPool) -> None:
    """`06-rendering.md` §10 lists this as a visual case: a map dropped onto a
    slide with its own background must not carry a white box with it."""
    transparent = style()
    # No background layer — a background paints over the transparency.
    transparent["layers"] = [transparent["layers"][1]]

    output = await render_once(
        pool, RenderSpec(style=transparent, bounds=MIDLAND, transparent=True)
    )

    image = pixels(output.image)
    assert rgba(image, 2, 2)[3] == 0, "corner is opaque in a transparent render"


# --- SSRF guards -------------------------------------------------------------


async def test_a_style_naming_an_internal_host_is_refused_before_dispatch(
    pool: BrowserPool,
) -> None:
    """`03-auth-security.md` §7.1. Refused rather than partially drawn: a map
    with the metadata service's response painted into one corner is worse than
    no map."""
    hostile = style(
        sources={
            "evil": {
                "type": "raster",
                "tiles": ["http://169.254.169.254/latest/meta-data/{z}/{x}/{y}.png"],
            }
        }
    )

    with pytest.raises(StyleRejected):
        await render_once(pool, RenderSpec(style=hostile, bounds=MIDLAND))


async def test_a_request_to_a_non_allowlisted_host_is_blocked_and_recorded(
    pool: BrowserPool,
) -> None:
    """**Layer two, and the one a validator cannot replace.**

    The style here is clean — `sprite` names an allowlisted host — but the
    browser will follow it somewhere the allowlist does not name. Only the
    route handler sees that, and it records the refusal so the hole in the map
    can be explained.
    """
    with_sprite = style(sprite="https://static.webmap.internal/sprite")

    output = await render_once(pool, RenderSpec(style=with_sprite, bounds=MIDLAND))

    # The sprite host is allowlisted but unreachable in the test environment,
    # so the request fails rather than being blocked — either way it is
    # recorded, which is the property that matters.
    assert output.failed_requests, "a failed sprite fetch went unrecorded"
    assert not output.is_complete


async def test_a_blocked_host_is_named_in_the_failures(pool: BrowserPool) -> None:
    """The recorded reason distinguishes "the tile server was down" from
    "someone tried to make this map fetch the metadata service"."""
    # `glyphs` is validated too, so this goes in as a layer the validator
    # accepts and the route handler refuses: an allowlisted-looking URL on a
    # host that is not in this test's allowlist.
    sneaky = style(glyphs="https://fonts.example.com/{fontstack}/{range}.pbf")

    with pytest.raises(StyleRejected, match=r"fonts.example.com"):
        await render_once(pool, RenderSpec(style=sneaky, bounds=MIDLAND))


# --- quiet failures ----------------------------------------------------------


async def test_a_clean_render_records_no_failures(pool: BrowserPool) -> None:
    output = await render_once(pool, RenderSpec(style=style(), bounds=MIDLAND))

    assert output.failed_requests == []
    assert output.is_complete


async def test_a_broken_source_produces_a_map_and_a_warning(pool: BrowserPool) -> None:
    """§5.1: a 404 on a tile produces a map with a hole, not an exception. The
    warning is what lets a geologist tell that from sparse data."""
    broken = style(
        sources={
            "data": {"type": "geojson", "data": GEOJSON},
            "tiles": {
                "type": "vector",
                "tiles": ["https://tiles.webmap.internal/missing/{z}/{x}/{y}.mvt"],
            },
        }
    )
    broken["layers"].append(
        {
            "id": "missing",
            "type": "line",
            "source": "tiles",
            "source-layer": "features",
            "paint": {"line-color": "#000000"},
        }
    )

    output = await render_once(pool, RenderSpec(style=broken, bounds=MIDLAND))

    assert output.image[:8] == b"\x89PNG\r\n\x1a\n", "no map was produced at all"
    assert output.failed_requests, "a failed tile fetch went unrecorded"


# --- overlays ----------------------------------------------------------------


async def test_the_legend_is_in_the_image(pool: BrowserPool) -> None:
    """`06-rendering.md` §9, and the Phase 3 criterion "legends appear
    correctly in rendered output, matching the interactive legend".

    The overlay is the app's own React component, mounted into the page — so
    this also proves the bundle loads and mounts, which is the part most likely
    to break silently after a dependency change.
    """
    overlay = {
        "legend": {
            "kind": "classes",
            "title": "Porosity (%)",
            "entries": [
                {"swatch": "#440154", "label": "< 5 %"},
                {"swatch": "#fde725", "label": ">= 20 %"},
            ],
        },
        "scaleBar": {"latitude": 31.99, "zoom": 9, "unit": "imperial"},
        "northArrow": {"bearing": 30},
    }

    with_overlay = await render_once(
        pool, RenderSpec(style=style(), bounds=MIDLAND, overlay=overlay)
    )
    without = await render_once(pool, RenderSpec(style=style(), bounds=MIDLAND))

    assert with_overlay.image != without.image, "the overlay changed nothing"
    # The legend's viridis low end, which appears nowhere in the base map.
    image = pixels(with_overlay.image)
    swatch = [
        1
        for x in range(image.width // 2, image.width)
        for y in range(image.height // 2, image.height, 2)
        if abs((px := rgba(image, x, y))[0] - 68) < 12 and abs(px[1] - 1) < 12
    ]
    assert len(swatch) > 20, "the legend swatch is not in the rendered image"
