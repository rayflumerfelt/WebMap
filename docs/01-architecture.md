# 01 — Architecture

## 1. Service topology

```
   ┌───────────────┐
   │  Browser      │───────────────────┐
   │  React SPA    │                   │
   └───────────────┘                   │
                        ┌──────────────▼───────────────┐
                        │  webmap-api    (FastAPI)     │
   Claude ──MCP HTTP───▶│  webmap-mcp    (FastMCP)     │
   (static token)       │  Both mount on same ASGI app │
                        └──┬────────┬────────┬─────────┘
                           │        │        │
              ┌────────────▼──┐  ┌──▼─────┐  └──────────┐
              │  PostgreSQL   │  │ Redis  │             │
              │  (control     │  │ (arq)  │             │
              │   plane only) │  │        │             │
              └───────────────┘  └──┬─────┘             │
                                    │                   │
              ┌─────────────────────┼───────────────────┼──────────┐
              │                     │                   │          │
        ┌─────▼─────┐        ┌──────▼────┐      ┌───────▼──────┐   │
        │ TiTiler   │        │ webmap-   │      │ webmap-      │   │
        │ (COG)     │        │ worker    │      │ render       │   │
        │           │        │ (arq)     │      │ (Playwright) │   │
        └─────┬─────┘        └─────┬─────┘      └──────────────┘   │
              │            DuckDB in-process                       │
              │            (webmap_geo)                            │
              │                    │                               │
         ┌────▼────────────────────▼───────────────────────────────▼┐
         │  Object storage (S3-compatible / MinIO)                  │
         │  GeoParquet features, COG grids, renders, uploads        │
         └──────────────────────────────────────────────────────────┘

MVT is generated in-process from DuckDB, by the API. There is no tile service.
```

## 2. Services

### 2.1 `webmap-api` — FastAPI

The control plane. Owns the database, registers datasets, manages styles and sessions,
enqueues jobs, and proxies tile requests. Stateless.

Does *not* do heavy computation. Any operation that can exceed 2 seconds is enqueued.

### 2.2 `webmap-mcp` — FastMCP

Mounted on the same ASGI application as `webmap-api`, at `/mcp`. Shares the database session
factory and service modules. It is a *presentation layer over the same services the REST API
uses* — never a parallel implementation.

Transport: Streamable HTTP, stateless JSON. Not stdio (this is a remote server), not SSE
(deprecated).

Rationale for co-locating rather than a separate deployable: the MCP server needs the same
domain services, the same validation, and the same error vocabulary. Splitting them means
maintaining two implementations of every operation and guaranteeing they drift.

### 2.3 `webmap-worker` — arq

Geoprocessing. Gridding, kriging, contouring, aggregation, dataset ingestion and sync.
CPU-bound, memory-hungry. Scale independently of the API.

Depends on `webmap-geo`, the pure-computation package (see §3.2).

### 2.4 `webmap-render` — Playwright

Headless Chromium running real MapLibre GL JS. Produces PNGs from Style JSON.

Kept as a separate service because the container is ~2 GB and its scaling profile is unlike
anything else. See `06-rendering.md`.

### 2.5 `titiler` — raster tiles

Off-the-shelf. Serves COGs with dynamic colormap application. Changing a color ramp is a URL
parameter change, not a regrid — that is what makes palette editing feel instant. Behind the
API's auth proxy, never exposed directly.

Vector tiles have no equivalent service. MVT is generated in-process from DuckDB over the
dataset's GeoParquet object (`06-rendering.md` §7), which keeps the "dynamic, no build step"
property that matters for edited layers without a second process reading the data plane.

## 3. Repository layout

Monorepo. pnpm workspaces + Turborepo for JS, uv workspaces for Python.

```
webmap/
├── CLAUDE.md
├── docs/                          # These specification files
├── apps/
│   ├── web/                       # React SPA (Vite)
│   ├── api/                       # FastAPI + FastMCP
│   ├── worker/                    # arq worker
│   └── render/                    # Playwright render service
├── packages/                      # Reusable JS
│   ├── map/                       # @webmap/map — the map component
│   ├── ui/                        # @webmap/ui — ramp editor, style editor
│   └── style-model/               # @webmap/style-model — TS types + compilers
├── python/                        # Reusable Python
│   ├── webmap_geo/                # Interpolation, contouring, aggregation, data plane
│   ├── webmap_io/                 # Format readers/writers, connectors
│   └── webmap_core/               # Models, services, style compilation
├── infra/
│   ├── docker/
│   ├── migrations/                # Alembic
│   └── compose.yaml
└── tests/
    ├── e2e/                       # Playwright
    └── visual/                    # Render regression goldens
```

### 3.1 Package boundary rules

These are enforced, not advisory. See `CLAUDE.md` for the lint configuration.

- `packages/map` **must not** import from `apps/web`. If it needs something from the app, the
  app passes it in as a prop. Violating this is how reusability dies.
- `python/webmap_geo` **must not** import from `webmap_core` or any web framework. It takes
  NumPy arrays and Shapely geometries in, returns NumPy arrays and Shapely geometries out. No
  database, no HTTP, no logging config. This is what makes it testable and separately
  versionable.
- `apps/api` **must not** contain geoprocessing algorithms. It orchestrates.

### 3.2 Why `webmap_geo` is isolated

It is the highest-value and highest-risk code in the project. Isolating it means:

- It can be tested against reference outputs from Surfer without spinning up a database.
- Its heavy dependencies (`scipy`, `gstools`, `triangle`, `pyamg`, `duckdb`) do not bloat the
  API container.
- It can be versioned and pinned independently, so a change to the kriging neighborhood search
  is a deliberate version bump rather than an accidental deploy.

## 4. Technology decisions

Each entry states the decision, the rationale, and what would change our mind. Record
subsequent changes as ADRs in `docs/adr/`.

### 4.1 MapLibre GL JS + Playwright for rendering

**Decision.** Interactive maps use MapLibre GL JS in the browser. Server-side images are
produced by Playwright driving headless Chromium running the *same* MapLibre GL JS.

**Rationale.** The alternative, MapLibre Native (`mbgl-render`), is a separate C++
implementation of the style spec. It is lighter and faster, but it does not agree with GL JS
in all cases, and the likeliest place it diverges is symbol collision and label placement.
Contour labels are core to this product. A user comparing "the map Claude showed me" against
"the map I just opened" would see differences we could not explain. Playwright makes parity
exact by construction.

Playwright also gives us HTML overlays for free — `page.screenshot()` captures the rendered
page, not just the WebGL canvas, so legends and scale bars are the app's own React components
rendered in the shell. One legend implementation, guaranteed identical in both contexts.

And `page.route()` gives us request interception, which is our SSRF control. MapLibre Native
has no equivalent hook.

**Cost.** ~2 GB container, ~1–2 s per render with a warm browser, SwiftShader software
rasterization.

**Would change our mind.** If render throughput becomes a bottleneck (hundreds per minute) or
the container size becomes a real operational constraint, revisit `mbgl-render` behind the
same `render_map()` interface. The visual regression harness (`tests/visual/`) exists partly to
make that evaluation cheap.

### 4.2 MapLibre Style JSON as single source of truth

**Decision.** Appearance is expressed exclusively as MapLibre Style JSON. The interactive map
and the render service consume byte-identical documents.

**Rationale.** The alternative — an internal style model translated separately for each
renderer — guarantees drift. There must be exactly one representation of "what this map looks
like," and it must be the one the renderer actually consumes.

**Consequence.** Higher-level style concepts (graduated symbology, classification schemes)
are *compilers* that emit Style JSON, not parallel representations. See `08-styling-palettes.md`.

### 4.3 Postgres for control, DuckDB and GeoParquet for data

**Decision.** Two planes, two engines. See `adr/0002-duckdb-data-plane.md`.

- **Control plane — PostgreSQL 16, no PostGIS.** Dataset registry, projects, sessions,
  palettes, style templates, preferences, jobs, lineage. Small transactional rows. Nothing
  here is spatial except `dataset.bbox_4326`, which is four floats.
- **Data plane — DuckDB over GeoParquet and COG on object storage.** Feature geometry,
  gridded values, tile generation, and the aggregation catalog. DuckDB runs in-process
  inside `webmap_geo`; it is a library, not a service.

**Rationale.** DuckDB's spatial extension covers what PostGIS was carrying here — verified
against 1.5.5: `ST_AsMVT`, `ST_AsMVTGeom`, `ST_TileEnvelope`, `ST_Transform`, RTREE indexes,
and every function in the `05-geoprocessing.md` §7 catalog. The one capability it has no
answer for is row-level security, and `adr/0001` removed the requirement for it.

Running the data plane in-process is also what makes `adr/0004`'s rule — all geometry
operations belong to `webmap_geo` — structural rather than aspirational. There is no SQL
path for a geometry operation to escape through.

**Why Postgres and not SQLite** for the control plane, given how small it is: Alembic against
SQLite needs batch mode for nearly every `ALTER TABLE`. One small container is cheaper than
that friction against Phase 0's "migrations apply and roll back cleanly" criterion.

**Would change our mind.** Concurrent multi-writer editing becoming a requirement. DuckDB is
a single-writer engine, and that would reopen `adr/0005` as well.

### 4.4 arq for job queuing

**Decision.** `arq` over Redis.

**Rationale.** Native asyncio, which matches FastAPI. Dramatically simpler than Celery for
what we need: enqueue, poll status, report progress, cancel. Celery's feature surface
(routing, chords, complex canvas) is not needed and its operational weight is real.

**Would change our mind.** If we need scheduled jobs with complex dependency graphs, or
multi-language workers. Neither is on the roadmap.

### 4.5 Constrained triangulation for fault-aware interpolation

**Decision.** Fault-constrained interpolation is built on a constrained Delaunay triangulation
with fault segments as constrained edges, followed by discrete smooth interpolation on the
mesh.

**Rationale.** No Python library supports barriers — GSTools, PyKrige, verde, and SciPy all
assume Euclidean distance in an unobstructed plane. We must build it. Of the possible
approaches, constrained triangulation is the one that generalizes: faults become
discontinuities in mesh connectivity, so every interpolator built on the mesh honors them
without special-casing. This is broadly how GOCAD/SKUA works.

Briggs (1974) finite-difference minimum curvature with fault-aware stencils is retained as a
fast path for the common case.

Detail in `05-geoprocessing.md`.

### 4.6 Single owner, no authorization model

**Decision.** Every object carries one `owner_user_id`. There is no visibility scope, no
grant model, no team, and no row-level security.

**Rationale.** Tenants here are business units inside one company. Cross-BU sharing is a
legitimate and frequent need — one asset team's fault interpretation is exactly what another
team wants. Hard partitioning would mean fighting our own data model within months.

RLS is retained, but the policy is written against the grant model rather than a partition
column.

### 4.7 Terra Draw for editing

**Decision.** Terra Draw, not mapbox-gl-draw or its forks.

**Rationale.** Actively maintained, MapLibre-native, adapter architecture, better-designed
mode system. The mapbox-gl-draw forks carry Mapbox-era assumptions and inconsistent MapLibre
support.

## 5. Request flows

### 5.1 Claude renders a map

```
Claude          webmap-mcp        webmap-api        render          storage
  │                 │                 │                │               │
  ├─render_map()───▶│                 │                │               │
  │                 ├─build style────▶│                │               │
  │                 ├─render (sync, direct call)──────▶│               │
  │                 │                 │                ├─fetch tiles   │
  │                 │                 │                ├─screenshot    │
  │                 │                 │                ├─put PNG──────▶│
  │                 │◀── render_id + image ────────────┤               │
  │◀─image + metadata┤                 │                │               │
```

**Rendering is synchronous.** At 1–2 s with a warm browser it fits inside an MCP tool
timeout, and making Claude poll for an image it will display immediately adds a turn for
nothing. The job queue is for gridding, not rendering — see `10-jobs-async.md` §1 and
`06-rendering.md` §11.

Renders are *persisted artifacts with IDs*, not transient bytes. Claude may reference the same
map on several slides; re-rendering a kriged surface is not free.

### 5.2 Claude opens an editing session

```
Claude ──webmap_open_session()──▶ webmap-mcp ──create session──▶ DB
       ◀──── https://webmap.corp/s/{id} ────────────────────────┘
```

The user clicks, authenticates via SSO in the browser, and the SPA loads the session's
datasets, style, and extent. Session ID is the shared vocabulary between Claude and the app.

### 5.3 Geologist grids a surface

```
POST /api/v1/grids  ──▶ validate params ──▶ enqueue ──▶ 202 + job_id
                                                │
GET /api/v1/jobs/{id} ◀── poll (or WS) ────────┤
                                                ├─ triangulate w/ fault constraints
                                                ├─ interpolate
                                                ├─ write COG ──▶ storage
                                                └─ register dataset + provenance
```

## 6. Environments

| Env | Purpose | Data |
|---|---|---|
| `local` | Docker Compose, all services | Seeded synthetic + `tests/fixtures/` |
| `work` | The one running instance | Real |

Two, not four. A shared `dev` and a `staging` existed to coordinate a team and to rehearse
deploys; there is no team and the deploy is `docker compose up`.

Use **separate MCP bearer tokens** per environment anyway, and never point a Claude connector
at `work` while developing. The reason is unchanged even without multi-tenancy: a tool call
that deletes a dataset does not care which environment it landed in.

## 7. Observability

- **Structured logging.** `structlog`, JSON to stdout. Every log line carries `request_id`,
  `channel` (`web` / `claude`), and where applicable `job_id` / `dataset_id`.
- **Tracing.** OpenTelemetry. The critical trace is MCP tool call → API → worker → render,
  which crosses three services and is otherwise impossible to debug.
- **Metrics.** Prometheus. Watch: render p95 latency, job queue depth, job failure rate by
  kind, tile request rate, browser pool saturation.
- **Provenance is not observability.** Lineage records live in the database as first-class
  domain data (see `02-data-model.md`), not in logs.
