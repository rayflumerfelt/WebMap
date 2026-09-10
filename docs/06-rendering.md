# 06 — Rendering

Service: `apps/render`. Playwright + headless Chromium running real MapLibre GL JS.

---

## 1. Why this design

Decided in `01-architecture.md` §4.1. Restated because implementers will be tempted to
simplify it:

- **Real GL JS, not MapLibre Native.** Parity with the interactive map is exact by
  construction. Native is a separate implementation and diverges most on symbol collision and
  label placement — precisely where contour labels live.
- **Screenshot the page, not the canvas.** Legends, scale bars, and title blocks are the app's
  own React components rendered in the shell. One implementation, guaranteed identical in both
  contexts. This is the main advantage over `mbgl-render`, which only ever produces a bare map.
- **`page.route` is the SSRF control.** Native has no equivalent hook.

Keep the interface at `render(spec: RenderSpec) -> bytes` so the engine stays swappable.

---

## 2. Container

```dockerfile
# infra/docker/render.Dockerfile
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

# Fonts for HTML overlays (legend, title block). Map labels use glyph PBFs
# served by the API, but overlay text uses system fonts — headless Linux
# ships with almost none, and missing fonts render as boxes with no error.
RUN apt-get update && apt-get install -y --no-install-recommends \
      fonts-inter fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY apps/render /app/render
COPY apps/render/shell /app/shell   # the MapLibre render page

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
CMD ["uv", "run", "python", "-m", "render.main"]
```

~2 GB. Accepted.

---

## 3. Browser pool

Never launch a browser per request — that is 2–4 seconds of pure startup. Launch once, create
a `BrowserContext` per job.

```python
# apps/render/src/webmap_render/pool.py

import asyncio
from contextlib import asynccontextmanager

from playwright.async_api import Browser, BrowserContext, async_playwright

CHROMIUM_ARGS = [
    "--use-gl=angle",
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",   # permits CPU rasterisation; verify flag
                                     # name against current Playwright docs,
                                     # the Chromium team changes headless GL
                                     # behaviour regularly
    "--disable-dev-shm-usage",       # /dev/shm is small in containers
    "--no-sandbox",                  # required in most container runtimes
]


class BrowserPool:
    """One Chromium process, many contexts.

    Contexts are cheap (tens of ms) and fully isolated — no cookie, cache, or
    storage bleed between renders. Isolation still matters: a style that
    manages to poison one context must not affect the next render.
    """

    def __init__(self, max_concurrent: int = 3, recycle_after: int = 300):
        self._sem = asyncio.Semaphore(max_concurrent)
        self._recycle_after = recycle_after
        self._render_count = 0
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(args=CHROMIUM_ARGS)

    @asynccontextmanager
    async def context(self, width: int, height: int, scale: int):
        async with self._sem:
            await self._maybe_recycle()
            ctx = await self._browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=scale,
                # No network access except what page.route allows
                bypass_csp=False,
            )
            try:
                yield ctx
            finally:
                await ctx.close()
                self._render_count += 1

    async def _maybe_recycle(self) -> None:
        """Chromium leaks slowly under sustained load. Restart periodically."""
        if self._render_count < self._recycle_after:
            return
        async with self._lock:
            if self._render_count >= self._recycle_after:
                await self._browser.close()
                self._browser = await self._pw.chromium.launch(args=CHROMIUM_ARGS)
                self._render_count = 0
```

**Concurrency.** Two to four per worker. SwiftShader is CPU-bound; higher concurrency just
makes them contend. Each context is 100–200 MB. Scale horizontally.

---

## 4. The render shell

A minimal local page. Never fetched from the network.

```html
<!-- apps/render/shell/index.html -->
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <link rel="stylesheet" href="./maplibre-gl.css">
  <link rel="stylesheet" href="./overlay.css">
  <style>
    html, body { margin: 0; padding: 0; overflow: hidden; }
    #root { position: relative; width: 100vw; height: 100vh; }
    #map  { position: absolute; inset: 0; }
  </style>
</head>
<body>
  <div id="root">
    <div id="map"></div>
    <div id="overlay"></div>   <!-- legend, scale bar, north arrow, title -->
  </div>
  <script src="./maplibre-gl.js"></script>
  <script src="./overlay.js"></script>   <!-- built from @webmap/ui -->
  <script src="./render.js"></script>
</body>
</html>
```

```javascript
// apps/render/shell/render.js

window.__mapReady = false;
window.__failedRequests = [];

window.renderMap = async function (spec) {
  const map = new maplibregl.Map({
    container: 'map',
    style: spec.style,
    bounds: spec.bounds,
    fitBoundsOptions: { padding: spec.padding ?? 24 },
    interactive: false,
    attributionControl: { compact: false },
    // preserveDrawingBuffer NOT set — we screenshot the page, not the canvas,
    // so retaining the framebuffer would cost performance for nothing.
    fadeDuration: 0,          // no cross-fade; we want the settled state
  });

  map.on('error', (e) => {
    window.__failedRequests.push({
      url: e?.error?.url ?? null,
      message: e?.error?.message ?? String(e),
    });
  });

  // Overlays are the app's own React components, mounted into #overlay.
  // Same code path as the interactive map's legend.
  if (spec.overlay) {
    window.WebMapOverlay.mount(document.getElementById('overlay'), spec.overlay);
  }

  // 'idle' fires once tiles, glyphs, and sprites have all landed and no
  // further rendering is queued. Screenshotting before this yields a
  // half-drawn map — and the failure is intermittent, which makes it
  // miserable to debug. Do not substitute 'load'.
  await new Promise((resolve) => map.once('idle', resolve));

  // Fonts used by the overlay must be settled too.
  await document.fonts.ready;

  window.__mapReady = true;
};
```

---

## 5. Render pipeline

```python
# apps/render/src/webmap_render/service.py

from dataclasses import dataclass
from urllib.parse import urlparse


SIZE_PRESETS = {
    "slide_full":    (1280, 720,  2),   # → 2560x1440
    "slide_half":    (640,  720,  2),   # → 1280x1440
    "slide_quarter": (640,  360,  2),   # → 1280x720
    "square":        (800,  800,  2),   # → 1600x1600
    "thumbnail":     (640,  360,  1),
}


@dataclass(frozen=True)
class RenderSpec:
    style: dict
    bounds: tuple[float, float, float, float]
    size_preset: str
    overlay: dict | None
    transparent: bool = False

# The auth token is NOT on RenderSpec. It is passed separately to
# install_guards and lives only in that closure — putting it on the spec
# would carry it into page JS via page.evaluate below, where the shell has
# no use for it and any script the style loads could read it.


async def render(
    pool: BrowserPool, spec: RenderSpec, auth_token: str
) -> RenderOutput:
    validate_style(spec.style)          # 03-auth-security.md §7.1
    w, h, scale = SIZE_PRESETS[spec.size_preset]

    async with pool.context(w, h, scale) as ctx:
        page = await ctx.new_page()
        failed: list[dict] = []
        await install_guards(page, ALLOWED_HOSTS, auth_token, failed)

        await page.goto("file:///app/shell/index.html")
        # asdict(spec) carries no credential — see the note on RenderSpec.
        await page.evaluate("(s) => window.renderMap(s)", asdict(spec))

        try:
            await page.wait_for_function(
                "window.__mapReady === true", timeout=30_000
            )
        except PlaywrightTimeout:
            # Screenshot anyway. A partial map with a warning is more useful
            # to a geologist than an error string.
            failed.append({"reason": "render_timeout"})

        client_failures = await page.evaluate("window.__failedRequests")
        failed.extend(client_failures)

        png = await page.screenshot(
            type="png",
            omit_background=spec.transparent,
            full_page=False,
        )

    # One screenshot, two encodes. The preview is what reaches Claude in the
    # MCP response; the master is what goes on a slide. See
    # adr/0006-render-image-delivery.md and 04-mcp-server.md §6.1.
    preview = downscale_png(png, longest_edge=1600)

    return RenderOutput(image=png, preview=preview,
                        width=w * scale, height=h * scale,
                        failed_requests=failed)
```

### 5.1 Failures are quiet

The main operational annoyance. A 404 on a tile produces a map with a hole, not an exception.
`failed_requests` is captured and persisted on the `render` row so the geologist can tell
"the data really is sparse there" from "the tile server was down."

Surface it in the MCP response as a warning when non-empty:

```markdown
⚠️ 3 tile requests failed during rendering. The northeast portion of this map
may be incomplete.
```

---

## 6. Style assembly

Styles are **always assembled server-side** from validated layer references. Client-supplied
symbology is accepted; client-supplied source URLs are not (`03-auth-security.md` §7.3).

```python
# python/webmap_core/src/webmap_core/services/style_builder.py

async def build_style(
    actor: Actor,
    layers: list[LayerRef],
    user_prefs: UserPreferences,
    bounds: Bbox,
) -> dict:
    """Assemble a complete MapLibre Style JSON.

    Order, bottom to top:
      1. User's default basemap layers (from preferences)
      2. Requested layers in the given order
      3. Labels

    Every source URL is minted here from internal endpoints. Nothing is
    passed through from the caller.
    """
    style = {
        "version": 8,
        "name": "webmap-render",
        "glyphs": f"{settings.internal_static}/glyphs/{{fontstack}}/{{range}}.pbf",
        "sprite": f"{settings.internal_static}/sprite",
        "sources": {},
        "layers": [],
    }
    for ref in resolve_layer_order(user_prefs, layers):
        dataset = await services.datasets.get(actor, ref.dataset_id)
        add_source_and_layers(style, dataset, ref)
    return style
```

Source construction per dataset kind:

| Kind | Source type | URL |
|---|---|---|
| vector, pointset, fault_network | `vector` (MVT) | `{api}/tiles/{dataset_id}/{z}/{x}/{y}` |
| grid | `raster` | `{titiler}/cog/tiles/{z}/{x}/{y}?url={cog}&colormap={ramp}&rescale={min},{max}` |

**The COG + TiTiler payoff:** changing a color ramp is a URL parameter change, not a regrid.
Palette editing feels instant.

---

## 7. Vector tiles

MVT is generated **in-process** by the API, from the dataset's current GeoParquet object via
DuckDB. No tile service, no build step — which still matters, because layers are edited and a
tile must reflect the current version the moment the version pointer advances.

```python
# python/webmap_geo/src/webmap_geo/tiles.py

TILE_SQL = """
SELECT ST_AsMVT(t, $layer, 4096, 'geom')
FROM (
    SELECT
        id,
        ST_AsMVTGeom(
            ST_Transform(geometry, $storage_srid, 3857),
            ST_TileEnvelope($z, $x, $y),
            4096, 64, true
        ) AS geom,
        props
    FROM read_parquet($parquet_key)
    -- Bbox filter in storage CRS against the back-transformed tile envelope,
    -- so the Parquet row-group statistics can prune. Filtering on a
    -- transformed geometry column would read every row group.
    WHERE bbox.xmin <= $env_xmax AND bbox.xmax >= $env_xmin
      AND bbox.ymin <= $env_ymax AND bbox.ymax >= $env_ymin
) AS t
WHERE t.geom IS NOT NULL
"""
```

Two things carry the performance here, and both were defects in the previous PostGIS design:

- **The filter is on the stored bbox columns, not on a transformed geometry.** GeoParquet
  writes per-row-group bounding boxes; a predicate over them lets DuckDB skip row groups
  without decoding them. Wrapping the geometry in `ST_Transform` inside the predicate — which
  is what the Martin function did — defeats every index and statistic and reads the whole
  layer per tile.
- **The tile envelope is transformed once, into storage CRS**, rather than transforming every
  feature into 3857 before comparing. 3857 → storage is separable and monotonic for the
  projections in scope, so a corner transform is an exact bound *for those* — but this path is
  reached with whatever `storage_srid` a user registered, and the predicate is only correct if
  the bound actually **contains** the region. An oblique or conic projection curves the edges
  outward; four corners under-cover it and features vanish from tiles with nothing logged.
  Densify the edges rather than resting on a property holding for every CRS someone registers.
  Twenty-odd extra point transforms against a cached transformer is not the bottleneck, and
  the failure it prevents is invisible.

Tiles are cached by `(dataset_id, version, z, x, y)`. The version in the key means an edit
invalidates exactly the layer that changed, and nothing else, with no explicit purge.

### 7.1 GeoJSON vs MVT

**500k features cannot go to the browser as GeoJSON.** Switch automatically on feature count:

| Feature count | Source | Rationale |
|---|---|---|
| < 5,000 | GeoJSON | Editable in place, instant style updates, no tile round-trip |
| ≥ 5,000 | MVT | Anything else stalls the main thread |

Actively-edited layers stay GeoJSON regardless while an edit session is open, then flip back.
See `09-editing.md`.

---

## 8. Client-side canvas capture

The secondary path: "capture what I'm looking at right now." Different job from "generate a
map from a description."

```typescript
// packages/map/src/capture.ts

/**
 * Capture the current map view without leaving preserveDrawingBuffer on.
 *
 * WHY THE DANCE: WebGL clears the drawing buffer after paint unless
 * preserveDrawingBuffer is set, and that flag costs real performance on
 * every pan and zoom. Instead we hook a single render frame, read the
 * canvas inside it while the buffer is still valid, and release.
 */
export function captureCanvas(map: maplibregl.Map): Promise<Blob> {
  return new Promise((resolve, reject) => {
    map.once('render', () => {
      try {
        map.getCanvas().toBlob((blob) => {
          blob ? resolve(blob) : reject(new Error('Canvas capture produced no data'));
        }, 'image/png');
      } catch (err) {
        // Canvas is tainted — a cross-origin tile source without CORS headers.
        // Should be impossible with our own tile servers; if it happens,
        // a basemap was added that we do not control.
        reject(new Error(
          'Cannot capture: a map source is missing CORS headers. ' +
          'Check for externally-added basemap layers.'
        ));
      }
    });
    map.triggerRepaint();
  });
}
```

The captured PNG is POSTed to the API, which composites the overlay server-side and creates a
`render` row — so a captured view is a first-class render with the same metadata and the same
ID space.

---

## 9. Legends and overlays

**Mandatory for anything going into a presentation.** A map on a slide is shown to people who
were not in the conversation, in a room where nobody can ask what the colors mean.

Overlay components live in `packages/ui` and are consumed by both the SPA and the render
shell:

- `<ColorBar>` — continuous ramp with min/mid/max labels and units
- `<CategoryLegend>` — discrete swatches with labels
- `<ScaleBar>` — computed from the actual projection at map centre, not from zoom level
- `<NorthArrow>` — omit when bearing is 0 and the user disabled it
- `<TitleBlock>` — title, subtitle, provenance stamp
- `<ProvenanceStamp>` — small, cornered: method, date, grid spacing

The provenance stamp is cheap and means the map defends itself once it is out of our hands. An
auto-assembled deck reaching a partner with maps whose parameters nobody reviewed is a real
exposure; a visible stamp is the mitigation that survives copy-paste.

---

## 10. Visual regression testing

Rendering bugs are visual and will not appear in unit tests.

```
tests/visual/
├── fixtures/            # style JSON + fixed data
├── golden/              # reference PNGs
├── output/              # actual (gitignored)
└── test_visual.py
```

```python
# tests/visual/test_visual.py

import pytest
from PIL import Image, ImageChops
import numpy as np

CASES = [
    "grid_with_contours_labeled",   # the label-placement canary
    "faults_over_grid",
    "dense_point_posting",
    "graduated_polygons",
    "continuous_ramp_legend",
    "transparent_background",
]


@pytest.mark.parametrize("case", CASES)
def test_render_matches_golden(case, render_service):
    actual = render_service.render_fixture(case)
    golden = Image.open(f"tests/visual/golden/{case}.png")

    diff = np.asarray(ImageChops.difference(actual, golden).convert("L"))
    changed = (diff > 8).mean()

    # 0.1% tolerance absorbs antialiasing jitter between Chromium builds
    # without hiding a real layout change.
    assert changed < 0.001, (
        f"{case}: {changed:.3%} of pixels differ. "
        f"Review tests/visual/output/{case}.png against golden."
    )
```

Regenerate goldens deliberately (`make update-goldens`), never automatically, and review the
diff in the PR. This harness is also what makes evaluating `mbgl-render` cheap if we ever
revisit the engine decision.

---

## 11. Performance targets

| Stage | Target |
|---|---|
| Context creation | < 50 ms |
| Page load (local shell) | < 200 ms |
| Style application + tile fetch | 300–1200 ms |
| SwiftShader rasterisation | 300–800 ms |
| Screenshot + encode | < 200 ms |
| **Total, warm browser** | **< 2 s p50, < 5 s p95** |

Comfortably inside MCP tool timeouts, so `webmap_render_map` returns synchronously. Only
gridding uses the job-handle pattern.
