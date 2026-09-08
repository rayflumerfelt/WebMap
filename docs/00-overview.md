# 00 — Overview

**Project name:** WebMap
**Status:** Specification, pre-implementation
**Audience:** Engineers and AI coding agents implementing the system

---

## 1. What this is

A web-based geospatial mapping application for subsurface geologists, with a Model Context
Protocol (MCP) server that lets Claude drive it conversationally.

Three capabilities, in priority order:

1. **Display** spatial data — vector layers, gridded surfaces, contours — with per-user
   basemap and symbology preferences.
2. **Compute** — interpolate scattered point data into grids using geostatistical methods
   that honor geological faults, derive contours, run spatial aggregations.
3. **Edit** — create and modify vector layers interactively.

The differentiator is fault-constrained interpolation. Everything else in this system exists
in some form elsewhere; gridding that correctly refuses to interpolate across a sealing fault,
exposed through a conversational interface, does not.

## 2. Why Claude integration matters

Geologists currently move between Surfer, QGIS, ArcGIS, and Excel to produce a single map for
a partner deck. Each tool has its own project files, its own CRS handling, and its own export
quirks. The map that ends up on the slide has no provenance.

The target interaction is:

> **Geologist:** Show me a color-filled contour map of Wolfcamp A porosity for the Midland
> Basin acreage.
>
> **Claude:** *[calls `webmap_list_datasets`, `webmap_create_grid`, `webmap_render_map`]*
> Here it is — kriged with an exponential variogram, 250 ft grid spacing, faults honored.
> Porosity ranges 4.1–21.8%. *[displays PNG]*
>
> **Geologist:** The southeast corner looks over-smoothed. Let me adjust it.
>
> **Claude:** *[returns session link]* Open it here.

Claude assembles PowerPoint decks from these renders using its own file-creation capability.
WebMap does not generate PPTX. It returns images plus enough structured metadata that Claude
can write a technically accurate caption.

## 3. Users

**Primary: subsurface geologists.** Domain-expert, not GIS-expert. Fluent in Surfer and
Petrel conventions. Care about variograms, fault sealing, depth conventions, and whether the
map is defensible in front of a partner. Not interested in coordinate reference system
mechanics but severely affected when they are wrong.

**Secondary: geotechs and analysts.** Prepare data, run standard workflows, produce deck
figures.

**Tertiary: data managers.** Register datasets, manage shared basemaps and style templates,
control access.

## 4. Deployment context

**Single operator.** One geologist, one machine or one internal host. Not a shared service.

- **Identity:** none. No identity provider, no login flow, no permission model. See
  `adr/0001-single-user-deployment.md` for what was removed and what it would cost to
  bring back.
- **Sharing** happens by exporting a file or sending a render, not by granting access.
- **No Esri footprint.** Shapefile is an interchange format only.
- **Data sources are mixed:** ad-hoc uploads, SMB file shares, existing PostGIS databases.
- **Not reachable from the public internet.** This is load-bearing for §7 and for how
  renders reach Claude — see `04-mcp-server.md` §6.1.

The security posture that follows from this is in `03-auth-security.md`: the threat is
hostile *data*, not hostile users.

## 5. Scale targets

| Dimension | Target | Consequence |
|---|---|---|
| Interpolation input | 10k–500k points | Local-neighborhood kriging mandatory; global solve impossible |
| Output grid | up to 2000×2000 cells | Sparse solve, ~seconds with multigrid |
| Vector layer display | up to 5M features | Dynamic MVT from PostGIS, not GeoJSON |
| Concurrent users | ~30 peak | Modest; horizontal scaling on render and job workers |
| Render latency | < 5 s p95 | Warm browser pool |
| Grid job latency | < 3 min p95 | Async job queue with progress |

## 6. Architecture summary

Detail in `01-architecture.md`. In one paragraph:

React/Mantine frontend with MapLibre GL JS. FastAPI backend on PostGIS. Separate worker pools
for geoprocessing (`arq`) and rendering (Playwright + headless Chromium running real MapLibre
GL JS). Rasters as Cloud-Optimized GeoTIFF served through TiTiler; vectors as dynamic MVT.
MapLibre Style JSON is the single source of truth for appearance — the interactive map and the
headless renderer consume byte-identical style documents. An MCP server exposes the whole thing
to Claude over Streamable HTTP with OAuth 2.1.

## 7. Non-goals

Explicitly out of scope. Do not build these; do not let them creep in.

- **PowerPoint generation.** Claude does this. WebMap returns images and metadata.
- **3D visualization.** No horizons in 3D, no well trajectories in space, no volume rendering.
- **Seismic data.** No SEG-Y, no seismic attribute display.
- **Petrophysics.** No log curve display, no log calculations. WebMap consumes tops and
  computed values as point attributes.
- **Reservoir simulation.** Grids are for mapping, not for flow simulation.
- **Esri format lock-in.** No SDE, no `.lyr`, no ArcPy, no geodatabase writing.
- **Real-time collaborative editing.** Multi-user simultaneous editing of the same layer is
  deferred indefinitely. Optimistic locking with conflict detection only.
- **Mobile and tablet support.** This is a **desktop-first** application — see §7.1. Small
  viewports are not a supported target and are not tested.
- **Public internet exposure.** Internal network and VPN only.

### 7.1 Desktop-first, deliberately

The application targets a geologist at a workstation with a large monitor, a mouse, and a
keyboard. That is not a limitation to apologize for; it is the correct target for the work.

Interpretation is a screen-real-estate activity. A geologist comparing a kriged surface against
well control, with the layer tree, symbology panel, and attribute table all in view, needs
pixels. Every design decision that trades density for touch-friendliness makes the tool worse
at its actual job.

Concretely this means:

- **Baseline viewport is 1440×900.** Below 1280×800 the app displays a notice rather than
  reflowing. It does not attempt a degraded small-screen layout.
- **Optimized for 1920×1080 and wider.** Ultrawide and dual-monitor setups get more panels
  visible simultaneously, not larger controls.
- **Mouse and keyboard are the input model.** Hover states, right-click context menus,
  drag-and-drop, modifier keys, and keyboard shortcuts are first-class. Touch is not a
  consideration, so controls are sized for pointer precision rather than fingertips.
- **No responsive breakpoints below desktop.** No mobile navigation pattern, no hamburger
  menu, no bottom sheet. Panels are docked, resizable, and persistent.

Tablets are not blocked and will mostly function, but nothing is tested or tuned for them and
no bug filed against a small viewport will be prioritized.

## 8. Definition of success

Phase 1 is successful when a geologist can ask Claude for a contour map of a registered
dataset and receive a correct, legible, correctly-projected image in under thirty seconds
without touching another application.

Full success is when a geologist assembles a partner deck entirely through conversation with
Claude, and every map in it carries provenance sufficient to reproduce it a year later.

## 9. Document map

| File | Contents |
|---|---|
| `00-overview.md` | This file |
| `01-architecture.md` | Services, topology, technology decisions with rationale |
| `02-data-model.md` | Schema DDL, Pydantic models, CRS model, ownership |
| `03-auth-security.md` | Threat model, SSRF controls, untrusted data, destructive-op safety |
| `04-mcp-server.md` | Complete tool surface with schemas |
| `05-geoprocessing.md` | Interpolation, fault handling, contouring, aggregation |
| `06-rendering.md` | Playwright render service, style pipeline, tiles |
| `07-frontend.md` | Monorepo, map component API, desktop layout, state management |
| `08-styling-palettes.md` | Style model, property editor, ramp editor |
| `09-editing.md` | Terra Draw, snapping, topology, validation |
| `10-jobs-async.md` | Queue, status protocol, progress, quotas |
| `11-file-io.md` | Format matrix, connectors, shapefile caveats |
| `12-roadmap.md` | Phased milestones with acceptance criteria |
| `CLAUDE.md` | Conventions and guardrails for agentic development |

## 10. Reading order for implementers

Read `01`, `02`, and `CLAUDE.md` before writing any code. `02` and `04` constrain nearly
everything downstream — if you find yourself disagreeing with the entity model, resolve that
first rather than working around it.
