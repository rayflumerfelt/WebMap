# WebMap

Web-based geospatial mapping for subsurface geologists, with an MCP server that lets Claude
drive it conversationally.

The differentiator is **fault-constrained interpolation** — gridding that correctly refuses to
interpolate across a sealing fault, exposed through a conversational interface.

**Status:** Phases 0–3 built, Phase 4 in progress. Current state and
known gaps are tracked in [`docs/12-roadmap.md`](docs/12-roadmap.md) under *Current status*.

---

## Read this first

| If you are | Read |
|---|---|
| Implementing anything | [`CLAUDE.md`](CLAUDE.md), then [`docs/01-architecture.md`](docs/01-architecture.md) and [`docs/02-data-model.md`](docs/02-data-model.md) |
| Orienting | [`docs/00-overview.md`](docs/00-overview.md) |
| Planning | [`docs/12-roadmap.md`](docs/12-roadmap.md) |

`docs/02` and `docs/04` constrain nearly everything downstream. If you find yourself disagreeing
with the entity model, resolve that before writing code rather than working around it.

## Specifications

| File | Contents |
|---|---|
| [`00-overview.md`](docs/00-overview.md) | Scope, users, scale targets, non-goals |
| [`01-architecture.md`](docs/01-architecture.md) | Services, topology, technology decisions with rationale |
| [`02-data-model.md`](docs/02-data-model.md) | Schema DDL, Pydantic models, CRS model, permissions |
| [`03-auth-security.md`](docs/03-auth-security.md) | OIDC, identity propagation to a local MCP server, SSRF controls |
| [`04-mcp-server.md`](docs/04-mcp-server.md) | Complete tool surface with schemas |
| [`05-geoprocessing.md`](docs/05-geoprocessing.md) | Interpolation, fault handling, contouring, aggregation |
| [`06-rendering.md`](docs/06-rendering.md) | Playwright render service, style pipeline, tiles |
| [`07-frontend.md`](docs/07-frontend.md) | Monorepo, map component API, desktop layout, state management |
| [`08-styling-palettes.md`](docs/08-styling-palettes.md) | Style model, property editor, ramp editor |
| [`09-editing.md`](docs/09-editing.md) | Terra Draw, snapping, topology, validation |
| [`10-jobs-async.md`](docs/10-jobs-async.md) | Queue, status protocol, progress, quotas |
| [`11-file-io.md`](docs/11-file-io.md) | Format matrix, connectors, shapefile caveats |
| [`12-roadmap.md`](docs/12-roadmap.md) | Phased milestones with acceptance criteria |
| [`13-kriging.md`](docs/13-kriging.md) | Variography, the kriging family, regression indicator kriging |
| [`adr/`](docs/adr/) | Architecture decision records |

## Architecture in a paragraph

React/Mantine frontend with MapLibre GL JS. FastAPI backend on PostgreSQL — **no PostGIS**;
geometry lives in the data plane as GeoParquet read through DuckDB ([ADR
0002](docs/adr/0002-duckdb-data-plane.md)). Separate worker pools for geoprocessing (`arq`) and
rendering (Playwright + headless Chromium running real MapLibre GL JS). Rasters as
Cloud-Optimized GeoTIFF served through TiTiler; vectors as dynamic MVT. MapLibre Style JSON is
the single source of truth for appearance — the interactive map and the headless renderer
consume byte-identical style documents. An MCP server exposes the whole thing to Claude **over
stdio, running on the user's own workstation** ([ADR
0008](docs/adr/0008-local-stdio-mcp.md)) — which is what removes the need for an OAuth server
rather than solving it.

## Not in scope

PowerPoint generation, 3D visualization, seismic data, petrophysics, reservoir simulation, Esri
format lock-in, real-time collaborative editing, mobile and tablet support, public internet
exposure. See [`docs/00-overview.md`](docs/00-overview.md) §7 before proposing any of them.

## Getting started

Toolchain: Node 20+, pnpm, uv, Docker, and GNU Make.

```bash
make setup      # install all deps, both languages
make dev        # bring up the stack (Postgres, Redis, MinIO, TiTiler, api, worker)
make seed       # load synthetic Midland Basin data in EPSG:2277
make check      # lint + typecheck + test, both languages
```

### Running the application

The stack in `make dev` is the backend. **The web application is not in Compose**
— it runs from Vite, which proxies `/api` and `/auth` to the API on :8000:

```bash
pnpm --filter @webmap/web dev     # http://localhost:5173
```

Open `http://localhost:5173` and it renders the empty shell: panels, toolbar,
status bar, no data. That is correct — the application is
*session-oriented*, and the route that shows a map is `/s/<short_code>`, the
link Claude hands out (`07-frontend.md` §8).

To get one, sign in and create a session. Sign-in is a POST, so it cannot be
reached by typing a URL; from the browser console at :5173:

```js
await fetch('/auth/dev/login?user=ada', { method: 'POST' });
```

That sets the `webmap_session` cookie. `ada`, `grace` and `alan` are the
development roster (`apps/api/src/webmap_api/auth/dev.py`); Ada and Grace are on the team
owning the seeded data and Alan is not, which is how the permission model is
demonstrated rather than asserted.

Then create a session over the three seeded layers and open the link it
returns:

```js
const datasets = await (await fetch('/api/v1/datasets')).json();
const session = await (await fetch('/api/v1/sessions', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    layers: datasets.items.map((d) => ({ dataset_id: d.id })),
    view: { bbox: [-102.91, 31.17, -101.09, 32.30] },   // the seeded extent
    name: 'Walkthrough',
  }),
})).json();
location.href = `/s/${session.short_code}`;
```

The other way in is Claude: `webmap_open_session` returns the same link, which
is the path the system is designed around. See
[`docs/04-mcp-server.md`](docs/04-mcp-server.md) and `webmap-mcp-install` for
registering the MCP server with a Claude client.

**On a network that blocks `extensions.duckdb.org`** — a captive portal will —
the API and worker refuse to start, because they cannot load the DuckDB
extensions that read geometry. Cache them on the host and mount them in:

```bash
make duckdb-extensions
docker compose -f infra/compose.yaml -f infra/compose.offline.yaml up -d
```

**The `api` and `worker` images bake the source in.** There is no bind mount
and no hot reload, so a change to a route, a request model, a service or a
worker task is invisible to a running stack until it is rebuilt:

```bash
docker compose -f infra/compose.yaml build api worker
docker compose -f infra/compose.yaml up -d api worker
```

This matters because the integration tests run against the live container. A
stale image fails them in a way that looks like a code bug: request models are
`extra="forbid"`, so a field the running server has not heard of comes back as
`Invalid request — <field>: Extra inputs are not permitted` rather than being
quietly dropped. That message means the *server* is old. Rebuild before
concluding anything about the field.

`make check` is the gate. It runs ruff, mypy, `import-linter`, eslint, `tsc`,
pytest and vitest — including the package boundary contracts, which are
enforced rather than advisory (`CLAUDE.md` §3.5).

On Windows, `make` is not present by default; install GNU Make, or run the
recipes in the [`Makefile`](Makefile) directly — each is a single line for
that reason. Docker Desktop needs WSL2 for its Linux engine.

Copy [`.env.example`](.env.example) to `.env` for local development. Its
defaults match [`infra/compose.yaml`](infra/compose.yaml), so nothing needs
editing to run the stack.

### What exists so far

Phase-by-phase status, including what is deliberately empty and what is
outstanding, lives in [`docs/12-roadmap.md`](docs/12-roadmap.md). In summary:

| Area | State |
|---|---|
| Monorepo, boundary lint (both languages) | Working; each contract verified against a deliberate violation |
| Schema, RLS policies, ownership trigger | Applied and rolled back cleanly; 16 tables, 24 policies |
| Compose stack, container images | `docker compose up` verified from empty volumes |
| Seed script | 2,000 points, 20 faults, one grid, queryable via DuckDB and TiTiler |
| Identity, permissions, audit | Working offline via the verifier seam ([ADR 0009](docs/adr/0009-offline-identity-seam.md)); OIDC and MSAL paths written but unrun |
| Dataset and project REST, sharing | Working |
| Upload, ingest, vector readers | Working for shapefile, GeoJSON, GeoPackage, CSV/XYZ |
| Tiles, style compilation, sessions | MVT from GeoParquet, TiTiler COG proxy, scoped tokens; TS and Python compilers agree on shared vectors |
| Web application | Layer tree, symbology, attribute table, docked shell, session route and autosave |
| `apps/render` | Playwright pool, SwiftShader, three-layer SSRF defence, render persistence and captions |
| `apps/mcp` | 13 tools over stdio — discovery, analysis, jobs, render, session — plus `webmap-mcp-install` |
| `webmap_geo` | Variograms, ordinary kriging, minimum curvature with fault-aware stencils, contouring, fault validation, constrained triangulation |
| Jobs | arq worker with progress, cooperative cancellation, quotas, idempotency; gridding and contouring end to end with lineage |
| `webmap_geo` (labels, contour bands) | Polygon label anchors and filled contour bands, with areas checked against a closed form |
| `webmap_geo.aggregate`, editing, connectors, export | Empty by design until Phases 5–6 |

Verified against a live stack: 906 Python tests including the full integration
suite, 320 TypeScript, lint and typecheck clean in both languages.

What is **not** demonstrated is as important: `tests/visual/golden/` and
`tests/e2e/` are still empty, and no test carries the `reference` marker, so
every browser-, image- and reference-comparison criterion is unmet. The
roadmap says which.
