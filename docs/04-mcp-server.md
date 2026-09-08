# 04 — MCP Server

Server name: `strata_mcp` (Python convention `{service}_mcp`).
Transport: **Streamable HTTP, stateless JSON**, mounted at `/mcp` on the `strata-api` ASGI app.
Framework: FastMCP (Python SDK).

---

## 1. Design principles

**Tool names carry the service prefix.** `strata_list_datasets`, not `list_datasets`. This
server will run alongside others; generic names collide and confuse tool selection.

**The MCP layer is a presentation layer.** It calls the same service functions as the REST
API. It never contains business logic, never talks to the database directly, never
re-implements a permission check. If you find yourself writing domain logic in a tool handler,
it belongs in `strata_core.services`.

**Responses are shaped for a reader with limited context.** List responses are compact and
paginated. Detail responses are full. Never return a 5 MB GeoJSON blob into a conversation.

**Errors instruct.** An error tells Claude what went wrong *and what to do next*. "Dataset not
found" is a dead end. "No dataset named 'wolfcamp porosity'; the closest matches are X, Y, Z —
use `strata_search_datasets` to look more broadly" lets the conversation continue.

**Metadata over pixels.** Claude cannot write a defensible slide caption from a PNG. Every
render returns the interpolation method, its parameters, value range, units, CRS, and vintage.

---

## 2. Tool surface

Twenty tools in six groups.

| Group | Tools |
|---|---|
| Discovery | `strata_list_projects`, `strata_list_datasets`, `strata_search_datasets`, `strata_describe_dataset` |
| Analysis | `strata_interpolate`, `strata_contour`, `strata_aggregate`, `strata_fit_variogram` |
| Rendering | `strata_render_map`, `strata_get_render`, `strata_suggest_maps` |
| Sessions | `strata_open_session`, `strata_get_session`, `strata_update_session` |
| Styling | `strata_list_palettes`, `strata_list_style_templates` |
| Jobs & admin | `strata_get_job`, `strata_cancel_job`, `strata_export_dataset`, `strata_delete_dataset` |

### 2.1 Annotations

| Tool | readOnly | destructive | idempotent | openWorld |
|---|---|---|---|---|
| `strata_list_*`, `strata_search_*`, `strata_describe_*`, `strata_get_*` | ✓ | ✗ | ✓ | ✗ |
| `strata_suggest_maps`, `strata_fit_variogram` | ✓ | ✗ | ✓ | ✗ |
| `strata_interpolate`, `strata_contour`, `strata_aggregate` | ✗ | ✗ | ✗ | ✗ |
| `strata_render_map`, `strata_open_session` | ✗ | ✗ | ✗ | ✗ |
| `strata_update_session`, `strata_export_dataset` | ✗ | ✗ | ✓ | ✗ |
| `strata_delete_dataset` | ✗ | **✓** | ✓ | ✗ |

---

## 3. Server skeleton

```python
# apps/api/mcp/server.py

from typing import Annotated, Literal
from uuid import UUID

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from strata_core import services
from strata_core.permissions import Principal

mcp = FastMCP(
    name="strata",
    instructions=(
        "Strata is a geospatial mapping system for subsurface geology. Use it to "
        "find spatial datasets, interpolate scattered point data into gridded "
        "surfaces (honoring geological faults), derive contours, and render maps "
        "for presentations.\n\n"
        "Typical flow: find a dataset with strata_search_datasets, inspect it with "
        "strata_describe_dataset, grid it with strata_interpolate, then render with "
        "strata_render_map. Renders return structured metadata — use it to write "
        "accurate figure captions rather than describing the image.\n\n"
        "When the user wants to edit data rather than view it, use "
        "strata_open_session and give them the link."
    ),
)


def principal_from_context(ctx) -> Principal:
    """Extract the authenticated user from the validated bearer token.

    There is no fallback and no service account. If this raises, the request
    is rejected. See 03-auth-security.md §5.
    """
    claims = ctx.request_context.auth  # populated by the token middleware
    return Principal(
        user_id=UUID(claims["strata_user_id"]),
        team_ids=frozenset(UUID(t) for t in claims["strata_team_ids"]),
        channel="claude",
    )
```

---

## 4. Discovery tools

### 4.1 `strata_list_datasets`

```python
@mcp.tool(
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
async def strata_list_datasets(
    ctx,
    project_id: Annotated[UUID | None, Field(
        None, description="Restrict to one project. Omit to list across all "
                          "projects the user can access.")] = None,
    kind: Annotated[Literal["vector", "grid", "pointset", "fault_network"] | None,
        Field(None, description="Filter by dataset kind.")] = None,
    limit: Annotated[int, Field(25, ge=1, le=100)] = 25,
    offset: Annotated[int, Field(0, ge=0)] = 0,
    response_format: Literal["markdown", "json"] = "markdown",
) -> str:
    """List spatial datasets the user can access.

    Returns compact summaries. Call strata_describe_dataset for full detail
    including attribute schema and value ranges.
    """
    p = principal_from_context(ctx)
    page = await services.datasets.list_(
        p, project_id=project_id, kind=kind, limit=limit, offset=offset
    )
    return format_page(page, response_format)
```

Markdown response shape — dense, scannable, IDs present but not dominant:

```markdown
**12 datasets** (showing 1–12)

| Name | Kind | Features | Updated | ID |
|---|---|---|---|---|
| Wolfcamp A Porosity Picks | pointset | 1,847 | 2026-07-31 | `9f3a…c21b` |
| Midland Basin Faults | fault_network | 23 | 2026-06-12 | `4e8d…7a02` |
| Wolfcamp A Structure | grid | 812×640 | 2026-08-14 | `bb17…9e44` |
```

JSON response follows the pagination contract:

```json
{
  "total": 12, "count": 12, "offset": 0, "has_more": false, "next_offset": null,
  "items": [ /* DatasetSummary */ ]
}
```

### 4.2 `strata_search_datasets`

```python
@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
async def strata_search_datasets(
    ctx,
    query: Annotated[str, Field(
        description="Free text matched against dataset name and description. "
                    "Trigram-based, so partial and misspelled terms work.")],
    bbox: Annotated[list[float] | None, Field(
        None, min_length=4, max_length=4,
        description="Optional spatial filter [west, south, east, north] in "
                    "EPSG:4326. Returns datasets intersecting this extent.")] = None,
    kind: Annotated[str | None, Field(None)] = None,
    limit: Annotated[int, Field(20, ge=1, le=50)] = 20,
) -> str:
    """Search datasets by name, description, and optionally spatial extent.

    Use this when the user refers to data by an informal name — 'the Wolfcamp
    porosity data', 'our fault picks' — rather than by ID.
    """
```

This is the tool that resolves "*this* data" into an ID. It is the most-called tool in the
server; make it fast and make it forgiving.

### 4.3 `strata_describe_dataset`

```python
@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
async def strata_describe_dataset(
    ctx,
    dataset_id: UUID,
    include_sample: Annotated[bool, Field(
        False, description="Include up to 5 sample feature attribute rows. "
                           "Useful for confirming field names before "
                           "interpolating. Geometry is never returned.")] = False,
) -> str:
    """Full detail for one dataset: attribute schema, value ranges, CRS,
    extent, provenance, and units.

    Call this before interpolating so you can name the correct value field
    and report accurate units in captions.
    """
```

Response includes the lineage record when present, so Claude can answer "how was this grid
made?" without a second call.

---

## 5. Analysis tools

### 5.1 `strata_interpolate`

The core operation. Long-running, so it returns a job handle.

```python
@mcp.tool()
async def strata_interpolate(
    ctx,
    dataset_id: Annotated[UUID, Field(
        description="Point dataset containing the values to interpolate.")],
    value_field: Annotated[str, Field(
        description="Attribute field holding the value to grid. Must be "
                    "numeric. Check strata_describe_dataset for field names.")],
    output_name: Annotated[str, Field(
        description="Name for the resulting grid dataset, e.g. "
                    "'Wolfcamp A Porosity — Kriged'.")],
    method: Annotated[
        Literal["ordinary_kriging", "universal_kriging", "minimum_curvature",
                "cubic_spline", "idw", "nearest"],
        Field("ordinary_kriging", description=(
            "ordinary_kriging: best general choice; models spatial correlation "
            "and gives uncertainty. universal_kriging: adds a trend surface, "
            "use when data has a regional dip. minimum_curvature: smooth "
            "surface honoring all data points, the Surfer default and what "
            "geologists usually expect for structure maps. cubic_spline: "
            "smooth, fast, can overshoot. idw: fast and robust, produces "
            "bull's-eyes around control points. nearest: diagnostic only."))
    ] = "ordinary_kriging",
    fault_dataset_id: Annotated[UUID | None, Field(
        None, description=(
            "Fault network to honor. Interpolation will not cross features "
            "marked constraint_kind='fault'. Strongly recommended for any "
            "structure or thickness map in a faulted area — omitting it "
            "produces geologically wrong surfaces that still look plausible."))
    ] = None,
    cell_size: Annotated[float | None, Field(
        None, gt=0, description=(
            "Grid cell size in project analysis-CRS units (usually feet or "
            "metres — check strata_list_projects). Omit to derive from data "
            "density."))] = None,
    variogram_model: Annotated[
        Literal["spherical", "exponential", "gaussian", "matern", "linear"] | None,
        Field(None, description="Kriging only. Omit to auto-fit.")] = None,
    variogram_range: Annotated[float | None, Field(None, gt=0)] = None,
    variogram_nugget: Annotated[float | None, Field(None, ge=0)] = None,
    anisotropy_ratio: Annotated[float, Field(1.0, ge=1.0)] = 1.0,
    anisotropy_angle: Annotated[float, Field(0.0, ge=-180, le=180)] = 0.0,
    n_neighbors: Annotated[int, Field(48, ge=8, le=256, description=(
        "Points in the local search neighbourhood. Higher is smoother and "
        "slower. 32–64 is typical."))] = 48,
    detrend: Annotated[Literal["none", "linear", "quadratic"], Field("none")] = "none",
) -> str:
    """Interpolate scattered point data into a gridded surface.

    Returns a job handle immediately — gridding takes seconds to minutes
    depending on point count and method. Poll with strata_get_job.

    On completion the job result contains the new grid's dataset_id, which
    can be passed to strata_render_map or strata_contour.
    """
```

Response:

```markdown
Gridding job queued.

- **Job**: `7c2e…f918`
- **Method**: ordinary kriging, auto-fit variogram
- **Input**: Wolfcamp A Porosity Picks (1,847 points)
- **Faults**: Midland Basin Faults (23 features) — will be honored
- **Estimated**: ~40 s

Poll with `strata_get_job`. Do not call again for the same input.
```

The last line matters. Without it, an agent that polls and sees `queued` may re-submit.

### 5.2 `strata_fit_variogram`

Read-only, fast, and the thing that makes kriging defensible.

```python
@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
async def strata_fit_variogram(
    ctx,
    dataset_id: UUID,
    value_field: str,
    model: Annotated[
        Literal["spherical", "exponential", "gaussian", "matern", "linear"] | None,
        Field(None, description="Omit to fit all models and rank by fit quality.")
    ] = None,
    max_lag: Annotated[float | None, Field(None, gt=0)] = None,
    n_lags: Annotated[int, Field(20, ge=5, le=100)] = 20,
    declustering: Annotated[bool, Field(True)] = True,
) -> str:
    """Estimate and fit an experimental variogram without gridding.

    Use this to sanity-check spatial structure before interpolating, or when
    the user asks why a kriged surface looks the way it does. Fast — runs on
    a subsample. Returns fitted parameters, fit residual, and detected
    anisotropy.
    """
```

Returns nugget, sill, range, anisotropy ratio and azimuth, plus a plain-language read:
"Strong spatial correlation out to ~4,200 ft with marked NE–SW anisotropy (1.8:1). Nugget is
9% of sill, suggesting modest measurement noise."

### 5.3 `strata_contour`

```python
@mcp.tool()
async def strata_contour(
    ctx,
    dataset_id: Annotated[UUID, Field(description="Grid dataset to contour.")],
    output_name: str,
    interval: Annotated[float | None, Field(
        None, gt=0, description="Contour interval in the grid's value units. "
                                "Omit for an automatic interval giving "
                                "roughly 10–20 contours.")] = None,
    levels: Annotated[list[float] | None, Field(
        None, description="Explicit contour values. Overrides interval.")] = None,
    smoothing: Annotated[float, Field(
        0.0, ge=0.0, le=1.0, description=(
            "0 = raw contours following grid cells exactly. Higher values "
            "apply spline smoothing. 0.3–0.5 is typical for presentation "
            "maps. Smoothing can move contours off the underlying grid "
            "values — avoid above 0.5 for technical work."))] = 0.0,
    index_every: Annotated[int, Field(
        5, ge=0, description="Every Nth contour is marked as an index contour "
                             "for heavier styling and labelling. 0 disables.")] = 5,
) -> str:
    """Generate contour lines from a gridded surface.

    Returns a job handle. The output is a vector dataset with elevation
    values as attributes, suitable for rendering or export.
    """
```

### 5.4 `strata_aggregate`

```python
@mcp.tool()
async def strata_aggregate(
    ctx,
    operation: Annotated[
        Literal["buffer", "dissolve", "clip", "intersect", "union", "difference",
                "spatial_join", "summarize_within", "aggregate_points",
                "centroid", "convex_hull", "concave_hull", "voronoi", "hexbin"],
        Field(description=(
            "buffer: expand geometries by a distance. dissolve: merge features "
            "sharing an attribute value. clip: cut layer A by layer B's extent. "
            "summarize_within: statistics of A grouped by containing polygons "
            "of B. aggregate_points: bin points into polygons with counts and "
            "statistics. hexbin: bin points into a hexagonal grid."))],
    input_dataset_id: UUID,
    output_name: str,
    overlay_dataset_id: Annotated[UUID | None, Field(
        None, description="Second layer, required for clip, intersect, union, "
                          "difference, spatial_join, summarize_within.")] = None,
    distance: Annotated[float | None, Field(
        None, description="Buffer distance in analysis-CRS units.")] = None,
    group_by: Annotated[str | None, Field(
        None, description="Attribute field for dissolve or aggregation.")] = None,
    statistics: Annotated[list[str] | None, Field(
        None, description="e.g. ['mean:porosity', 'sum:volume', 'count']")] = None,
) -> str:
    """Run a spatial aggregation or overlay operation.

    All operations run in the project's analysis CRS, so distances and areas
    are correct. Returns a job handle for large inputs, or the result
    directly for small ones.
    """
```

---

## 6. Rendering tools

### 6.1 `strata_render_map`

The tool that produces the image Claude displays.

```python
@mcp.tool()
async def strata_render_map(
    ctx,
    layers: Annotated[list[dict], Field(description=(
        "Ordered list, bottom to top. Each entry: "
        "{dataset_id: UUID, style_template_id?: UUID, palette_id?: UUID, "
        "opacity?: float, label_field?: str}. The user's default basemap "
        "layers are added beneath these automatically."))],
    title: Annotated[str, Field(description=(
        "Map title, rendered in the image. Write something a geologist "
        "would recognise, e.g. 'Wolfcamp A Porosity — Midland Basin'."))],
    bbox: Annotated[list[float] | None, Field(
        None, min_length=4, max_length=4,
        description="Extent [west, south, east, north] in EPSG:4326. Omit to "
                    "fit all layers with a small margin.")] = None,
    size: Annotated[
        Literal["slide_full", "slide_half", "slide_quarter", "square", "thumbnail"],
        Field("slide_full", description=(
            "Output dimensions. slide_full is 16:9 at 2560×1440, sized for a "
            "full-bleed PowerPoint slide. Use consistent sizes across a deck."))
    ] = "slide_full",
    show_legend: Annotated[bool, Field(True, description=(
        "Include a legend. Keep this on for any map going into a "
        "presentation — a colour-filled map without a scale is not "
        "interpretable once separated from this conversation."))] = True,
    show_scale_bar: Annotated[bool, Field(True)] = True,
    show_north_arrow: Annotated[bool, Field(True)] = True,
    transparent_background: Annotated[bool, Field(
        False, description="Render without a background fill, so the map can "
                           "sit on a branded slide template.")] = False,
) -> str:
    """Render a map image from one or more datasets.

    Returns the image plus structured metadata: interpolation method and
    parameters, value range and units, CRS, extent, and data vintage. Use
    that metadata to write figure captions — do not describe the image from
    its pixels, and never state a value range you did not receive here.

    Renders are persisted with an ID. To place the same map on several
    slides, reuse the render_id rather than calling this again.
    """
```

Response — image content block plus structured text:

```markdown
![Wolfcamp A Porosity](strata://render/3f9c...)

**Render** `3f9c…a71e` · 2560×1440

- **Layers**: Wolfcamp A Porosity (grid), Midland Basin Faults, Well Control
- **Values**: 4.1 – 21.8 % porosity
- **Method**: ordinary kriging, exponential variogram (range 4,200 ft,
  nugget 1.1, sill 12.4), anisotropy 1.8:1 at 035°, faults honored
- **Grid**: 250 ft cells, 812 × 640
- **CRS**: NAD83 / Texas Central (EPSG:32038)
- **Vintage**: 2026-07-31
- **Control**: 1,847 points, 23 faults

**Suggested caption**: Wolfcamp A porosity distribution, Midland Basin.
Ordinary kriging of 1,847 well control points with fault constraints;
250 ft grid. Values 4.1–21.8%.

Open interactively: https://strata.corp/s/k3n8fq
```

Every render carries a session link. Review without blocking.

### 6.2 `strata_suggest_maps`

Domain knowledge the app has and Claude does not.

```python
@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
async def strata_suggest_maps(
    ctx,
    dataset_id: Annotated[UUID | None, Field(None)] = None,
    project_id: Annotated[UUID | None, Field(None)] = None,
) -> str:
    """Suggest maps worth making from the available data.

    Given a dataset or project, returns candidate map products with the
    tool calls needed to produce them — structure maps, isopachs, property
    distributions, well control posting. Use this when the user asks for
    something broad like 'make me a summary of this acreage' rather than
    naming a specific map.
    """
```

This is the difference between Claude assembling a deck and Claude assembling a *good* deck.

---

## 7. Session tools

### 7.1 `strata_open_session`

```python
@mcp.tool()
async def strata_open_session(
    ctx,
    layers: Annotated[list[dict], Field(description="Same shape as render_map.")],
    name: Annotated[str | None, Field(None)] = None,
    bbox: Annotated[list[float] | None, Field(None, min_length=4, max_length=4)] = None,
    mode: Annotated[Literal["view", "edit"], Field("view", description=(
        "'edit' opens with editing tools active and the top layer selected "
        "for modification."))] = "view",
) -> str:
    """Create a map session and return a link for the user to open.

    Use this when the user wants to interact with data rather than look at
    an image — editing a shapefile, adjusting a variogram, inspecting
    values. The session persists; they can return to it later.
    """
```

Returns:

```markdown
Session ready: **https://strata.corp/s/k3n8fq**

Loaded: Wolfcamp A Structure (grid), Midland Basin Faults (editable),
Well Control. Editing enabled on the fault layer.

Changes save automatically. Ask me to re-render when you're done and
I'll pick up the current state.
```

### 7.2 `strata_get_session`

Read back session state so Claude can pick up after the user has edited. This closes the loop
— session ID is the shared vocabulary between conversation and application.

---

## 8. Job tools

### 8.1 `strata_get_job`

```python
@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
async def strata_get_job(ctx, job_id: UUID) -> str:
    """Check the status of a long-running operation.

    States: queued, running, succeeded, failed, cancelled. When running,
    includes progress and a description of the current phase. When
    succeeded, includes the output dataset_id.

    Poll no more than once every few seconds. Typical gridding jobs finish
    in 20–90 seconds.
    """
```

Running response:

```markdown
**Job** `7c2e…f918` — running (62%)
Solving on constrained mesh (1.2M cells)
Started 34 s ago · estimated 20 s remaining
```

Failure response is actionable:

```markdown
**Job** `7c2e…f918` — failed

Fault network validation failed: 3 fault polylines intersect without a
shared node, which prevents triangulation.

- 'Big Lake Fault' × 'Unnamed F-12' near (-102.31, 31.88)
- 'Unnamed F-12' × 'Unnamed F-14' near (-102.19, 31.94)
- 'Gardendale Fault' has a dangling end 340 ft from 'Big Lake Fault'

Fix in the map editor: https://strata.corp/s/m4p2xz?tool=fault-cleanup
Or re-run without fault constraints by omitting fault_dataset_id — note
this will interpolate across the faults and may produce a geologically
incorrect surface.
```

Both paths forward are named, and the consequence of the easy one is stated.

---

## 9. Error handling

```python
# apps/api/mcp/errors.py

class StrataToolError(Exception):
    """Base for errors surfaced to Claude. The message IS the interface —
    write it for a reader deciding what to do next."""

    def __init__(self, message: str, suggestions: list[str] | None = None):
        self.message = message
        self.suggestions = suggestions or []

    def render(self) -> str:
        out = f"**Error**: {self.message}"
        if self.suggestions:
            out += "\n\nTry:\n" + "\n".join(f"- {s}" for s in self.suggestions)
        return out


class DatasetNotFound(StrataToolError):
    def __init__(self, ref: str, near_matches: list[tuple[str, str]]):
        msg = f"No dataset matching '{ref}'."
        sugg = []
        if near_matches:
            msg += " Closest matches:"
            sugg = [f"`{did}` — {name}" for name, did in near_matches[:5]]
        sugg.append("Use `strata_search_datasets` with a broader query.")
        super().__init__(msg, sugg)
```

**Rules.**

- Never return a bare stack trace or a database error string.
- Always name at least one next action.
- Validation errors quote the offending value and the constraint.
- Permission errors name the owner to ask (see `03-auth-security.md` §3.2).

---

## 10. Evaluations

Per the MCP builder guidance, maintain at least 10 evaluation questions in
`tests/mcp/evaluations.xml`. Each must be independent, read-only, complex enough to require
several tool calls, realistic, verifiable by string comparison, and stable over time.

```xml
<evaluation>
  <qa_pair>
    <question>Which dataset in the Midland Basin project has the largest
      number of point features, and what is the name of the numeric
      attribute field with the widest value range in it?</question>
    <answer>PHIE</answer>
  </qa_pair>
  <qa_pair>
    <question>The Wolfcamp A Structure grid was produced by interpolation.
      What variogram model was used, and was a fault constraint applied?</question>
    <answer>exponential, yes</answer>
  </qa_pair>
  <!-- 8 more -->
</evaluation>
```

Run these on every change to the tool surface. A tool description edit that degrades tool
selection is invisible without them.

---

## 11. Implementation checklist

- [ ] Streamable HTTP transport, stateless JSON
- [ ] All tools prefixed `strata_`
- [ ] All tools annotated (readOnly / destructive / idempotent / openWorld)
- [ ] Every list tool paginates and returns `has_more` / `next_offset` / `total`
- [ ] Every tool supports `response_format` where it returns data
- [ ] No business logic in tool handlers — all delegate to `strata_core.services`
- [ ] `principal_from_context` used in every handler; no service-account path
- [ ] Destructive tools require `confirm: true`
- [ ] Error messages name a next action
- [ ] Render responses include full metadata and a suggested caption
- [ ] 10+ evaluations passing
- [ ] Tested with MCP Inspector (`npx @modelcontextprotocol/inspector`)
