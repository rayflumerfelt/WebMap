# WebMap

Web-based geospatial mapping for subsurface geologists, with an MCP server that lets Claude
drive it conversationally.

The differentiator is **fault-constrained interpolation** — gridding that correctly refuses to
interpolate across a sealing fault, exposed through a conversational interface.

**Status:** Phases 0 and 1 complete. Current state and
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
| [`02-data-model.md`](docs/02-data-model.md) | PostGIS DDL, Pydantic models, CRS model, permissions |
| [`03-auth-security.md`](docs/03-auth-security.md) | OIDC, MCP OAuth + DCR, identity propagation, SSRF controls |
| [`04-mcp-server.md`](docs/04-mcp-server.md) | Complete tool surface with schemas |
| [`05-geoprocessing.md`](docs/05-geoprocessing.md) | Interpolation, fault handling, contouring, aggregation |
| [`06-rendering.md`](docs/06-rendering.md) | Playwright render service, style pipeline, tiles |
| [`07-frontend.md`](docs/07-frontend.md) | Monorepo, map component API, desktop layout, state management |
| [`08-styling-palettes.md`](docs/08-styling-palettes.md) | Style model, property editor, ramp editor |
| [`09-editing.md`](docs/09-editing.md) | Terra Draw, snapping, topology, validation |
| [`10-jobs-async.md`](docs/10-jobs-async.md) | Queue, status protocol, progress, quotas |
| [`11-file-io.md`](docs/11-file-io.md) | Format matrix, connectors, shapefile caveats |
| [`12-roadmap.md`](docs/12-roadmap.md) | Phased milestones with acceptance criteria |
| [`adr/`](docs/adr/) | Architecture decision records |

## Architecture in a paragraph

React/Mantine frontend with MapLibre GL JS. FastAPI backend on PostGIS. Separate worker pools
for geoprocessing (`arq`) and rendering (Playwright + headless Chromium running real MapLibre
GL JS). Rasters as Cloud-Optimized GeoTIFF served through TiTiler; vectors as dynamic MVT.
MapLibre Style JSON is the single source of truth for appearance — the interactive map and the
headless renderer consume byte-identical style documents. An MCP server exposes the whole thing
to Claude over Streamable HTTP with OAuth 2.1.

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
| Schema, RLS policies, ownership trigger | Applied and rolled back cleanly; 17 tables, 24 policies |
| Compose stack, container images | `docker compose up` verified from empty volumes |
| Seed script | 2,000 points, 20 faults, one grid, queryable via DuckDB and TiTiler |
| Identity, permissions, audit | Working offline via the verifier seam ([ADR 0009](docs/adr/0009-offline-identity-seam.md)); OIDC and MSAL paths written but unrun |
| Dataset and project REST, sharing | Working |
| Upload, ingest, vector readers | Working for shapefile, GeoJSON, GeoPackage, CSV/XYZ |
| `apps/mcp` | Four discovery tools over stdio, plus `webmap-mcp-install` for per-workstation registration |
| `webmap_geo` solvers, `apps/render`, `packages/*` | Empty by design until Phases 2-4 |
