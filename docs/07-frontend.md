# 07 — Frontend

React 19 + TypeScript + Vite. Mantine 7 for UI primitives. MapLibre GL JS for mapping.

---

## 1. Package structure

```
packages/
├── style-model/          @webmap/style-model   (no React, no MapLibre)
│   └── Symbology types + compilers to MapLibre Style JSON
├── ui/                   @webmap/ui            (React + Mantine, no MapLibre)
│   └── Ramp editor, style property editor, legend, scale bar
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
  snapping (`09-editing.md` §3), Space to temporarily pan while a draw tool is active.
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
  'e':           'tool.edit',
  'v':           'tool.select',
  'i':           'tool.identify',
  'm':           'tool.measure',
  'f':           'view.zoomToLayer',
  'Escape':      'tool.cancel',
} as const;
```

Single-letter tool shortcuts follow the convention geologists already know from QGIS and
Surfer. A command palette (`mod+k`) covers everything else without growing the toolbar.

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
    // Back off as the job runs; gridding takes 20-90 s and hammering the
    // endpoint every second is pure waste.
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

## 9. Performance

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

## 10. Accessibility floor

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

## 11. Testing

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
