# 12 — Roadmap

Six phases. Each has a demoable outcome and explicit acceptance criteria. A phase is not
complete until every criterion passes.

Estimates assume a small team with AI assistance. Treat them as relative weights, not
commitments — the ordering is the part worth defending.

Phase 1 is shorter than originally planned, but not for the reason a previous revision of this
document gave. The identity work is real and is back (`adr/0007-multi-user-directory-sso.md`).
What is gone is `webmap-auth` — the OAuth server with Dynamic Client Registration — because
`adr/0008-local-stdio-mcp.md` removes the requirement for one rather than solving it.

---

## Phase 0 — Foundations (2–3 weeks)

Scaffolding. Boring, and skipping it costs triple later.

**Deliverables**

- Monorepo: pnpm + Turborepo, uv workspaces, package boundary lint rules enforced
- Docker Compose: Postgres, Redis, MinIO, TiTiler
- Alembic migrations for the full schema in `02-data-model.md`
- CI: lint, typecheck, test, build on every PR
- `CLAUDE.md` in place; pre-commit hooks active
- Structured logging and OpenTelemetry wiring
- Seed script generating synthetic Midland Basin data in EPSG:2277: 2,000 points, 20
  faults, one grid

**Acceptance**

- [ ] `docker compose up` gives a working stack from a clean clone
- [ ] `make check` runs lint, typecheck, and tests across both languages
- [ ] `import-linter` and `eslint-plugin-boundaries` contracts fail CI (verify each with a
      deliberate violation, including a `pyproj` import outside `webmap_geo.crs`)
- [ ] Migrations apply and roll back cleanly
- [ ] Seed data loads and is queryable, and its GeoParquet object prunes row groups on a
      tile-extent predicate (see `11-file-io.md` §6.1 — an unsorted write silently defeats it)

---

## Phase 1 — Identity and data plane (2–3 weeks)

**Auth starts here, not later.** Identity propagation touches every service, every query, and
every job payload; retrofitting it means rewriting all of them.

The original plan put 4–6 weeks here, most of it building `webmap-auth`. That is gone —
`adr/0008-local-stdio-mcp.md` removes the requirement for an authorization server rather than
solving it, which takes the largest schedule risk in the project off the board.

**Deliverables**

- OIDC login against the corporate IdP; team sync from group claims
- Permission model, RLS policies on all ownable tables, startup assertion
- Local MCP server: stdio, OS-brokered token acquisition, packaged install
- Dataset registry with the upload connector
- Vector read for shapefile, GeoJSON, GeoPackage, CSV/XYZ
- Ingest pipeline with validation, normalization, and warnings
- REST CRUD for projects, datasets, grants
- Audit logging

**Acceptance**

- [ ] A geologist logs in via SSO and lands with the correct team memberships
- [ ] Uploading a shapefile with a `.prj` registers a dataset with correct CRS and bbox
- [ ] Uploading a shapefile *without* a `.prj` fails with the message from `11-file-io.md` §3
- [ ] User A cannot read User B's private dataset — verified at both application and RLS layer
- [ ] The app DB role lacks `BYPASSRLS`; the assertion fires when it is granted
- [ ] Every endpoint reading the data plane has an explicit permission check — RLS does not
      cover object storage (`02-data-model.md` §4.1)
- [ ] The local MCP server acquires a token silently and calls an authenticated tool, with no
      prompt and no stored password
- [ ] MCP calls execute as the requesting user (verify: two users, different results)
- [ ] The local MCP server holds no database connection and no permission logic — verified by
      inspection, and by confirming it still works with the database firewalled from it
- [ ] All hostile fixtures from `11-file-io.md` §8 fail with actionable messages

**Risk.** Which directory backs the Windows credentials. Entra ID gives plain OIDC and silent
token acquisition through MSAL's broker; pure on-prem AD means Kerberos for the browser and
SSPI for the local process, and a keytab and SPN registration for the API. Confirm in week 1 —
the fallback path is well-trodden but it is not the same week of work.

---

## Phase 2 — Display (4–5 weeks)

First phase with something a geologist recognizes.

**Deliverables**

- `@webmap/map` component with the API in `07-frontend.md` §2
- In-process MVT generation from GeoParquet via DuckDB; automatic GeoJSON/MVT switching
- TiTiler COG serving with dynamic colormaps
- Auth proxy for tile endpoints; tile cache keyed on (dataset_id, version, z, x, y)
- Style compilation in both TypeScript and Python, sharing test vectors
- Layer tree, basic symbology (single symbol, categorized)
- Session create, load, autosave
- `@webmap/ui` legend, scale bar, north arrow
- Desktop application shell: docked resizable panels, toolbar, status bar, keyboard
  shortcuts, context menus (`07-frontend.md` §5)

**Acceptance**

- [ ] A 500k-feature layer pans and zooms at 30+ fps
- [ ] Tile requests without a valid scoped token return 403
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

- [ ] Claude renders a map of a registered dataset in under 5 s p95, and the image
      actually displays — an image content block, not a markdown URI
- [ ] The rendered image is pixel-comparable to the same view in the browser
- [ ] Render metadata contains value range, units, CRS, and vintage — verified against the source
- [ ] A hostile style referencing an internal host is rejected before dispatch
- [ ] `page.route` blocks a redirect to a non-allowlisted host (test with a fixture)
- [ ] Render workers cannot reach the database or the internet (verify by attempting egress)
- [ ] `webmap_open_session` produces a link that loads correctly for the same user, and shows
      a helpful permission error for a different user
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
- Copy-on-write version commit with 409 conflict detection; version history browser
- Undo/redo
- Attribute editing

**Acceptance**

- [ ] A Surfer `.clr` imports and renders identically to Surfer's display of the same grid
- [ ] The property editor exposes every paint and layout property for each layer type,
      correctly filtered by geometry
- [ ] Snapping lands within tolerance on vertices, edges, and intersections, with visual
      feedback
- [ ] Snap index query completes in under 2 ms with 50k features in view
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
- Performance tuning against the targets in `05` §10 and `06` §11
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
| Real-time collaborative editing | Sustained demand; concurrent commits are already detected, just not merged (`adr/0005`) |
| Remote MCP endpoint | Access needed from outside the domain, or enough users that per-workstation install is a burden (`adr/0008`) |
| MapLibre Native renderer | Render throughput > 100/min, or container size becomes an operational blocker |
| Full planar topology | Coverage editing becomes a primary workflow |
| PostGIS for the data plane | An operation DuckDB spatial cannot express, or concurrent writers (`adr/0002`) |
| Kerberos delegation for shares | Share-sourced data expands beyond well-understood locations |
| Unattended/batch rendering | A scheduled reporting requirement appears |
| 3D, seismic, petrophysics | Never — these are explicit non-goals (`00-overview.md` §7) |

---

## Sequencing rationale

Two orderings that might look wrong and are deliberate.

**Claude integration before gridding.** Phase 3 renders existing data; it does not need
kriging. Getting Claude end-to-end early validates the riskiest architectural assumption in the
project — that the conversational interface is actually good — while there is still time to
change course. Building six months of geoprocessing first and discovering the interaction model
is wrong would be the expensive failure.

**Auth before display.** Tempting to build a pretty map first and bolt on auth later. Do not.
Identity propagation touches every service, every query, and every job payload. Retrofitting it
means rewriting all of them, and the version that ships without it will leak data between
colleagues who were never meant to see each other's work.

**A note on Phase 4.** It is untouched by every architectural revision so far and remains the
longest phase and the differentiator. Nothing in `adr/0001` through `adr/0008` makes
fault-constrained interpolation easier or shorter. Do not let a cheaper Phase 1 create the
impression that the whole plan compressed.
