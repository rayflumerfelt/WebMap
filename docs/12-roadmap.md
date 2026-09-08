# 12 — Roadmap

Six phases. Each has a demoable outcome and explicit acceptance criteria. A phase is not
complete until every criterion passes.

Estimates assume 2–3 engineers plus AI-assisted development. Treat them as relative weights,
not commitments.

---

## Phase 0 — Foundations (2–3 weeks)

Scaffolding. Boring, and skipping it costs triple later.

**Deliverables**

- Monorepo: pnpm + Turborepo, uv workspaces, package boundary lint rules enforced
- Docker Compose: Postgres+PostGIS, Redis, MinIO, Martin, TiTiler
- Alembic migrations for the full schema in `02-data-model.md`
- CI: lint, typecheck, test, build on every PR
- `CLAUDE.md` in place; pre-commit hooks active
- Structured logging and OpenTelemetry wiring
- Seed script generating synthetic Midland Basin data: 2,000 points, 20 faults, one grid

**Acceptance**

- [ ] `docker compose up` gives a working stack from a clean clone
- [ ] `make check` runs lint, typecheck, and tests across both languages
- [ ] Boundary violations fail CI (verify with a deliberate violation)
- [ ] Migrations apply and roll back cleanly
- [ ] Seed data loads and is queryable

---

## Phase 1 — Data plane (~1 week)

Previously "Identity and data plane," at 4–6 weeks. The identity half is gone — see
`adr/0001-single-user-deployment.md`. What remains is getting real data into the system,
which was always the part Phase 2 actually needed.

**Deliverables**

- Dataset registry with the upload connector
- Vector read for shapefile, GeoJSON, GeoPackage, CSV/XYZ
- Ingest pipeline with validation, normalization, and warnings
- REST CRUD for projects and datasets
- Static bearer token on the API and MCP endpoints
- Audit and lineage records written on ingest

**Acceptance**

- [ ] Uploading a shapefile with a `.prj` registers a dataset with correct CRS and bbox
- [ ] Uploading a shapefile *without* a `.prj` fails with the message from `11-file-io.md` §3
- [ ] All hostile fixtures from `11-file-io.md` §8 fail with actionable messages
- [ ] An ingested dataset carries a lineage record naming its source and actor
- [ ] A request without the bearer token is rejected on both the REST and MCP surfaces
- [ ] Claude calls an authenticated tool end to end

**Risk.** Low, now. The previous risk here — DCR brokering blocked by corporate security
policy — was the largest in the project and no longer exists. The remaining variance is in
file I/O breadth, which `11-file-io.md` §8 makes testable up front.

---

## Phase 2 — Display (4–5 weeks)

First phase with something a geologist recognizes.

**Deliverables**

- `@webmap/map` component with the API in `07-frontend.md` §2
- Martin MVT function-sources; automatic GeoJSON/MVT switching
- TiTiler COG serving with dynamic colormaps
- Auth proxy for tile endpoints with scoped signed tokens
- Style compilation in both TypeScript and Python, sharing test vectors
- Layer tree, basic symbology (single symbol, categorized)
- Session create, load, autosave
- `@webmap/ui` legend, scale bar, north arrow
- Desktop application shell: docked resizable panels, toolbar, status bar, keyboard
  shortcuts, context menus (`07-frontend.md` §5)

**Acceptance**

- [ ] A 500k-feature layer pans and zooms at 30+ fps
- [ ] Tile requests without a valid token return 403
- [ ] TypeScript and Python compilers produce identical Style JSON for every test vector
- [ ] A session saved in the browser reloads with identical appearance
- [ ] Scale bar is correct at three latitudes spanning the working area
- [ ] Layer reorder, visibility, and opacity persist across reload
- [ ] At 1920×1080, layer tree + symbology + attribute table are all usable without
      occluding the map
- [ ] Panel widths and collapsed state persist per user across sessions
- [ ] Below 1280 px the app shows the minimum-width notice rather than reflowing
- [ ] Status bar shows analysis CRS, live cursor coordinates in that CRS, and map scale
- [ ] Every documented keyboard shortcut works; every context menu is reachable via
      `Shift+F10`

---

## Phase 3 — Claude integration (3–4 weeks)

The point of the project.

**Deliverables**

- `webmap-render` service: Playwright, browser pool, SwiftShader, request allowlist
- Server-side style assembly with SSRF validation
- Render persistence with metadata and captions
- Visual regression harness with goldens
- MCP server: discovery, session, and render tools
- MCP evaluations (10 questions)

**Acceptance**

- [ ] Claude renders a map of a registered dataset in under 5 s p95
- [ ] The rendered image is pixel-comparable to the same view in the browser
- [ ] Render metadata contains value range, units, CRS, and vintage — verified against the source
- [ ] A hostile style referencing an internal host is rejected before dispatch
- [ ] `page.route` blocks a redirect to a non-allowlisted host (test with a fixture)
- [ ] Render workers cannot reach the database or the internet (verify by attempting egress)
- [ ] `webmap_open_session` produces a link that loads correctly for the same user, and shows a
      helpful permission error for a different user
- [ ] All 10 evaluations pass
- [ ] Legends appear correctly in rendered output, matching the interactive legend

**This is the first demoable milestone that proves the concept.** Get here before adding
breadth.

---

## Phase 4 — Gridding (6–8 weeks)

The longest phase and the differentiator. Do not compress it.

**Deliverables**

- `webmap_geo` package, isolated per `01-architecture.md` §3.2
- Variogram estimation with subsampling and declustering; automatic and interactive fitting
- Minimum curvature (Briggs) with fault-aware stencils
- Constrained Delaunay triangulation and fault network validation/cleaning
- Ordinary and universal kriging with local neighborhoods and barrier-aware distance
- Cubic spline, IDW, nearest
- Contouring with smoothing and index contours
- arq job queue with progress, cancellation, quotas, idempotency
- COG output and dataset registration
- Lineage records
- MCP analysis tools

**Sub-phase ordering.** Build unfaulted first, prove it against Surfer references, then add
constraints. Do not attempt both at once — when a faulted grid looks wrong you need to know
whether the interpolator or the constraint handling is at fault.

1. Variogram + ordinary kriging, no faults (2 weeks)
2. Minimum curvature, no faults (1 week)
3. Constrained triangulation + fault validation (1.5 weeks)
4. Fault-aware minimum curvature (1 week)
5. Fault-aware kriging (1.5 weeks)
6. Contouring + job infrastructure (1 week)

**Acceptance**

- [ ] Minimum curvature matches the Surfer reference grid within 0.5% of value range
- [ ] Kriging with a known synthetic variogram recovers the field within tolerance
- [ ] A surface gridded across a sealing fault shows the correct discontinuity — verified
      visually and by sampling either side
- [ ] A breakline produces gradient discontinuity with value continuity
- [ ] Fault network validation catches all defects in the hostile fault fixture, each with a
      location
- [ ] 100k points → 1000×1000 with faults completes in under 5 minutes
- [ ] Cancelling a running job leaves no partial dataset registered
- [ ] Lineage record is sufficient to re-run and reproduce the identical grid
- [ ] Contour intervals are round numbers a geologist would choose

**Risk.** Fault-aware kriging performance. The Dijkstra neighbor search is 5–20× slower than
cKDTree. If it misses the target, the compartment-major caching in `05-geoprocessing.md` §5.2 is
the first lever; reducing default `n_neighbors` is the second.

---

## Phase 5 — Styling and editing (5–6 weeks)

**Deliverables**

- Ramp editor with histogram underlay
- Palette import: `.clr`, `.cpt`, QGIS XML
- Schema-driven property editor generated from the MapLibre style spec
- Graduated and rule-based symbology; all classification methods
- Style templates with resolution order
- Terra Draw integration, vertex editing
- Snapping with spatial index
- Geometry validation, blocking on errors
- Optimistic locking, conflict UI
- Undo/redo
- Attribute editing

**Acceptance**

- [ ] A Surfer `.clr` imports and renders identically to Surfer's display of the same grid
- [ ] The property editor exposes every paint and layout property for each layer type,
      correctly filtered by geometry
- [ ] Snapping lands within tolerance on vertices, edges, and intersections, with visual
      feedback
- [ ] Snap index query completes in under 2 ms with 50k features in view
- [ ] Concurrent edits from two sessions produce a 409 with a usable diff, never a silent
      overwrite
- [ ] A polygon digitized against an existing boundary with snapping on produces no sliver
- [ ] Editing a fault and re-gridding produces a surface reflecting the new geometry
- [ ] Undo restores exact prior state across 20 random operation sequences

---

## Phase 6 — Aggregation and polish (3–4 weeks)

**Deliverables**

- Full spatial aggregation catalog
- File share and PostGIS connectors with scheduled sync
- Export with loss reporting
- `webmap_suggest_maps`
- User preferences: basemaps, default palettes, units
- Performance tuning against the targets in `05` §9 and `06` §11
- Accessibility audit
- Documentation and onboarding

**Acceptance**

- [ ] Every aggregation operation produces results matching a PostGIS/QGIS reference
- [ ] Share-sourced datasets sync on schedule and show `synced_at` in the UI
- [ ] Path traversal via a crafted share URI is blocked
- [ ] Shapefile export reports truncation and collision before writing
- [ ] `webmap_suggest_maps` proposes sensible products for the seed dataset
- [ ] All performance targets met
- [ ] Keyboard navigation reaches every control; focus is always visible
- [ ] Security checklist in `03-auth-security.md` §11 fully green

---

## Cross-cutting: build continuously

These are not phases. They accumulate from Phase 0.

| Concern | Practice |
|---|---|
| Visual regression | Add a golden for every new render capability |
| MCP evaluations | Add questions as tools are added; run on every tool-description change |
| Reference fixtures | Add a Surfer/ArcGIS reference for every new algorithm |
| ADRs | Record every decision that contradicts these docs |
| Security checklist | Re-run at the end of every phase, not once at the end |

---

## Deferred

Not scheduled. Revisit only with a stated trigger.

| Item | Trigger to reconsider |
|---|---|
| MapLibre Native renderer | Render throughput > 100/min, or container size becomes an operational blocker |
| Real-time collaborative editing | Sustained demand from more than one team |
| Full planar topology | Coverage editing becomes a primary workflow |
| GeoParquet storage backend | A single layer exceeds 10M features |
| Kerberos delegation for shares | Share-sourced data expands beyond well-understood locations |
| Unattended/batch rendering | A scheduled reporting requirement appears |
| 3D, seismic, petrophysics | Never — these are explicit non-goals (`00-overview.md` §7) |

---

## Sequencing rationale

Two orderings that might look wrong and are deliberate.

**Auth before display.** Tempting to build a pretty map first and bolt on auth later. Do not.
Identity propagation touches every service, every query, and every job payload. Retrofitting it
means rewriting all of them, and the version that ships without it will leak data.

**Claude integration before gridding.** Phase 3 renders existing data; it does not need
kriging. Getting Claude end-to-end early validates the riskiest architectural assumption in the
project — that the conversational interface is actually good — while there is still time to
change course. Building six months of geoprocessing first and discovering the interaction model
is wrong would be the expensive failure.
