# 12 — Roadmap

Seven phases. Each has a demoable outcome and explicit acceptance criteria. A phase is not
complete until every criterion passes.

Estimates assume a small team with AI assistance. Treat them as relative weights, not
commitments — the ordering is the part worth defending.

Phase 1 is shorter than originally planned, but not for the reason a previous revision of this
document gave. The identity work is real and is back (`adr/0007-multi-user-directory-sso.md`).
What is gone is `webmap-auth` — the OAuth server with Dynamic Client Registration — because
`adr/0008-local-stdio-mcp.md` removes the requirement for one rather than solving it.

---

## Current status

Updated 2026-09-11 (Phase 4 complete; Phase 5 in progress). A phase is complete only when every
criterion passes; a criterion met with a caveat says so rather than being ticked quietly.

Acceptance criteria read `[x]` met, `[ ]` not met, and **`[~]` met in part** — the caveat is
always stated, because a tick that quietly stands for "mostly" is how a phase gets called
complete twice.

Verified on this date against a live stack: **1,080 Python tests pass and 12 skip** —
the skips name the service they need — with 696 TypeScript tests (97 style-model, 76 ui,
37 map, 486 web), and lint, formatting, typechecking and the package-boundary contracts
clean in both languages. Migrations apply, roll back to base and re-apply.

**What is deliberately not counted as done.** Editing now works end to end: pick the edit tool,
select a feature, drag a vertex onto a neighbour's, save, and the layer holds a new version
whose tiles come back changed. What is *not* there is recovery. A concurrent save is detected
and reported (§5.3) and the edits stay in the buffer, but Refresh and Force are unbuilt, so the
answer to "someone saved first" is still to redo the work. Nothing survives a browser refresh
either — §5.4's IndexedDB mirror is unbuilt. And a layer at or above 5,000 features refuses to
open for editing rather than degrading, which is what §17 asks for and is not the same as the
viewport working set §17 describes.

| Phase | State |
|---|---|
| 0 — Foundations | **Complete.** All criteria verified. |
| 1 — Identity and data plane | **Complete.** Two caveats in "Carried forward" below. |
| 2 — Display | **Built.** 8 of 11 criteria verified; the rest need a browser — see below. |
| 3 — Claude integration | **Built.** 4 of 9 verified. The visual harness exists now and its goldens do not; `tests/mcp_eval/` runs ten questions against the live tool surface. |
| 4 — Gridding | **Complete.** All 10 criteria verified. Jobs, contouring with gapped labels ([`adr/0015`](adr/0015-contour-labels-gap-the-line.md)), lineage, breaklines, clipping, label anchors, and the ingest, sync and export tasks. |
| 5 — Styling and editing | **In progress.** The backend is largely there — layers and basemaps as shared objects, capability roles, the three preference tiers, palette import/export, attribute summaries, and the feature-edit writer with its version pointer. On the front end the nine shared controls, the formatting dialog, all four editing surfaces, and vertex editing on the map — click selection, snapping against exact geometry, handles, move/add/delete, Save and Discard — are built. Drawing new geometry is not. **The operation catalog's geometry is built** — Split, Combine, Explode, Dissolve, Smooth, Simplify and Reshape as pure functions in `webmap_geo.edit`, with the rules the spec states and the failures they prevent under test; what remains there is the UI that calls them. |
| 6 — Aggregation and polish | **Partly done ahead of order.** The aggregation catalog and the clip job landed with Phase 4's work, because both were needed by it. |
| 7 — Geostatistics | **Started.** `13` §19's build order, steps 1 and part of 2: the §16 flag registry, the `prep/` pipeline (validate, decluster, transform, screen), Matérn and stable models with nested structures, and §7.6's anisotropy significance test. The kriging engine and REML are next, and §19 is emphatic that REML comes **before** the trend loop, which is unsafe without it. |

**The visual harness now runs.** Bringing the render service up found four defects stacked on
each other — the shell stage never copied `tsconfig.base.json`, `*.tsbuildinfo` was not
dockerignored so `tsc` emitted nothing, the Playwright base image did not match the locked
library, and the service refused the host its own glyphs are served from. The harness itself
looked for the service on the wrong port. All fixed; the six cases render, and the goldens are
the reviewer's to generate and commit.

**The gap worth naming** was unwritten *harnesses*, and both are now built.
`tests/visual/` renders six cases through the render service and compares them against
goldens; `tests/e2e/` drives Playwright against the running application at `07` §5.1's
1440 px design width. Between them they gate criteria across Phases 2, 3 and 6:
everything that needs a browser at a real resolution or a rendered image.

**`tests/visual/golden/` is still empty, deliberately.** A golden is a reference image,
and one nobody reviewed makes every later diff meaningless — so generating them is a
person's job with `make update-goldens`, and until then every case *skips with
instructions* rather than passing. A green suite that checked nothing would be worse
than the empty directory was.

`tests/mcp_eval/` is no longer among them — ten questions run against the live tool
surface — but it tests reachability rather than tool *selection*, which is the half
§10 cares most about and the half that needs a model.

**The other gap is reference comparison.** `CLAUDE.md` §6.1 puts it at the highest bar in
the repo for `webmap_geo`, and no test in the suite carries the `reference` marker. Minimum
curvature against a Surfer grid is blocked on having a Surfer grid at all — which is a thing
to be supplied rather than written.

### Carried forward from Phase 1

One item is genuinely untestable here and one is deliberately deferred.
Neither blocks later phases; the first must be closed on deployment day.

- **The MSAL broker call is untested and will stay so until a domain-joined
  workstation.** `adr/0009-offline-identity-seam.md` confines the untested
  surface to the acquisition call itself — everything downstream of it is the
  same code the development path exercises — but that one call meets reality
  cold on deployment day. The same is true of the OIDC authorization-code flow
  in `apps/api/routes/auth.py`: written, reviewed, never run against a tenant.
  The three things most likely to be wrong are the audience value, the groups
  claim name, and whether groups arrive as object ids or display names.
- **Ownership transfer is blocked, and now blocked for a second reason.**
  Migration `0002` installs a trigger refusing ownership changes through an
  ordinary UPDATE, because the RLS `WITH CHECK` did not catch them, and leaves
  `webmap.allow_ownership_transfer` as a transaction-local escape for the
  audited operation `03-auth-security.md` §3.3 describes. Migration `0006`
  extends that trigger to `layer` and `basemap`, which `adr/0010` added two
  revisions after it was written — so the two objects the sharing model exists
  *for* were the two an ordinary UPDATE could reassign.

  The operation itself was **attempted on 2026-09-10 and backed out**. It exists
  to recover objects an administrator cannot read, and every route to them is
  closed by design: ordinary service code gets `NotFound` because the app role
  is `NOBYPASSRLS`, and a `SECURITY DEFINER` function owned by the table owner
  fares no better, because migration `0004` sets `FORCE ROW LEVEL SECURITY`,
  which applies to the owner and refuses `row_security = off`. Both were
  written and run; the function reported "No layer with id …" for the object it
  had just been handed. Finishing it needs a narrowly scoped `BYPASSRLS` role —
  one that owns two functions and nothing else — which is a **new role in the
  security model** and belongs in a decision rather than a commit. `adr/0010`
  §5 carries the reasoning.

### Operational notes

- **DuckDB extensions are baked into the API and worker images.** A container that cannot
  load `spatial` and `httpfs` refuses to start rather than serving a 500 on the first tile
  request. On a network that blocks `extensions.duckdb.org` — a captive portal will — run
  `make duckdb-extensions` and start with `infra/compose.offline.yaml`.
- **The HTTP-driven integration tests write to the development stack** and soft-delete what
  they create. They are the only tests that do; everything else on the `engine` fixture gets
  a throwaway database.

### Deliberately empty

These modules exist so that import contracts and package boundaries have
something to check, and are empty until their phase. They are not oversights:

| Module | Phase |
|---|---|
| `packages/ui` schema-driven property editor | 5 — the nine shared controls of `07` §6.3 are built; this is the §6 escape hatch generated from the MapLibre style spec |
| `webmap_io.connectors` (file share, PostGIS) | 6 — **but the directory is empty**: no `__init__.py`, so it is not importable and no contract names it |
| `webmap_io` readers for `.grd`, ZMAP+, KML, DXF | 6 |
| Export and loss reporting (`11-file-io.md` §4.2, §7) | 6 |
| `tests/visual/golden` (the images; the harness is built) | 2, 3 — needs a person to generate and review them |

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

- [x] `docker compose up` gives a working stack from a clean clone
- [x] `make check` runs lint, typecheck, and tests across both languages
- [x] `import-linter` and `eslint-plugin-boundaries` contracts fail CI (verify each with a
      deliberate violation, including a `pyproj` import outside `webmap_geo.crs`)
      — all seven checked against a deliberate violation, and the `pyproj` one has since
      caught a real one in `webmap_io.read`
- [x] Migrations apply and roll back cleanly
- [x] Seed data loads and is queryable
- [x] The ingest writer's Hilbert ordering prunes row groups on a tile-extent predicate,
      asserted against a layer large enough to produce many groups — 2,000 seed points is a
      single row group at the 128 MB target, so the seed cannot demonstrate this itself (see
      `11-file-io.md` §6.1 — an unsorted write silently defeats it)

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

- [x] A geologist logs in via SSO and lands with the correct team memberships — through the development verifier (`adr/0009`); the OIDC path is written but unrun
- [x] Uploading a shapefile with a `.prj` registers a dataset with correct CRS and bbox
- [x] Uploading a shapefile *without* a `.prj` fails with the message from `11-file-io.md` §3
- [x] User A cannot read User B's private dataset — verified at both application and RLS layer
- [x] The app DB role lacks `BYPASSRLS`; the assertion fires when it is granted
- [x] Every endpoint reading the data plane has an explicit permission check — RLS does not
      cover object storage (`02-data-model.md` §4.1). `resolve_feature_object` and
      `resolve_grid_object` are the only functions that return a storage key, and both
      check first; grep for other readers of `parquet_key` when reviewing
- [~] The local MCP server acquires a token silently and calls an authenticated tool, with no
      prompt and no stored password — proven for the development token source; the MSAL
      broker path is unrun (see "Carried forward")
- [x] MCP calls execute as the requesting user (verify: two users, different results)
- [x] The local MCP server holds no database connection and no permission logic — enforced by
      an `import-linter` contract rather than by inspection, so it fails CI instead of
      review. It also imports no `webmap_core`: that package depends on SQLAlchemy, and a
      contract permitting the package that imports the driver permits the driver
- [x] All hostile fixtures from `11-file-io.md` §8 fail with actionable messages

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

- [ ] A 500k-feature layer pans and zooms at 30+ fps — the browser harness exists now
      (`tests/e2e/`), and **no case measures frame rate**; that needs a trace, not an
      assertion
- [x] Tile requests without a valid scoped token return 403 — bearer, scoped, tampered,
      malformed, and cross-dataset cases all covered
- [x] TypeScript and Python compilers produce identical Style JSON for every test vector
- [x] A session saved in the browser reloads with identical appearance
- [x] Scale bar is correct at three latitudes spanning the working area — tested as the
      cos(latitude) relation plus equator, symmetry and the Mercator clamp, which is the
      property the three-latitude check was standing in for
- [x] Layer reorder, visibility, and opacity persist across reload
- [ ] At 1920×1080, layer tree + symbology + attribute table are all usable without
      occluding the map — the harness drives that resolution and checks the breakpoint
      widens the panels; **"usable without occluding" is not yet asserted**
- [x] Panel widths and collapsed state persist per user across sessions
- [~] Below 1280 px the app shows the minimum-width notice rather than reflowing —
      **now covered**, by `tests/e2e/test_shell.py` at a 1024 px viewport. It asserts the
      notice is visible and the shell is not, which is the pair that matters. Tilde rather
      than tick because the case skips when the stack is down, so it is green in CI and
      absent on a workstation with nothing running
- [x] Status bar shows analysis CRS, live cursor coordinates in that CRS, and map scale
- [x] Every documented keyboard shortcut works; every context menu is reachable via
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

- [x] Claude renders a map of a registered dataset in under 5 s p95, and the image
      actually displays — an image content block, not a markdown URI. Measured at
      0.9–1.1 s for three 2560×1440 renders with zero failed requests; **the p95 is a
      spot measurement, not a standing test**
- [ ] The rendered image is pixel-comparable to the same view in the browser — **the
      harness is built and its goldens are not**. `tests/visual/` renders six cases and
      compares them at a 0.1% tolerance; every case skips, naming the command that would
      generate a golden and the reminder to look at it first
- [ ] Render metadata contains value range, units, CRS, and vintage — verified against the
      source. The *formatter* is tested over a hand-built record; **nothing checks the
      values against the dataset they came from**, which is the half that matters
- [x] A hostile style referencing an internal host is rejected before dispatch
- [x] `page.route` blocks a redirect to a non-allowlisted host (test with a fixture)
- [ ] Render workers cannot reach the database or the internet (verify by attempting egress)
      — the allowlist is tested; **the container's own egress is not**
- [x] `webmap_open_session` produces a link that loads correctly for the same user, and shows
      a helpful permission error for a different user
- [~] All 10 evaluations pass — the ten questions exist and each one's answer is
      verified *reachable* through the tool surface. **Tool selection is not tested**:
      that needs a model in the loop, and there is none. See
      `tests/mcp_eval/test_evaluations.py` for exactly what the harness does and does
      not prove
- [ ] Legends appear correctly in rendered output, matching the interactive legend — legend
      derivation is tested; **its appearance in a render is not**

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
- Ordinary and universal kriging with local neighborhoods (Euclidean; not fault-aware)
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
5. Contouring + job infrastructure (1 week)

**Fault-aware kriging was cut**, not deferred. `05-geoprocessing.md` §6.2 carries the
measurements: the mesh path-distance search missed the performance target by an order of
magnitude, and the cheap approximation that would have met it is within ~6% of path distance
for the median neighbour pair, which is not a convincing trade to build a second faulted
interpolator on. Minimum curvature is the fault-aware method. A structure map across a sealing
fault is gridded with it.

**Acceptance**

- [ ] Minimum curvature matches the Surfer reference grid within 0.5% of value range —
      **blocked: no reference grid.** Nothing in the suite carries the `reference` marker,
      so the highest bar in `CLAUDE.md` §6.1 is currently unmet for every algorithm
- [x] Kriging with a known synthetic variogram recovers the field within tolerance
- [x] A surface gridded across a sealing fault with **minimum curvature** shows the correct
      discontinuity — sampled either side. The visual half now has a harness
      (`faults_over_grid` in `tests/visual/`) and no golden, so it is written and not yet
      exercised
- [x] Kriging with a fault network supplied warns that it does not honour it, and names the
      method that does
- [x] A breakline produces gradient discontinuity with value continuity — asserted on the
      surface rather than on the operator: over a terrace with a control gap straddling the
      break, the corner turns in one cell with the breakline and is rounded off without it,
      while the largest step across the line stays under one cell of honest dip. A fault on
      the same line is the control, and produces a different surface
- [x] Fault network validation catches all defects in the hostile fault fixture, each with a
      location
- [x] 1000×1000 with faults completes in under 5 minutes (minimum curvature) — **measured
      2026-09-10 at 181.5 s** for 1,083,630 cells with a 20-fault network and 2,000 control
      points, end to end through the job queue (`05` §10). Not yet measured at 100k control
      points, which is a control-density question rather than a grid-size one
- [x] Cancelling a running job leaves no partial dataset registered
- [x] Lineage record is sufficient to re-run and reproduce the identical grid — asserted by
      re-running the job and comparing the arrays, not by inspecting the record
- [x] Contour intervals are round numbers a geologist would choose
- [x] Filled bands measure the area between their levels — checked against a closed form on a
      cone, and sitting exactly under the contours drawn over them

**Phase 4's worker tasks are all built.** Ingest, sync and export: the connector
abstraction (`11` §2) with a file share connector carrying §2.2's four mitigations, a
`sync` job that re-materialises a dataset behind a guarded pointer move, an `export` job
with §4.2's loss reporting, and an `ingest` job for uploads past the 32 MB inline limit
`10` §6 asks for. The inline and queued ingest paths are asserted to produce the same
dataset, because two code paths that disagree are worse than one that is slow.
connector abstraction (`11` §2), a file share connector with §2.2's four mitigations, and a
`sync` job that re-materialises a dataset from its upstream behind a guarded pointer move.

**Universal kriging and cubic spline are done** (`05` §6.2, §6.4). `Method` now has six
members, all reachable from `webmap_interpolate`.

**Breaklines are honoured** (`05` §6.1). `soft_edges` marks the same links `blocked_edges`
would, and `minimum_curvature` drops the curvature rows across them while keeping the gradient
rows — the surface kinks and does not tear. `breakline_control` turns the line's own Z into
densified control, because the mask permits a kink and says nothing about where it goes. Every
other method gets the elevations and a warning that it will round the break off.

**The label-anchor job is done** (`08` §2.4) — one point per polygon, with `anchor_method` and
`clearance`, reachable as `webmap_label_anchors` and `POST /api/v1/jobs/label-anchors`. The
geoprocessing already existed; what was missing was anything that ran it over a dataset.

**Grid clipping is done** (`08` §5.2) — to a polygon layer, to selected features of one, or to
the control, with the sense invertible. A derived grid with lineage to both inputs, recomputing
the display range and the extrapolation fraction, reachable as `webmap_clip_grid` and
`POST /api/v1/jobs/clip`.

**The aggregation catalog is done** (`05` §8): fifteen operations in `webmap_geo.aggregate`,
a job, `POST /api/v1/jobs/aggregate`, and `webmap_aggregate`. **`webmap_fit_variogram` is
done** and runs inline rather than as a job (`10` §6).

**Filled contour bands are done** (`05` §7.1). `webmap_contour` takes `fill`, and produces the
polygons between levels as a second layer from the same level list — with each band's bounds,
midpoint, area and whether it is open-ended.

**Risk.** Minimum curvature is now the only fault-aware interpolator, so a geologist who wants
a kriged surface of a faulted field cannot have one. Mitigated by saying so at the point of
use — `webmap_interpolate` warns when a fault network is supplied with a kriging method, and
names minimum curvature — rather than by producing a surface that looks fault-aware and is not.
Watch for users routing around the warning by omitting the fault network entirely, which
produces the same wrong surface with nothing said about it.

---

## Phase 5 — Styling and editing (5–6 weeks)

**Deliverables**

- Ramp editor with histogram underlay — **built**, and the underlay is the point of it
  rather than decoration: a ramp is only right relative to the values it colours
- Palette import: `.clr`, `.cpt`, QGIS XML — **built**, plus WebMap's own `.json` for
  round-tripping, with `.clr`, `.cpt` and `.json` written back out
- Schema-driven property editor generated from the MapLibre style spec
- Graduated and rule-based symbology; all classification methods
- Style templates with resolution order
- **Command registry** (`09` §8) — **built**, ahead of every surface as the section requires.
  Fifty-odd commands defined once, with the menu in §9's order, the palette excluding what is
  disabled, per-scope context menus, and `conflicts(state)` to prove no key resolves to two
  enabled commands at once. What remains is the surfaces that render from it
- **Selection** — **click selection is built** (`apps/web/src/editing/modes.ts`,
  `useMapEditing.ts`): multi-feature and vertex scopes, every §4 transition rule including the
  two-press `Esc` and the active-layer switch that ends the session, and the hit test that turns
  a click into a feature id. Rectangle and lasso are what remain
- **Edit session** (`09` §5) — **built**: dirty buffer, `Command`/`FeatureDelta`, undo/redo
  atomic over a command's whole delta set, Save/Discard, the Refresh half of the 409 rebase,
  and the snapshot §5.4 mirrors to IndexedDB. The IndexedDB write itself and the recovery
  prompt are what remain
- **Screen-space snapping** (`09` §6) — **built**: vertex, edge, intersection and midpoint
  passes in priority order, the pixel-clamped tolerance reporting *which* clamp bound, the drag
  exclusion with its ring wrap, the `queryRenderedFeatures` candidate set with its
  drag-duration projection cache, local geometry substituted for stale tile geometry, and the
  indicator drawn as the four glyphs §6.7 assigns. §6.6's exact-coordinate resolution came with
  the working set rather than as coordinate matching: the layer's exact geometry is already in
  hand and keyed by the same id, so a candidate is resolved by lookup. A feature outside the
  working set keeps its tile geometry and reports itself inexact, which is what the hollow
  indicator was always meant to say
- **The editing surfaces** (`09` §9, §10) — **built**: the menu bar in §9's order with disabled
  items greyed and their shortcuts shown, the command palette that excludes what cannot run and
  matches on descriptions and keywords, the persistent toolbar with its ten-slot budget
  asserted, the contextual operation bar with `Esc`/`Enter`, and the status strip. All four
  render from the one registry, so none of them can disagree about what is enabled
- **Vertex editing** — **built**: square handles from their own source, a press that selects
  and a drag that moves, the three-pixel threshold that keeps a shaky click from moving
  anything, double-click to delete with the ring minimum refused rather than cascaded, and Z
  carried through every one of them. Nudge, numeric and bearing entry remain
- **Persistence** (`09` §13, `adr/0005`) — **built**: `POST /features/{id}/edits` writes the
  next immutable object and advances the version pointer, which is the commit; the pointer
  update is conditional on the base version, so two editors who both read version 7 produce one
  winner and one 409. Save and Discard are wired to the editor. What remains is the recovery
  half — §5.3's Refresh and Force — and §5.4's IndexedDB mirror
- **The working set** (`09` §17) — **the cap is built and the viewport query is not**: a
  session fetches the layer's exact geometry, and a layer at or above 5,000 features refuses to
  open for editing with a message saying so rather than degrading silently. The viewport query,
  the re-query on pan and the MVT/GeoJSON source switch remain
- **Terra Draw for new geometry only** ([`adr/0014`](adr/0014-terra-draw-scoped-to-creation.md)),
  wired to the snap engine through `toCustom`
- **Topological editing** ([`adr/0013`](adr/0013-topological-editing-within-the-active-layer.md))
  — **the index and the propagation rules are built**: the coordinate hash over the active
  layer, which operations honour the toggle and why the two that do not say so, and the
  shared-edge lookup that makes vertex-add propagate. What remains is propagation from the
  vertex handlers — a move currently changes only the vertex dragged — and the discoverability
  nudge
- **Operations catalog** (`09` §11) — split, reshape, combine/explode/dissolve, overlay,
  smooth/simplify, buffer, align
- Geometry validation blocking on errors, plus **Validate Topology** as a whole-layer job
- Copy-on-write version commit; the 409 names the changed features (`adr/0005`, amended twice)
- Attribute editing, including bulk edit and dissolve resolution strategies
- **Layers and basemaps as shared objects** (`adr/0010`): **done on the backend.** Revision
  `0004_layers_basemaps` created the tables, RLS with `FORCE` and all four policies, and the
  `ON DELETE RESTRICT` that makes a shared layer un-deletable while a basemap uses it;
  `webmap_core.services.layers` and `/api/v1/layers`, `/api/v1/basemaps` are the services and
  endpoints over it — CRUD, duplication by reference, save-any-map-as-basemap, and the `07`
  §6.1 refusal that names the basemaps. **What remains is the map's active layer**, which is
  frontend state, and the `map_session.layers` reshape `adr/0010` calls for
- **Ownership transfer** (`adr/0010` §5) — attempted and **backed out**, with the reason
  recorded in that ADR. It needs a narrowly scoped `BYPASSRLS` role: the operation exists to
  recover objects an administrator cannot read, and both ordinary service code and a
  `SECURITY DEFINER` function are correctly blocked by RLS and by `FORCE ROW LEVEL SECURITY`.
  A new role in the security model is a decision, not a commit. Migration 0006 — the
  ownership-change trigger reaching `layer` and `basemap` — did land
- **Capability roles**: `webmap_core.services.capabilities` holds the checks —
  `is_global_admin`, per-team `role`, and `require_publish_scope`, which makes
  `visibility = 'org'` administrator-only as `adr/0010` §2 requires. **Wired into layers and
  basemaps only.** Extending it to the older ownable services (project, dataset, palette,
  style template, session, render) is a deliberate behaviour change on existing endpoints and
  is its own commit. The administration screens they gate are not built
- **Default basemaps** across the user / team / global tiers, keyed on presentation: **done.**
  `webmap_core.services.preferences` resolves user → team → global, exhausting each tier
  before descending, breaking multi-team ties alphabetically by `team.slug`, and falling
  through a default that names a basemap the caller cannot resolve. Revision
  `0005_user_default_basemaps` fixed the user tier, whose column revision 0004 never actually
  added — `CREATE TABLE IF NOT EXISTS` on a table that already existed
- **Formatting dialogs and shared controls** (`07` §6.2-6.3): **the nine shared controls and
  the dialog that composes them are built** — colour modes, line, marker, text, halo, the zoom
  window, null colour and the live legend preview. What remains is the data behind three of
  them — **now built**: `GET /api/v1/features/{id}/summary` answers with distinct values and
  counts (refusing past 5,000 distinct rather than truncating) or with a range and a 40-bin
  histogram, and `usePalettes` feeds the picker. **`PaletteIO` is served**:
  `webmap_core.style.palette_io` reads Surfer `.clr`, GMT `.cpt`, a QGIS ramp `.xml` and
  WebMap's own `.json`, and writes the first, second and fourth, behind
  `POST /api/v1/palettes/import` and `GET /api/v1/palettes/{id}/export.{fmt}`. Size-by-column
  and index-contour rules are rule-based symbology and are not built
- **Label formatting** (`08` §2.4): the size-mode control and its reference zoom, the zoom
  window that is the only thinning control once collision detection is off, and consuming the
  precomputed anchor source Phase 4 produces
- **Grid colouring** (`08` §5.2): interval bands snapped to the contour interval, gradient
  over a P5-P95 display range, and clip-to-polygon

**Acceptance**

- [ ] A Surfer `.clr` imports and renders identically to Surfer's display of the same grid
- [ ] The property editor exposes every paint and layout property for each layer type,
      correctly filtered by geometry
- [ ] A feature round-trips through every engine hop with bit-identical coordinates (`09` §2.4)
- [ ] A topological vertex move leaves every previously coincident vertex still coincident —
      the coincidence index that answers "which vertices" is built and tested; nothing moves a
      vertex yet
- [~] Vertex add propagates to the neighbour sharing that edge — the case that silently creates
      slivers when implemented halfway (`09` §7.3). **`sharedEdges` is built and its first
      version reproduced exactly that sliver**: a ring's closing segment has endpoints N-1
      apart rather than 1, so an adjacency test accepting only 1 never saw it, and on the
      two-lease fixture the neighbour's shared boundary *was* its closing segment. Tested;
      not yet wired to a vertex handler
- [ ] Two sessions commit against one version; the loser gets a 409 **naming the features that
      changed**, not just the version
- [ ] Snapping lands within tolerance on vertices, edges, and intersections, with visual
      feedback
- [ ] The full snap pass completes in under 4 ms with 50k segments in view (`09` §6.5)
- [ ] A polygon digitized against an existing boundary with snapping on produces no sliver
- [ ] Editing a fault and re-gridding produces a surface reflecting the new geometry
- [~] Undo restores exact prior state across 20 random operation sequences — undo is built
      and tested on the cases that carry the design (a multi-feature command undone atomically;
      undoing to the start leaving the session *clean*), but by example rather than by the
      Hypothesis property `CLAUDE.md` §6.3 asks for
- [ ] A colour-filled grid's bands land on the contour levels drawn over it
- [ ] An interval palette renders the exact colours it names, and a value outside every band
      clamps to an end colour rather than rendering transparent
- [x] The italic control is disabled for a family that has no italic — and says so, because
      a disabled control with no reason is a bug report waiting to be filed
- [ ] A reference-scale label covers the same ground distance at every zoom, and a fixed label
      the same screen size — measured against MapLibre's expression engine, not eyeballed
- [ ] Every label draws above every object layer, whatever the layer draw order
- [ ] A polygon spanning a tile boundary keeps one label in one place while panning and
      zooming — the failure precomputed anchors exist to prevent
- [~] Two basemaps share one layer; editing the layer changes both, and soft-deleting it is
      refused with both basemaps named — **the refusal is tested and names them**, in the
      message rather than as a foreign-key error. "Editing the layer changes both" needs the
      map, which reads the membership at session open
- [~] Duplicating a layer copies the row and references the same object — asserted on
      `dataset_id` rather than on storage size. The same claim, and it runs in a millisecond;
      the storage-size version is the one that would also catch a copy made somewhere else
- [x] A default basemap resolves user → team → global, presentation-specific before general at
      each tier, with the source tier reported — including the rule that is easy to get
      backwards: a tier is exhausted before resolution descends, so a user's *general* default
      beats their team's presentation-specific one
- [~] A non-administrator cannot set `visibility = 'org'` — `require_publish_scope` enforces
      it and is wired into layers and basemaps. **No test covers it yet**, and it is not wired
      into the older ownable services
- [~] A global administrator has no implicit read access to a private layer — **observed, not
      asserted**. The ownership-transfer attempt was blocked by exactly this property, twice:
      once through RLS and once through `FORCE ROW LEVEL SECURITY`. That was a development
      finding and the tests around it were backed out with the feature, so nothing in the
      suite stands guard over it today
- [ ] Removing a user from a team leaves everything they own intact
- [ ] A deactivated user cannot sign in, and their team-visible layers still resolve in a
      colleague's map
- [ ] A global administrator can transfer a deactivated user's private layer to a named owner
      **without being able to read it** — the test that keeps the confidentiality claim honest
- [ ] The same transfer is refused for an *active* user's objects

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

- [ ] Every aggregation operation produces results matching a PostGIS/QGIS reference — the
      fifteen operations are built and tested against closed forms and invariants; **none is
      compared against another tool's output**, which is the bar this criterion sets
- [~] Share-sourced datasets sync on schedule and show `synced_at` in the UI — the job is
      built and `synced_at` is written on every run, including one that found nothing
      changed. **On demand, not yet on a schedule**: `datasets_due` decides what a sweep
      would pick up and no cron calls it
- [x] Path traversal via a crafted share URI is blocked — `../` and a symlink pointing out
      of the share, both refused against a resolved root
- [x] Shapefile export reports truncation and collision before writing — and refuses to
      write until the loss is accepted, rather than reporting it beside a file that was
      produced anyway. The names the warning predicts are the names the writer assigns,
      which is asserted rather than assumed: both call one function
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
| Full planar topology — a stored node/edge graph, and cross-layer propagation | Coverage editing spans layers. The narrower within-layer coincidence editing of `adr/0013` is Phase 5, not deferred |
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

---

## Phase 7 — Geostatistics (8–10 weeks)

Specified in full in `13-kriging.md`, which also carries the build order (§19) and the
reasoning behind each acceptance criterion below. The largest single phase in this plan, and
the one with the most ways to produce plausible output that is wrong.

**Deliverables**

- Structured flags (`13` §16) replacing free-text warnings across `webmap_geo`
- Declustering, target transforms, covariate screening
- Variography: directional, variogram maps, nested and Matérn models, anisotropy significance
- **REML fitting for trend residuals** ([`adr/0011`](adr/0011-reml-for-trend-residual-variograms.md)),
  before any trend code exists
- Simple, universal/KED, indicator and block kriging beside the existing ordinary kriging
- Trend estimation: preset forms, the sandboxed expression parser, GLS, the GLS↔REML loop
- **Regression kriging** — the first end-to-end estimator and the first useful deliverable
- **Regression indicator kriging** and the `LocalCDF`, persisted as a multi-band grid
- Spatially-blocked validation: CRPS, PIT, threshold accuracy, Krige slope, four baselines
- Attribution diagnostics (`13` §12), variance budget first
- SGS, connectivity statistics, UK/KED
- The job, the API route, the MCP tool
- The geostatistics workbench (`07` §9), including ECharts as a new frontend dependency

**Acceptance**

- [ ] Synthetic recovery: the GLS↔REML loop recovers a known θ within its standard errors, and
      REML recovers range, sill and nugget with **less bias than WLS** on the same data
- [ ] Bias demonstration: a method-of-moments fit to the same residual materially
      underestimates the range — the test that fails if REML is ever "simplified" away
- [ ] Simple kriging with a global neighbourhood interpolates exactly at data points, with
      zero variance
- [ ] Order-relation correction returns a monotone CDF in [0, 1] for any input, over a
      property test rather than examples
- [ ] The hyperbolic upper tail integrates to 1 and matches at the last threshold;
      negative-support input triggers the fallback rather than producing a number
- [ ] PIT of a correctly specified Gaussian model is uniform by KS test
- [ ] No block-CV fold has a training point within the block buffer of a test point
- [ ] Declustering matches a reference implementation on a GSLIB dataset
- [ ] A run reproduces bit-identically from its lineage record alone
- [ ] `TREND_ABSORBED` fires on a synthetic case built so the covariates are spatial proxies,
      and does not fire on one built so they are not
- [ ] The trend-only baseline winning is reported above the map, not buried in a metrics table
- [ ] Changing the quantile, the exceedance threshold or the reference scenario updates the
      map with **no server call** (`07` §9.1)
- [ ] A tail-dependent quantile is labelled as such wherever it appears, including in the
      layer name

**Risk.** The failure mode this phase is most exposed to is not a crash — it is a run that
completes, looks authoritative, and has let the trend absorb the spatial signal. `13` §12.4 and
§13 exist for that, and the acceptance criteria above test the detectors rather than only the
estimators. Watch for the detectors being weakened to make a demo look cleaner.
