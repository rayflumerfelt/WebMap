# 07 — Frontend

React 19 + TypeScript + Vite. Mantine 7 for UI primitives. MapLibre GL JS for mapping.

---

## 1. Package structure

```
packages/
├── style-model/          @webmap/style-model   (no React, no MapLibre)
│   └── Symbology types + compilers to MapLibre Style JSON
├── ui/                   @webmap/ui            (React + Mantine, no MapLibre)
│   └── Layer tree, legend, scale bar, north arrow today; ramp editor and style property editor in Phase 5
└── map/                  @webmap/map           (React + MapLibre + @webmap/*)
    └── The map component
apps/
└── web/                                        (consumes all of the above)
```

### 1.1 Boundary rules

Enforced by `eslint-plugin-boundaries`, not by convention.

- `@webmap/map` **must not** import from `apps/web`. If it needs something from the app, the
  app passes it as a prop. The moment the map package imports app code, reuse is already
  broken — and reuse is an explicit requirement of this project.
- `@webmap/ui` **must not** import MapLibre. The legend and ramp editor must render in the
  headless render shell, which has no map instance in scope for overlays.
- `@webmap/style-model` **must not** import React or MapLibre. It is pure data
  transformation, shared with tooling and testable in Node.

ESLint 9 loads flat config; `.eslintrc.cjs` is the previous format and is not
read by default. The element types and the allow-matrix are the rule — the
wrapper around them is whatever the installed ESLint reads.

```javascript
// eslint.config.js (excerpt)
"boundaries/elements": [
  { type: "app",    pattern: "apps/*" },
  { type: "map",    pattern: "packages/map/*" },
  { type: "ui",     pattern: "packages/ui/*" },
  { type: "model",  pattern: "packages/style-model/*" },
],
"boundaries/rules": [
  { from: "map",   allow: ["ui", "model"] },
  { from: "ui",    allow: ["model"] },
  { from: "model", allow: [] },
  { from: "app",   allow: ["map", "ui", "model"] },
]
```

---

## 2. The map component

The public API is the contract. Design it as if it will be consumed by three other
applications, because that is the stated goal.

```typescript
// packages/map/src/WebMap.tsx

export interface WebMapProps {
  /** MapLibre Style JSON. The single source of truth for appearance.
   *  Assembled server-side — never constructed ad hoc in the browser. */
  style: StyleSpecification;

  /** Initial camera. Uncontrolled after mount unless `view` is provided. */
  initialView?: MapView;

  /** Controlled camera. Supply with onViewChange for full control. */
  view?: MapView;
  onViewChange?: (view: MapView) => void;

  /** Layer metadata for legends, identify, and the layer tree.
   *  Keyed by MapLibre layer id. */
  layerMeta: Record<string, LayerMetadata>;

  /** Editing configuration. Omit for a read-only map. */
  editing?: EditingConfig;

  /** Overlay elements. Rendered as HTML above the canvas — so they are
   *  captured by the headless renderer's page screenshot. */
  overlays?: {
    legend?: boolean | LegendConfig;
    scaleBar?: boolean;
    northArrow?: boolean;
    titleBlock?: TitleBlockConfig;
    provenance?: ProvenanceConfig;
  };

  /** Feature click. Return false to suppress the default identify popup. */
  onFeatureClick?: (features: MapGeoJSONFeature[], lngLat: LngLat) => boolean | void;

  /** Fires when the map has settled — all tiles, glyphs, sprites loaded.
   *  The render shell keys off this. */
  onIdle?: () => void;

  /** Non-fatal problems: failed tile requests, missing glyphs. */
  onWarning?: (warning: MapWarning) => void;

  className?: string;
}

export interface MapView {
  center: [number, number];
  zoom: number;
  bearing?: number;
  pitch?: number;
}

export interface LayerMetadata {
  datasetId: string;
  name: string;
  kind: 'vector' | 'grid' | 'pointset' | 'fault_network';
  valueRange?: { min: number; max: number; unit?: string };
  paletteId?: string;
  editable?: boolean;
}
```

Deliberately **not** in the API: dataset fetching, authentication, API base URLs, routing. The
component receives a style and metadata. Where those came from is the app's problem.

### 2.1 Imperative handle

Some operations do not fit declarative props.

```typescript
export interface WebMapHandle {
  fitBounds(bounds: LngLatBoundsLike, options?: FitBoundsOptions): void;
  capture(): Promise<Blob>;         // see 06-rendering.md §8
  queryFeatures(point: PointLike, layerIds?: string[]): MapGeoJSONFeature[];
  getMap(): maplibregl.Map;         // escape hatch; document it as unstable
}
```

`getMap()` exists because we cannot anticipate everything, but every use of it in `apps/web`
is a signal that the public API is missing something. Track them.

### 2.2 Instance lifecycle

The commonest MapLibre-in-React bug is recreating the map on every render.

```typescript
export const WebMap = forwardRef<WebMapHandle, WebMapProps>((props, ref) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);

  // Create ONCE. Style and view updates go through separate effects that
  // mutate the existing instance.
  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: props.style,
      center: props.initialView?.center ?? [0, 0],
      zoom: props.initialView?.zoom ?? 2,
      // NOT set: preserveDrawingBuffer. Costs real performance on pan/zoom.
      // Capture uses the triggerRepaint trick instead — see capture.ts.
    });
    mapRef.current = map;
    return () => { map.remove(); mapRef.current = null; };
  }, []);   // eslint-disable-line react-hooks/exhaustive-deps -- intentional

  // Style updates: diff and apply, never recreate.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.isStyleLoaded()) return;
    applyStyleDiff(map, props.style);
  }, [props.style]);
  ...
});
```

`applyStyleDiff` uses MapLibre's `setStyle(style, { diff: true })` where possible. A full
`setStyle` drops all tiles and causes a visible flash — unacceptable when the user is nudging a
color ramp.

---

## 3. State management

Three distinct kinds of state. Do not conflate them.

| State | Tool | Examples |
|---|---|---|
| Server state | TanStack Query | Datasets, sessions, palettes, job status |
| Map/session state | Zustand | Layer order, visibility, symbology overrides, view |
| Ephemeral UI | React local | Panel open/closed, form drafts, hover |

```typescript
// apps/web/src/stores/sessionStore.ts

interface SessionState {
  sessionId: string | null;
  layers: SessionLayer[];
  view: MapView;
  dirty: boolean;

  addLayer(datasetId: string, opts?: Partial<SessionLayer>): void;
  removeLayer(layerId: string): void;
  reorderLayers(from: number, to: number): void;
  updateSymbology(layerId: string, symbology: Symbology): void;
  setView(view: MapView): void;
}

export const useSessionStore = create<SessionState>()(
  subscribeWithSelector((set, get) => ({ /* ... */ }))
);

// Autosave. Sessions are the shared vocabulary with Claude — if a geologist
// edits and then asks Claude to re-render, the server must already have the
// current state.
useSessionStore.subscribe(
  (s) => ({ layers: s.layers, view: s.view }),
  debounce(async (snapshot) => {
    await api.sessions.update(useSessionStore.getState().sessionId!, snapshot);
  }, 2000),
  { equalityFn: shallow }
);
```

### 3.1 Style derivation

The style is **derived**, never stored. Storing both a symbology model and a compiled style
guarantees they drift.

```typescript
const style = useMemo(
  () => compileStyle({ layers, layerMeta, palettes, basemapPrefs }),
  [layers, layerMeta, palettes, basemapPrefs]
);
```

`compileStyle` lives in `@webmap/style-model` and is the *same function* the backend runs (via
a shared JSON schema and a Python port with a shared test-vector suite). If the two ever
disagree, the visual regression tests catch it.

It is almost a per-layer map, with one exception worth knowing about: **label layers are held
back and appended after every object layer**, so nothing a later layer draws can cover a label
(`08` §2.4). Draw order is preserved among the labels themselves, and the basemap is exempt —
its place names stay beneath the geologist's data.

---

## 4. Visual design direction

Design brief for `apps/web`: a working instrument for subsurface geologists, not a consumer
product. The map is the interface; chrome exists to get out of its way.

**Grounding.** The subject is subsurface interpretation — contour intervals, fault throw,
well control. The visual vocabulary should come from that world: precise, monochrome-dominant
chrome so that *data* carries all the color, and typographic restraint so numbers read
unambiguously.

**Palette.** Chrome is near-neutral with a cool cast so it does not compete with map color
ramps, which span the full spectrum.

```typescript
// packages/ui/src/theme.ts
export const webmapTheme = createTheme({
  colors: {
    // Chrome greys with a slight blue cast — reads as "instrument", and
    // critically does not tint perception of adjacent map colours.
    slate: ['#f5f7f9','#e6eaee','#ccd4dc','#adb9c5','#8e9dad',
            '#75879a','#5f7286','#4a5b6d','#374553','#242e39'],
    // Single accent, used only for selection and active tools.
    signal: ['#e8f4f8','#c5e4ef','#9dd2e4','#6fbdd6','#48a9c8',
             '#2b93b3','#1e7a97','#15607a','#0e485c','#08313f'],
  },
  primaryColor: 'signal',
  fontFamily: 'Inter, system-ui, sans-serif',
  // Tabular figures throughout. Coordinates, elevations, and contour values
  // must align vertically in tables and readouts — proportional digits make
  // scanning a column of depths genuinely harder.
  fontFamilyMonospace: 'IBM Plex Mono, monospace',
  headings: { fontFamily: 'Inter, system-ui, sans-serif', fontWeight: '600' },
  defaultRadius: 'xs',
});
```

**Type.** One family. Numeric readouts use tabular lining figures — non-negotiable for a tool
where people compare columns of elevations.

**Restraint.** Spend boldness in one place — the data. Chrome gets one accent color, one
border weight, no shadows beyond a hairline on floating panels, and no entrance animation.
Motion answers user action (panel open, layer reorder) and nothing else.

**Layout** is covered separately in §5, because the desktop-first constraint drives enough
decisions to warrant its own section.

---

## 5. Desktop-first layout

The target is a geologist at a workstation: large monitor, mouse, keyboard. Per
`00-overview.md` §7.1, small viewports are not a supported target. **Do not write mobile-first
CSS.** Base styles describe the desktop layout; the only breakpoints scale *upward*.

### 5.1 Viewport policy

| Width | Behaviour |
|---|---|
| < 1280 px | Notice: "WebMap requires a window at least 1280 px wide." No degraded layout. |
| 1280–1439 | Functional. Two panels max open simultaneously. |
| 1440–1919 | Baseline. Design and review at this size. |
| ≥ 1920 | Optimal. Three panels plus the attribute table without occluding the map. |

Extra width buys **more visible at once**, never larger controls. A geologist on an ultrawide
should see the layer tree, symbology editor, and attribute table simultaneously — that is the
whole point of the screen.

```css
/* apps/web/src/styles/layout.css
   Desktop is the base case. Breakpoints add capability at larger sizes;
   there is no `max-width` query in this codebase. */
:root {
  --panel-w-left: 280px;
  --panel-w-right: 340px;
  --toolbar-h: 44px;
  --statusbar-h: 24px;
  --row-h: 26px;          /* dense list rows — pointer, not fingertip */
  --control-h: 28px;
  --hit-slop: 4px;        /* invisible padding around small targets */
}

@media (min-width: 1920px) {
  :root { --panel-w-left: 320px; --panel-w-right: 400px; }
}
```

### 5.2 Docked shell, not overlays

Single-document application. The map is the document; everything else docks around it.

```
┌────────────────────────────────────────────────────────────────┐
│  toolbar: project · tools · view · render                      │ 44px
├──────────┬──────────────────────────────────────┬──────────────┤
│  LAYERS  │                                      │  SYMBOLOGY   │
│          │                                      │              │
│  ▸ base  │                MAP                   │  ramp editor │
│  ▾ data  │           (fills remainder)          │  properties  │
│    ...   │                                      │              │
│          ├──────────────────────────────────────┤              │
│          │  ATTRIBUTE TABLE (resizable, hides)  │              │
├──────────┴──────────────────────────────────────┴──────────────┤
│  status: CRS · cursor coords · scale · job progress            │ 24px
└────────────────────────────────────────────────────────────────┘
```

Panels are **docked and resizable**, not floating overlays. Overlay panels obscure the map,
and the map is what the geologist is reading. Drag handles between regions; widths persist per
user in `user_preferences`. Each panel collapses to a 40 px icon rail, and collapsed state
persists too.

The status bar is always visible and always shows the analysis CRS, live cursor coordinates in
that CRS, and the current map scale. Geologists check these constantly; burying them in a menu
is a daily irritation.

### 5.3 Density

Aim for the density of a professional CAD or GIS application, not a consumer web app.

- List rows 26 px, not 40. A layer tree showing 20 layers without scrolling is more useful
  than one showing 10 comfortably.
- Mantine `size="xs"` or `"sm"` throughout; never `"md"` or larger for controls.
- Compact table rows with tabular figures; column widths sized to content.
- Panel padding 8–12 px, not 16–24.

### 5.4 Pointer and keyboard interaction

These are first-class because the input model is fixed. None require a touch fallback.

- **Hover** reveals row actions, shows feature tooltips, previews symbology changes.
- **Right-click context menus** on the map (identify, zoom to, copy coordinates), the layer
  tree (zoom to layer, duplicate, remove, properties), and the attribute table.
- **Drag-and-drop** for layer reordering, ramp stops, and dropping files onto the map to
  import.
- **Modifier keys**: Shift for range select, Ctrl/Cmd for multi-select, Alt to bypass
  snapping (`09-editing.md` §6.4), Space to temporarily pan while a draw tool is active.
- **Middle-click drag** pans; scroll wheel zooms at the cursor.
- **Double-click** on a layer zooms to its extent; on a vertex deletes it in edit mode.

```typescript
// apps/web/src/keyboard/shortcuts.ts
export const SHORTCUTS = {
  'mod+s':       'session.save',
  'mod+z':       'edit.undo',
  'mod+shift+z': 'edit.redo',
  'mod+f':       'search.datasets',
  'mod+k':       'command.palette',
  'mod+1':       'panel.layers.toggle',
  'mod+2':       'panel.symbology.toggle',
  'mod+3':       'panel.attributes.toggle',
  'mod+enter':   'render.current',
  '1':           'tool.select',
  'e':           'tool.edit',
  'i':           'tool.identify',
  'd':           'tool.measure',
  'f':           'view.zoomToLayer',
  'Escape':      'tool.cancel',
} as const;
```

Single-letter tool shortcuts follow the convention geologists already know from QGIS and
Surfer. A command palette (`mod+k`) covers everything else without growing the toolbar.

**`v` and `m` are not bound here.** They belong to the editing command registry, where `v` is
vertex-edit and `m` is move (`09-editing.md` §15) — the QGIS and ArcGIS bindings this section
claims to follow. This map previously bound them to select and measure, so the same two keys
meant different things depending on which document you read, while `09` §8 required that
shortcuts derive from the registry and never be registered independently. The app-level map
yields: select moved to `1` — which is what `09` §15 binds click-select to inside an edit
session, so the key means the same thing in both contexts — and measure moved to `d`.

**Inside an edit session the registry is the only registrar.** This map covers the application
shell; `09` §15 covers editing, and the two do not overlap.

### 5.5 Multi-window

Detachable panels are worth building once the core is stable — a second monitor holding the
attribute table while the map fills the first is a real workflow. Use the Popout/`window.open`
approach with shared state via `BroadcastChannel`, not an iframe.

Not Phase 2 work. Design panel components so they do not assume a parent layout context, and
this stays cheap to add later.

---

## 6. Layer tree

```typescript
// packages/ui/src/LayerTree/LayerTree.tsx

export interface LayerTreeProps {
  layers: SessionLayer[];
  layerMeta: Record<string, LayerMetadata>;
  selectedId: string | null;
  onSelect(id: string): void;
  onReorder(from: number, to: number): void;
  onToggleVisibility(id: string): void;
  onOpacityChange(id: string, opacity: number): void;
  onRemove(id: string): void;
  /** Basemap layers render in a locked group at the bottom. */
  basemapLayers: SessionLayer[];
}
```

Drag-to-reorder via `@dnd-kit/sortable`. Basemap group is visually distinct and not
reorderable relative to data layers — it is a user preference, not a per-session choice.

Each row shows a symbology preview swatch rendered from the compiled style, so the tree and
the map cannot disagree.

---

### 6.1 Managing layers, maps and basemaps

All three are ownable objects (`adr/0010`), so create, edit, duplicate and delete are the same
verbs on each and share one permission model. What differs is what they contain.

**A map is a basemap, its layers, and one active layer.** Only the active layer is editable;
Auto Zoom fits it, Zoom to Extents fits every layer in the map.

> **"Only the active layer can be edited" is an interface affordance and never an
> authorization boundary.** The API checks permission on every request regardless of what the
> client considers active. A client that made a view-only layer active is still refused by the
> service.

Auto Zoom with no active layer, or one whose extent is unknown, falls back to Zoom to Extents
rather than doing nothing — a button that silently does nothing reads as broken.

**Loading a basemap is always available** and adds only the layers not already present.
*Present* means the same `layer_id`: the same shapefile styled two ways is legitimately two
layers, and treating them as duplicates would silently drop one.

**Any map can be saved as a basemap**, which is a copy of its layer list. The dialog asks
whether to include the active layer — saving it means the next map built on this basemap opens
with last week's grid underneath it.

**Deleting a layer a basemap still uses is refused**, naming the basemaps. Two basemaps sharing
a layer is the point of the model; the sharing has to become visible at the moment it costs
something rather than afterwards, when someone else's map has quietly lost a layer.

**Refresh and replace** apply to any layer whose data came from a file or a connector:

- *Refresh* re-reads the source. Per `adr/0005` that produces a new dataset **version**, so
  maps referencing the layer follow the pointer with no edit of their own.
- *Replace* points the layer at a different dataset. If the new data lacks a column the
  symbology references, the layer breaks — so this validates at replace time and names the
  missing column. A silently blank layer is the failure to avoid.

### 6.2 Formatting dialogs

One dialog per layer, with sections that appear only where the geometry supports them — the
`SymbolSpec` union already prevents offering a fill colour for a line (§2.1 of `08`).

**Colour mode** applies to every outline and fill on the layer:

- **By column value.** For a numeric column, either *gradient* (stops, editable and
  importable) or *interval* (maximums only; the minimum of each band is the maximum of the one
  below, and the compiler closes both ends — see `08` §5.2). For a text column, a value/colour
  table with an **Other** row pinned to the bottom, defaulting to light grey.
- **Fixed.** One colour for everything.

The text-value picker is sorted by descending count of occurrences, which needs a distinct-
values query rather than anything MapLibre offers. It is **capped at the top few hundred**,
reports how many values remain uncounted, and the endpoint **refuses to enumerate a column
above a cardinality threshold** rather than hanging the dialog — a well-name column on 500k
features has 500k distinct values and no useful colour mapping.

**Transparency** applies to fills only. On a grid it is `raster-opacity`, which is the one part
of a grid's appearance MapLibre still controls.

**Line formatting** is colour, width and type. Type is a fixed choice, never by column value:
`line-dasharray` accepts no property expressions (`08` §2.2). A layer that needs dash-by-
category uses rule-based symbology instead.

**Text formatting** is family, weight, style, size in points, and size mode. The family list
comes from `GET /static/glyphs` so it reflects what this deployment actually built. **The
italic control is disabled when the family has none** — Oswald — rather than offered and
ignored.

**Size mode** is the reference-scale control, and both behaviours are implemented (`08` §2.2):

- **Fixed** — 12 pt stays 12 pt however far the map is zoomed. The default.
- **Scale with map** — 12 pt *at a reference zoom*, doubling in and halving out, so the text
  always covers the same distance on the ground. Picking this mode reveals the reference-zoom
  control, which is meaningless without it.

The preview has to zoom, not just render once: the difference between the two modes is
invisible in a still.

Sections the requirement did not name but the model needs, all already in `Symbology`:

- **Size by column** — graduated symbols. Bubble size by production or thickness is core
  geological visualisation and `Graduated.vary` already carries `'size'`.
- **Which column is labelled, and at what zooms.** With collision detection off (`08` §2.4)
  the zoom window is the *only* thinning control, so it is not an advanced option — it is how a
  section grid stops being a wall of text. Halo colour and width belong here too, starting at
  none — a default, not a rule, since over a colour-filled grid a halo is usually what makes
  the text readable at all.
- **Null and no-data colour.** *Other* catches unlisted text. It does not catch a numeric null,
  a value outside the gradient range, or a blanked grid cell — and a well with no porosity
  reading is not zero.
- **Automatic classification.** Equal interval, quantile, Jenks, standard deviation and pretty
  breaks are implemented (`webmap_core.style.classify`); the dialog generates stops with them
  and leaves the result editable.
- **Index contours.** "Every fifth contour heavier and labelled" is not expressible in a flat
  colour/line/text dialog — it is a rule on the `is_index` flag the contour job already emits,
  and it is the single most important piece of contour formatting.

Every dialog shows a **live legend preview**. `deriveLegend` already exists, and it is the
cheapest way to catch a palette that looks fine in the editor and illegible on the map.

### 6.3 Shared controls

Built as standalone components with no knowledge of layers, maps or this application, so they
can be lifted into another one. Each takes a value and an `onChange` and nothing else.

| Component | Used by |
|---|---|
| `ColorPicker` | Fixed colour, every stop, every category, Other, no-data |
| `RampEditor` | Gradient stops, with a histogram underlay (§5) |
| `IntervalEditor` | Interval maximums and their colours |
| `CategoryTable` | Text value/colour rows, sorted by count, Other pinned last |
| `LinePicker` | Width, dash pattern, cap and join, with a preview |
| `MarkerPicker` | Point shape, size, rotation |
| `FontPicker` | Family, weight, style — disables what the family lacks |
| `PaletteIO` | Import and export, `.clr` / `.cpt` / QGIS XML (`08` §5.1) |
| `LegendPreview` | Every mode, in every dialog |

---

## 7. Data loading

```typescript
// apps/web/src/api/datasets.ts

export const datasetKeys = {
  all: ['datasets'] as const,
  list: (filters: DatasetFilters) => [...datasetKeys.all, 'list', filters] as const,
  detail: (id: string) => [...datasetKeys.all, 'detail', id] as const,
};

export function useDataset(id: string) {
  return useQuery({
    queryKey: datasetKeys.detail(id),
    queryFn: () => api.get<DatasetDetail>(`/datasets/${id}`),
    staleTime: 5 * 60_000,
  });
}

export function useJobPolling(jobId: string | null) {
  return useQuery({
    queryKey: ['jobs', jobId],
    queryFn: () => api.get<Job>(`/jobs/${jobId}`),
    enabled: !!jobId,
    // **The server says how long to wait** — `poll_after_seconds` (`10` §5.1),
    // which varies by state. Honour it, so the web client and the MCP client
    // back off identically; the curve below is only the fallback for a
    // response that predates the field.
    refetchInterval: (query) => {
      const state = query.state.data?.state;
      if (state === 'succeeded' || state === 'failed' || state === 'cancelled') {
        return false;
      }
      const elapsed = Date.now() - (query.state.dataUpdatedAt ?? Date.now());
      return Math.min(1000 * Math.pow(1.5, elapsed / 10_000), 5000);
    },
  });
}
```

---

## 8. Session loading

The route Claude links to.

```typescript
// apps/web/src/routes/SessionRoute.tsx

export function SessionRoute() {
  const { shortCode } = useParams();
  const { data: session, isLoading, error } = useSession(shortCode!);

  // Progressive: show the map with basemaps as soon as we have the view,
  // then layers as their metadata resolves. A geologist arriving from a
  // Claude link should see something within a second, not a spinner for
  // the full load.
  if (isLoading) return <MapSkeleton />;
  if (error) return <SessionError error={error} shortCode={shortCode!} />;

  return <MapWorkspace session={session} />;
}
```

`SessionError` must handle the permission case specifically — a geologist following a shared
link to data they cannot access should see who to ask, matching the message from
`03-auth-security.md` §3.2 — alongside a short code that does not resolve, a session whose
datasets have been deleted, and one that has expired.

---

## 9. Geostatistics workbench

The controls for `13-kriging.md`. The map itself is already built — §2 renders the grids this
produces, `08` §5.2 colours them, and `05` §7 contours them. This section is everything
around it: the parameter, diagnostic and validation surfaces that turn a batch geostatistics
engine into something a geologist will explore.

### 9.1 Three tiers of interaction

**The single most important organising idea here.** Every control belongs to exactly one
tier, and the tiers must *feel* different. Getting this wrong is how tools like this become
unpleasant: if changing a P90 threshold spins a job queue, people stop exploring — and
exploration is the entire value of a distributional output.

| Tier | Cost | Controls | Pattern |
|---|---|---|---|
| **Free** | Recomputed in the browser from the stored local CDF. No server call. | Quantile, `P(Z > z)` threshold, reference scenario, colour ramp, stretch | Live sliders. **No Apply button.** Updates on drag. |
| **Cheap** | Re-krige the preview grid only. Under a second (`13` §11.3). | Neighbourhood (min/max n, radius, sectors), grid resolution, variogram overrides | Preview on drag; **Apply** promotes to full resolution. |
| **Expensive** | Full refit. A new job, a new dataset, a new lineage record. | Covariate set, trend form, target transform, threshold count and placement, declustering | Explicit **Run**. Produces a *new* result; never mutates the current one. |

**Why the free tier is genuinely free.** The trend enters the model as a location shift
(`13` §6), so changing the reference scenario is arithmetically adding a constant to every
node before reading the CDF. Quantiles and exceedance probabilities are lookups into
`LocalCDF.probs`, which the client already holds. None of it needs the server.

This is not a UI convenience — it is the reason the model was specified with a global trend
and a per-node CDF rather than as a surface. A design that made P10/P50/P90 three separate
server runs would produce the same maps and a tool nobody explores.

### 9.2 The local CDF on the client

`13` §4.4 persists the local CDF as a multi-band grid, one band per threshold. For the free
tier the client fetches the band stack once per run — the same permission check as any tile
request, no new storage.

Size is the constraint that decides the tier boundary. A 500 × 500 grid with 7 thresholds is
1.75 M values: **7 MB as float32, 3.5 MB as float16**. Acceptable once per run, and it makes
every derived surface instant.

- Above `SOFT_CELL_LIMIT` (4 M cells, `13` §11.0) the payload stops being reasonable and the
  free tier degrades: quantile and exceedance surfaces are derived server-side and delivered
  as ordinary tiles. **Say so in the UI** — a slider that was instant and becomes a spinner
  reads as a bug unless the reason is on screen.
- Compute quantiles and exceedances in a **Web Worker**, so a drag never blocks the main
  thread.
- Derived surfaces are never stored (`13` §4.4). Storing P50 beside the distribution it came
  from guarantees the two drift.

### 9.3 Charting: ECharts

A charting library is a new dependency for `apps/web`, and this is what justifies it: the
workbench needs a variogram plot with draggable model handles, small multiples, a heatmap, a
reliability curve and a PIT histogram. ECharts covers all of them, including the ones that
looked like they would need d3.

**The one non-trivial case** is draggable sill / range / nugget handles on the model curve:
the `graphic` component with `draggable: true`, positioning handles via
`chart.convertToPixel({ seriesIndex: 0 }, [x, y])` and reading drags back with
`convertFromPixel`. Imperative rather than declarative — roughly forty lines — but a
documented pattern rather than a fight. The same technique gives the draggable readout line
on the CDF inspector (§9.7).

| Chart | Feature |
|---|---|
| Variogram points sized by pair count | `scatter` with `symbolSize` as a function of the datum |
| Fitted model curve | `line`, `smooth: false`, densely sampled |
| Per-threshold indicator small multiples | Several `grid` objects in one instance |
| Correlation matrix | `heatmap` + `visualMap` |
| Shaded tail regions; PIT uniform reference band | `markArea` |
| 1:1 line on the Krige-slope scatter | `markLine` |
| Coefficient trajectory | `line`, `animation: false` for live updates |
| Large sample scatter | `large: true` with progressive rendering |

Three practical notes, each of which costs an afternoon if discovered late:

1. **Theme is fixed at init.** `echarts.init(dom, theme)` cannot be changed by `setOption`.
   Key the wrapper component on the Mantine colour scheme so React remounts it, or build
   colours from Mantine CSS variables at option-build time.
2. **Resize is manual.** Wire a `ResizeObserver` to `chart.resize()`. This matters most inside
   `Drawer` and `Tabs`, where a chart initialised while hidden gets zero dimensions and stays
   broken.
3. **Import from `echarts/core`**, registering only what is used. The barrel is about a
   megabyte, and `09-editing.md`-scale bundles are already the thing §10 watches.

Write one `<EChart option={} />` wrapper handling init, `setOption`, the `ResizeObserver`,
disposal and theme keying. After that, touch the instance API only in the variogram workbench
and the CDF inspector.

### 9.4 TrendBuilder

The most substantial component, and the one that saves the most time. Its job is to get
`f(X; θ)` **defined and proven sane** before anyone spends a run. Every failure in `13` §8.4
is cheaper to catch here than after a five-iteration GLS/REML loop.

**What makes it work:** on every change it runs a throwaway **OLS fit with declustering
weights** — one least-squares call, no covariance matrix, no kriging, sub-second for thousands
of samples. That yields real coefficients, real residuals and real diagnostics to render live,
instead of a form that validates syntax and nothing else. `POST /api/v1/trend/preview`,
debounced at ~300 ms.

**Step 1 — covariates.** Multi-select from the dataset's numeric columns. Live: correlation
heatmap, design-matrix condition number and per-covariate VIF, and the spatial-proxy check
(`13` §5.4). This is where a user discovers that proppant and fluid intensity are nearly
collinear — the moment to drop one is here, not after a run raises `COVAR_COLLINEAR`.

**Step 2 — form.** A `SegmentedControl` across three modes:

- *Preset* — cards for power, saturating, additive-saturating, Cobb-Douglas and log-linear,
  each with the formula rendered and a one-line description of the response shape.
- *Expression* — a text input parsed **server-side** through the sandboxed sympy path
  (`13` §8.3). A client-side parse is a convenience, never the source of truth. Live feedback
  shows the parsed expression, a chip list splitting free symbols into **covariates** (matched
  to selected columns) and **parameters** (everything else, which become θ), autocomplete over
  covariate names and the allow-listed functions, and inline errors. Run stays disabled until
  it validates server-side.
- *Import* — paste a discovered expression (`13` §9). With several seeds supplied, show them
  side by side: seed instability means the interpretability is illusory, and the user should
  see that here rather than meet `SR_UNSTABLE` three dialogs later.

**Step 3 — θ₀ and bounds.** Prefilled from the preset, collapsed by default.

**Step 4 — preview.** The part that earns the dialog:

- **Partial dependence curves**, one per covariate, evaluated over that covariate's observed
  range with the others held at declustered medians, with a **rug plot** of the data
  underneath and the region outside the covariate hull shaded. **Mark any sign reversal in the
  numerical derivative in red.** `TREND_NONMONOTONE` is instantly legible as a curve that
  turns over and nearly illegible as a warning string — and symbolic-regression expressions do
  this constantly.
- **Coefficient table** — estimate, standard error, and a flag on any coefficient whose
  interval spans zero or that sits in the smallest singular vectors of `JᵀJ`.
- **Observed vs predicted** with the 1:1 line, and **residual vs fitted** to catch
  heteroscedasticity the transform did not handle.
- **Residual bubble map thumbnail.** Not a real diagnostic — the proper standardised-residual
  map needs LOO kriging (`13` §12.1) — but coherent same-sign patches here are an early
  warning that the global trend will strain, and it costs nothing.
- **Variance share** — the fraction of declustered target variance the trend explains. If it
  is very high, say so now: `TREND_ABSORBED` is likely and the map will be regression-driven.

**Two boundaries to enforce in code.** TrendBuilder never fits a variogram, never runs the GLS
loop and never kriges — that constraint is what keeps it fast enough to be live. And its OLS
coefficients are a **preview, not the answer**: the real θ comes from the GLS/REML loop and
will differ, because the loop corrects exactly the clustering bias weighted OLS only
approximates (`13` §8.1). Label them as a preview, and after a run completes **show OLS and
GLS side by side** — a large gap is itself informative.

### 9.5 Variogram workbench

The one place experts will judge this tool.

- Empirical points **sized by pair count**, fitted curve overlaid. A variogram plot without
  pair counts invites trust in a tail built from nine pairs.
- Sill, range and nugget as both numeric inputs and draggable handles (§9.3).
- A permanent readout of the **auto-selected value beside any override**, with one-click reset.
- `Tabs`: omnidirectional | directional | variogram map. The directional tab overlays the
  azimuths with the fitted ellipse and **states the bootstrap p-value** (`13` §7.6), because an
  anisotropy azimuth quoted without it looks like a measurement.
- **For RIK, a small-multiples strip of per-threshold indicator variograms**, plus a chart of
  fitted range against threshold quantile. That second chart is the entire argument for
  indicators — if range grows with threshold, connected highs are real. Put it on screen.
- Model family selector showing the REML likelihood of each candidate, so selection is visible
  rather than magic.

**Dragging fits by WLS; Apply runs REML** ([`adr/0011`](adr/0011-reml-for-trend-residual-variograms.md)).
REML is an optimisation with a Cholesky per evaluation and cannot live under a slider.

### 9.6 Flag panel

Given that `13` §1 makes findings text-first with figures as evidence, this is arguably more
important than the map.

- A persistent docked panel, grouped by severity.
- Each flag: the code as a `Badge`, the message, and two actions — **jump to figure**, **jump
  to the parameter that caused it**.
- `TREND_ABSORBED`, `COVAR_CONFOUND` and `TREND_ONLY_WINS` render as **full-width alerts above
  the map**, not list items. These are the findings a non-expert cannot recover unaided.

### 9.7 Local CDF inspector, and the reference scenario

Click a grid node → a panel showing the recovered CDF and PDF at that node, the kriged
threshold positions marked, **the extrapolated tail regions shaded distinctly** with the tail
model and ω named in place, and a draggable vertical line reading out `P(Z > z)` and the
quantile at the cursor.

This is what makes a distributional output comprehensible to someone who has only seen
single-value maps, and it is pure client-side arithmetic.

The **reference scenario dialog** is a slider per covariate, each bounded by the observed range
with the region outside the covariate hull shaded, plus a *typical* preset setting everything
to declustered medians. Live map update, free tier. Give it prominence rather than burying it
in settings — *"the target at a reference configuration"* is the question users actually have.

### 9.8 Data health, progress, validation, attribution

**Data health** runs before anyone commits to a job: declustering weight map, covariate
correlation heatmap with the condition number, spatial-proxy table, and the target histogram
before and after transform.

**Job progress** uses the existing polling contract (`10-jobs-async.md`) — the response says
how long to wait, and the same contract serves MCP. What changes is the rendering: a
`Timeline` with the real stages from `13` §14.3 rather than a spinner, because the trend loop
and per-threshold kriging give genuine structure. **Draw the coefficient trajectory live**
during the trend iterations; it doubles as the `13` §8.4 diagnostic, so it is free.

> Server-Sent Events would give smoother progress than polling. They are not adopted here:
> `10-jobs-async.md` specifies polling with a backoff hint, MCP depends on that contract, and
> a progress bar updating once a second is not the bottleneck. Revisit as a change to `10`,
> not as a second progress mechanism beside it.

**Validation panel:** the baseline table at the top, then the PIT histogram with a uniform
reference band, the CRPS bar chart across the four baselines, the reliability curve, and the
Krige-slope scatter with a 1:1 line. **If trend-only wins, state it in plain language above
everything else** (`13` §13).

**Attribution panels:** the four-way joint classification (`13` §12.3) as a map layer with a
real legend — ideally interactive, clicking a quadrant to isolate it; the confound report as a
table, one row per covariate, one number, sorted descending; and the trend-versus-residual
variance ratio as a diverging ramp centred at 1, swipeable against P50.

### 9.9 Run manager

A table of runs — target, trend form, covariates, key metrics, flag counts. Select two to
diff: **a lineage diff showing only what changed**, side-by-side or swipe maps, and the metric
deltas.

Without this, parameter exploration is unfalsifiable — a user changes four things, likes the
result, and cannot say which change did it.

Because the run configuration and the lineage record are the same shape (`13` §4.6), export is
a serialisation and import is a hydration. **Import needs two guards, both required:** a
schema-version check that fails cleanly rather than half-loading, and a **dataset hash check**
that warns prominently when the parameters were recorded against different data. Show an
import summary before applying, and offer *apply parameters only* so a user can inspect before
committing to an expensive tier.

### 9.10 Cross-cutting rules

- Every auto-selected parameter carries a visible **"auto" badge** and a one-click reset. If
  users cannot tell what they have overridden, they will override things by accident and not
  know why the map changed.
- Any quantile flagged `LocalCDF.tail_dependent(q)` carries an inline warning **wherever it
  appears, including in the layer name**. P95 and P99 are the numbers people quote in meetings
  and the numbers most determined by an assumption (`13` §10.4).
- **Masked areas must be visually distinct from low values** — a hatch or stipple, not the
  bottom of the ramp — with the reason in the legend ("beyond 1× variogram range from any
  sample"). A hole that looks like a rendering bug gets reported as one. This is a requirement
  on `08` §5.2's grid rendering, not on this panel.
- **The colour scale must be lockable across runs.** Auto-restretching per run silently makes
  every map look equally decisive, which defeats run comparison — the same argument as the
  display range in `08` §5.2.

### 9.11 Build order

1. The `<EChart>` wrapper, and the run-configuration store shaped to the lineage record.
2. Data health panel — the first real screen, and it needs no estimator.
3. TrendBuilder steps 1–3 against `/trend/preview`.
4. TrendBuilder step 4 previews. **First genuinely valuable milestone.**
5. Run submission, polling and result caching.
6. Free-tier controls: quantile, exceedance, scenario, with the Web Worker CDF math.
7. Local CDF inspector.
8. Flag panel.
9. Variogram workbench — read-only, then overrides, then draggable handles.
10. Validation and attribution panels.
11. Run manager and diff.
12. Report download and parameter export/import.

Milestone 6 is where the tool starts feeling live; milestone 8 is where it starts being
trustworthy.

---

## 10. Performance

- **Never put >5,000 features through a GeoJSON source.** The switch to MVT is automatic and
  decided server-side (`06-rendering.md` §7.1). The client honors what the style says.
- **Debounce symbology edits at 150 ms** before recompiling the style. Dragging a ramp stop
  should not trigger 60 style diffs per second.
- **Virtualize the attribute table.** `@tanstack/react-virtual`. Some datasets have 500k rows.
- **Lazy-load the editing bundle.** Terra Draw and the topology tools are a meaningful chunk
  and most sessions are read-only.

```typescript
const EditingTools = lazy(() => import('@webmap/map/editing'));
```

---

## 11. Accessibility floor

Not optional, and cheap if done from the start.

- Keyboard navigation for every panel, the layer tree, and all form controls.
- Visible focus rings; never `outline: none` without a replacement.
- Map keyboard controls enabled (MapLibre supports arrow-key pan, +/- zoom).
- Color is never the only channel: legends pair swatches with values, layer visibility uses an
  icon plus state, and validation errors carry text.
- `prefers-reduced-motion` respected — disables the panel transitions.
- Live region announcing job completion, since gridding finishes while attention is elsewhere.

Desktop density raises two obligations that a roomier layout would not:

- **Every hover-revealed action has a keyboard path.** Row actions that appear on hover must
  also appear on focus, and must be reachable in tab order. A hover-only affordance is
  invisible to keyboard users.
- **Every right-click context menu is also on `Shift+F10` / the context-menu key**, with
  identical contents. Right-click-only functionality is inaccessible.

Small targets are acceptable here — the input model is a mouse — but use `--hit-slop` padding
so the clickable area exceeds the painted area. Precision at 26 px rows depends on it.

---

## 12. Testing

| Layer | Tool | Scope |
|---|---|---|
| Style compilation | Vitest | `@webmap/style-model`, shared test vectors with Python |
| Components | Vitest + Testing Library | `@webmap/ui` in isolation |
| Map component | Vitest + mocked MapLibre | Prop → instance-call assertions |
| Integration | Playwright | Session load, layer add, symbology edit, export |
| Visual | Playwright screenshots | `06-rendering.md` §10 |

The style-compilation test vectors are shared with the Python implementation. Same inputs,
same expected Style JSON, run in both languages. This is the mechanism that keeps the two
compilers honest.
