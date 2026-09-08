# 01 — Architecture

## 1. Service topology

```
                        ┌──────────────────────────────┐
                        │  Corporate IdP (Entra ID)    │
                        └──────────────┬───────────────┘
                                       │ OIDC
   ┌─────────────────────────┐         │
   │ WORKSTATION             │         │
   │  Browser (React SPA)────┼─────────┤
   │  Claude Code / Desktop  │         │
   │        │ stdio          │         │
   │        ▼                │         │
   │  webmap-mcp (local)─────┼─────────┤  HTTPS + the user's
   │   no DB, no authz       │         │  OS-brokered token
   └─────────────────────────┘         │
                        ┌──────────────▼───────────────┐
                        │  webmap-api    (FastAPI)     │
                        │  enforces authorization      │
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

### 2.2 `webmap-mcp` — FastMCP, local to each workstation

**Runs on the geologist's machine, not the server.** Transport is stdio; Claude Code or Claude
Desktop launches it as a subprocess. See `adr/0008-local-stdio-mcp.md`.

It is a thin HTTP client of `webmap-api`, authenticating as the logged-in Windows user through
the OS credential broker. It holds **no database connection, no service credential, and no
authorization logic** — it runs where the user can modify it, so it is trusted with nothing.

This reverses the original decision to mount it on the API's ASGI app. That decision existed to
avoid duplicating authorization logic, "the single most dangerous thing to duplicate." The
concern is satisfied more strongly rather than abandoned: the local server duplicates no
authorization logic because it has none. What it does duplicate — response formatting and the
markdown shapes in `04-mcp-server.md` — is safe in two places.

What this buys: no OAuth 2.1 authorization server, no Dynamic Client Registration, no upstream
federation. That was four to six weeks and the largest schedule risk in the project.

What it costs: Claude must run on a domain-joined workstation that can reach the internal
network.

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
and every function in the `05-geoprocessing.md` §8 catalog.

**On row-level security**, which DuckDB has no answer for: RLS lives on the control plane,
where the registry rows are, and Postgres provides it there. It never reached feature
*content* under either design — geometry was always going to be in objects, and Postgres
policies do not extend to object storage. `02-data-model.md` §4.1 states plainly that the API
is the sole enforcement point for the data plane, so that this is a decision on the record
rather than a gap someone discovers.

Running the data plane in-process is also what makes `adr/0004`'s rule — all geometry
operations belong to `webmap_geo` — structural rather than aspirational. There is no SQL
path for a geometry operation to escape through.

**Why Postgres and not SQLite** for the control plane, given how small it is: Alembic against
SQLite needs batch mode for nearly every `ALTER TABLE`. One small container is cheaper than
that friction against Phase 0's "migrations apply and roll back cleanly" criterion.

**Would change our mind.** An operation DuckDB spatial cannot express, or a working set large
enough that reading Parquet per request stops being viable.

Not concurrency — that objection was overstated in `adr/0002` and is corrected in its
amendment. DuckDB here is a query engine over immutable Parquet, not a database file: many
concurrent readers are fine, and concurrent writers contend on a single Postgres row rather
than inside DuckDB.

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

### 4.6 Ownership + grants, not tenant partitioning

**Decision.** Every object carries one `owner_user_id`. There is no visibility scope, no
grant model, no team, and no row-level security.

**Rationale.** Tenants here are business units inside one company. Cross-BU sharing is a
legitimate and frequent need — one asset team's fault interpretation is exactly what another
team wants. Hard partitioning would mean fighting our own data model within months.

RLS is retained as a backstop, with the policy written against the grant model rather than a
partition column. Note what it does *not* reach: feature geometry lives in object storage, so
`02-data-model.md` §4.1 states plainly where the single enforcement point is.

The schedule risk this decision used to carry — a federated OAuth server, because `claude.ai`
needed to authenticate to a remote MCP server — is gone for an unrelated reason. See §4.7.

### 4.7 Local stdio MCP instead of an authorization server

**Decision.** The MCP server runs on each geologist's workstation over stdio rather than as a
remote HTTP endpoint. See `adr/0008-local-stdio-mcp.md`.

**Rationale.** A remote MCP server authenticates with OAuth 2.1, and the specification expects
Dynamic Client Registration so a client can register itself. Entra ID, Okta and Ping do not
expose DCR by default and enabling it is frequently blocked by security policy — which is why
the original plan stood up `webmap-auth`, an authorization server federating upstream, and why
`03-auth-security.md` called itself the highest schedule risk in the set.

Every user is at a domain-joined Windows workstation, on the internal network, running Claude
locally. Moving the server there removes the requirement instead of solving it: the process
acquires the user's own token from the OS credential broker and calls `webmap-api` over HTTPS.

**Consequence for trust.** A process on the user's machine cannot enforce anything against
that user. Authorization lives at the API, and only there. This is a strengthening of the
co-location argument in §2.2, not an abandonment of it.

**Would change our mind.** Access needed from outside the domain, or enough users that
per-workstation installation becomes an operational burden. The fallback is not the full
`webmap-auth` build — it is a pre-provisioned confidential OAuth client per environment.

### 4.8 Terra Draw for editing

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
| `local` | Docker Compose on a workstation; development and testing | Seeded synthetic + `tests/fixtures/` |
| `prod` | The internal server everyone uses | Real |

Two, not four. A shared `dev` and a `staging` existed to coordinate a larger team and rehearse
deploys; with a handful of users and `docker compose up` as the deploy, they earn nothing that
`local` does not.

**The MCP server's API base URL is fixed at install time**, not switchable at runtime
(`03-auth-security.md` §4.5). A development install points at localhost; a production install
points at the internal server. A tool call that deletes a dataset does not care which
environment it landed in, and the local server is the one component sitting on a machine where
both configurations are plausible.

## 7. Observability

- **Structured logging.** `structlog`, JSON to stdout. Every log line carries `request_id`,
  `channel` (`web` / `claude`), and where applicable `job_id` / `dataset_id`.
- **Tracing.** OpenTelemetry. The critical trace is MCP tool call → API → worker → render,
  which crosses three services and is otherwise impossible to debug.
- **Metrics.** Prometheus. Watch: render p95 latency, job queue depth, job failure rate by
  kind, tile request rate, browser pool saturation.
- **Provenance is not observability.** Lineage records live in the database as first-class
  domain data (see `02-data-model.md`), not in logs.
