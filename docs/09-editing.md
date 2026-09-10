# 09 — Vector Editing

MapLibre has no editing. This document specifies the whole geometry editing subsystem: drawing,
selection, vertex editing, snapping, topological editing, the operations catalog, and the
command surfaces that expose them.

Out of scope here: identity, tile serving, basemaps (`10`, `06`, `08`), and layer attribute
*schemas* — though attribute *handling* during edits is specified in §14.

---

## 1. Scope, honestly stated

Editing gets hard fast, and the hard parts are snapping and topology, not drawing.

**In scope:**

- Create, modify, delete point / line / polygon features
- Multi-feature selection, and vertex-level selection within it
- Vertex editing: add, move, delete, nudge, numeric and bearing entry
- Snapping to vertices, edges, intersections and midpoints, across layers
- **Topological editing within the active layer** — see [`adr/0013`](adr/0013-topological-editing-within-the-active-layer.md)
- The operations catalog of §11: split, reshape, combine/explode/dissolve, overlay, smooth,
  simplify, buffer, align, validate
- Attribute editing, including bulk edit across a selection
- Geometry validation with actionable, located errors
- Undo/redo over multi-feature commands
- Copy-on-write versioning with conflict detection; every retained version addressable — which is every version for 30 days, then one per day (§13)

**Still deferred, deliberately:**

- **Real-time collaborative editing** — two people on one layer with live cursors. Concurrent
  commits are *detected* and reported per feature (§5.3), never merged.
- **Cross-layer topological propagation.** Propagation is scoped to the active layer, because
  the alternative silently edits a layer the user did not make editable (§7.1). Cross-layer
  coincidence is created by Align (§11.2), which is where it belongs.
- **A true topological data model** — a stored node/edge graph, as distinct from the coordinate
  coincidence of §7. Nothing here maintains topology *between* sessions.
- **Automatic gap and sliver removal across a coverage.** Validate detects them and offers a
  bounded auto-fix (§12.2); nothing removes them silently.

> **What changed, and why it is not a reversal of the old rule.** An earlier revision of this
> section deferred "full planar topology" outright, on the argument that a polygon editor which
> silently permits slivers is worse than no editor. That argument was right and still holds —
> which is why validation still blocks a save that would introduce one. What §7 adds is much
> narrower than what was deferred: a coordinate coincidence index over the active layer, off by
> default, at a tolerance three orders of magnitude tighter than snapping. It maintains
> coincidence that already exists. It does not construct topology, and it does not claim to.

---

## 2. Technology decisions

### 2.1 Terra Draw, scoped to creation only

Terra Draw (`terra-draw` + `terra-draw-maplibre-gl-adapter`) is used for **new geometry
creation only** — polygon, linestring, point, rectangle and freehand modes.

Terra Draw does **not** own selection state, vertex editing of existing features, or the
feature store. See [`adr/0014`](adr/0014-terra-draw-scoped-to-creation.md) for the full
argument; the short version is that its select mode is single-feature, multi-select is an open
upstream request, and its snapping only targets its own internal store rather than arbitrary
MapLibre layers. Every one of those is load-bearing for the multi-feature Edit menu in §9.

Where Terra Draw is used, our snap engine feeds it through `snapping.toCustom`, which takes a
function returning a position synchronously.

**The exit condition is written down on purpose.** If implementation finds it needs custom
Terra Draw modes for selection, splitting or vertex editing, **drop Terra Draw entirely** — at
that point the library is being reimplemented and the adapter is providing negative value.

### 2.2 UI components

| Need | Component |
|---|---|
| Menu bar | Composed from `Menu` instances — **Mantine has no `Menubar`** |
| Nested menus | `Menu.Sub` / `Menu.SubTarget` / `Menu.SubDropdown` |
| Menu toggles and radio groups | `Menu.Item` with our own checked-state rendering, under `closeOnItemClick={false}` — **Mantine has no `Menu.CheckboxItem` or `Menu.RadioGroup`** |
| Command palette | `@mantine/spotlight` |
| Keyboard shortcuts | `useHotkeys` from `@mantine/hooks` |
| Mutually exclusive tool modes | `SegmentedControl` |
| Toolbar button clusters | `ActionIcon.Group` |
| Snap / topology settings | `Popover`, split-button pattern |
| Shortcut display | `Kbd` in a `Menu.Item` `rightSection` |
| Async operation feedback | `@mantine/notifications` |
| Context menu | `Menu` positioned at the click point |

**Three of these were specified against components that do not exist** in the Mantine this
project pins (`@mantine/core` 7.17): `Menubar`, `Menu.CheckboxItem`, and
`Menu.RadioGroup`/`Menu.RadioItem`. `Menu.Sub` does exist. The checked-item behaviour those
were chosen for — not closing the menu on click — comes from `closeOnItemClick={false}`, so
nothing is lost beyond having to render the check mark ourselves.

**New dependencies this section requires**, none currently installed: `@mantine/spotlight`,
`@mantine/notifications`, `terra-draw`, `terra-draw-maplibre-gl-adapter`, and a client geometry
library (§2.3). Control sizes stay `xs` or `sm` per `CLAUDE.md` §5.1.

### 2.3 Geometry engines

**Client:** `@turf/turf` for lightweight display-side geometry — bbox, point-in-polygon,
measurement readouts. **Never for geometry that will be committed**; that is the §3.3 rule and
it is what keeps the tile approximation out of the store.

**Server: DuckDB and Shapely. There is no PostGIS.**

| Engine | Role |
|---|---|
| **Shapely** | Per-feature work in the request path: split, reshape, smooth, simplify, buffer, overlay, single-feature validity and repair. |
| **DuckDB** (`spatial`) | Set-based and bulk work: whole-layer validation scans, spatial joins to find candidate pairs, gap and overlap detection, aggregate unions, GeoParquet read and write. The layer never enters Python. |

**Routing rule.** Choose by the *shape of the work*, not by preference. One feature or a
handful, synchronously → Shapely. A scan, join or aggregate across a layer → DuckDB. This is
`adr/0004` applied to editing.

**Why there is no PostGIS fallback.** The source specification named PostGIS for four
operations. WebMap runs **plain Postgres** — [`adr/0002`](adr/0002-duckdb-data-plane.md) chose
it deliberately, and nothing in the control plane is spatial except `dataset.bbox_4326`, which
is four floats. [`adr/0004`](adr/0004-geoprocessing-owns-geometry.md) then moved geometry
operations out of SQL entirely, because "where does geometry get transformed?" having three
answers is the condition under which a CRS bug hides. Adding PostGIS for four functions would
reverse both.

All four have Shapely equivalents, and they are not workarounds:

| Wanted | Instead |
|---|---|
| `ST_Snap` | `shapely.ops.snap`, per candidate feature. The expensive half is finding candidates, and that is a DuckDB spatial join (§11.2). |
| `ST_SharedPaths` | Intersect the two boundaries; the linear components of the result are the shared paths. |
| `ST_Node` | `shapely.ops.unary_union` over the line set nodes it at every intersection. Watch memory on a whole layer — chunk by tile if it bites. |
| `ST_IsValidDetail` | `shapely.validation.explain_validity` returns the reason *and* the location, which is what §12 already parses. |

If a fifth operation ever has no good answer in either engine, that is an ADR, not a quiet
dependency.

### 2.4 Precision across engines

Geometry crosses boundaries — DuckDB → Python → Shapely → the wire → the client. Every hop can
lose precision, which defeats the exact-coordinate work in §6.6.

- Pass geometry between engines as **WKB**, never WKT or GeoJSON text, wherever both sides
  support it.
- Where GeoJSON is unavoidable, serialise at full `float64`. **Do not round for readability.**
- **A round-trip precision test belongs in CI**: load a feature, pass it through every engine
  hop, assert bit-identical coordinates on return. Without it this rule decays into a comment.

---

## 3. Core data model

### 3.1 Layers and the active layer

```ts
interface EditableLayer {
  id: string;
  name: string;
  geometryKind: 'point' | 'linestring' | 'polygon' | 'mixed';
  sourceId: string;
  layerIds: string[];              // the compiled MapLibre layers (08 §3)

  // Orthogonal flags, not one enum. A layer can be visible and not selectable,
  // or snappable and not visible (§6.4).
  visible: boolean;
  selectable: boolean;
  snappable: boolean;

  snapConfig: LayerSnapConfig;     // §6.2
}
```

**Exactly one layer is the active edit target.** This is the same invariant `07` §6.1 states
for the map — and the same warning applies: *it is an interface affordance and never an
authorization boundary.* The API checks permission on every request regardless of what the
client considers active.

The active layer is the only one whose features may be modified, the only one new features are
created into, forced `visible` and `selectable`, and the scope for topological editing (§7).

**Selection is scoped to `selectable` layers, and this must be surfaced.** A user looking at
five visible layers will click any of them and be confused when nothing selects. The status
strip (§10.4) says which layer is active and how many features are selected, for exactly this
reason.

Selectable-but-not-active layers may be selected for **read-only** operations — copy, zoom-to,
inspect attributes. Any mutating command is disabled when the selection contains a feature
outside the active layer, and the disabled tooltip says so.

### 3.2 Selection: two independent scopes

Not optional. The Edit menu operates on features; vertex delete operates on vertices.

```ts
interface SelectionState {
  features: Set<FeatureId>;
  vertices: Set<VertexRef>;        // only populated in vertex mode
}

interface VertexRef {
  featureId: FeatureId;
  ring: number;                    // 0 = exterior ring or linestring; 1+ = interior
  index: number;                   // ordinal within the ring
  part?: number;                   // multi-part index
}
```

Vertex selection is meaningful only in vertex mode and is cleared on leaving it. **Feature
selection survives mode changes**, because losing it on every mode switch makes a multi-step
edit intolerable.

### 3.3 Three representations of the same geometry

Confusing these is the primary source of bugs in this subsystem, so they are named.

| Representation | Source | For | Never for |
|---|---|---|---|
| **Tile geometry** | `queryRenderedFeatures` | Hover, hit testing, snap *preview* | Committing an edit |
| **Exact geometry** | Backend, cached client-side | Every committed coordinate; snap resolution on pointerdown | Bulk hover work |
| **Dirty geometry** | The local edit buffer | Rendering pending edits, the undo stack | — |

```ts
interface EditSession {
  activeLayerId: string;
  exactCache: Map<FeatureId, Feature>;
  dirty: Map<FeatureId, FeatureDelta>;
  undoStack: Command[];
  redoStack: Command[];
  baseVersion: number;             // the dataset version read at session start (§5.3)
}
```

**Rendering pending edits.** Dirty features render over the tile layer: keep a GeoJSON source
(`edit-overlay`) holding them, and add a MapLibre filter on the base layer excluding those
feature ids, so the stale tile version does not show through underneath.

### 3.4 Commands

Every mutation is a command, and a command carries a **set** of feature deltas. Topological
editing (§7) mutates N features per gesture and undo must treat that atomically.

```ts
interface Command {
  id: string;
  label: string;                   // "Move Vertex" — shown in the undo tooltip
  deltas: FeatureDelta[];
  timestamp: number;
}

interface FeatureDelta {
  featureId: FeatureId;
  before: Feature | null;          // null = created
  after: Feature | null;           // null = deleted
}
```

`apply` writes `after` into the dirty buffer, `invert` writes `before`. Redo is cleared on any
new command, and on a failed flush — replaying forward from a state the server never accepted
produces a layer nobody authored.

**Built** as `apps/web/src/editing/session.ts`, ahead of any mutating operation as §14's
sequence requires. Five decisions:

**Undoing back to the start leaves the session clean.** `restore` compares the restored feature
against the exact cache and *drops it from the buffer* when they match, rather than writing it
back. Without that, Save stays enabled with nothing to send and the tab warns about unsaved
changes that no longer exist. "Unchanged" is therefore decided by comparison, not by counting
operations — a move and a move back is two commands and zero changes.

**A pending delete is `null` in the buffer, not an absent key.** An absent key is an untouched
feature, and a delete stored that way is a delete that never reaches the server.

**`pendingDeltas` is derived from the buffer, not accumulated.** A feature moved four times is
one delta from its original; the server writes a whole immutable object either way
(`adr/0005`), and four deltas for one feature is four chances to disagree about the order.

**Commit clears both stacks.** After a commit the previous state is a version on the server,
and undoing into it would leave a local buffer that disagrees with a `baseVersion` the session
has already moved past. A rebase clears them for the same reason: an entry referencing a
feature just replaced underneath would restore a `before` that is nobody's state.

**The snapshot leaves out the exact cache.** It is a copy of what the server holds and can be
fetched again; persisting it would multiply the stored size by the size of the layer, and
§5.4's recovery rebases onto whatever the layer holds *now* — `isStale` is what tells the
recovery prompt the version moved while the tab was closed.

---

## 4. Mode state machine

Exactly one mode is active, and it is always visible in the toolbar (§10.1).

```ts
type EditMode =
  | 'select'
  | 'vertex' | 'vertex-add'
  | 'move'
  | 'draw-polygon' | 'draw-line' | 'draw-point' | 'draw-rectangle' | 'draw-freehand'
  | 'split' | 'reshape'
  | 'paste-place';

type SelectTool = 'click' | 'rectangle' | 'lasso';   // sub-state of 'select'
```

**Transition rules.**

- `Esc` always exits to `select`. With a modal operation running (§5.1), the first `Esc`
  cancels the operation and the second returns to `select`.
- Feature selection survives a mode change; vertex selection does not.
- Entering a draw mode clears feature selection.
- A running modal operation blocks a mode switch — either auto-apply or prompt, consistently.
- **Switching the active layer clears both selection scopes and cancels any operation.** It
  also ends the edit session, which is why §5.2 requires the dirty buffer to be saved or
  discarded first.

**Built** as `apps/web/src/editing/modes.ts` — a reducer, because the content of this machine
is its transitions: a mode is a string, and what makes it a state machine is what each
transition does to the selection and the operation.

Two things beyond the rules above, both decided by writing the tests:

**A vertex selection made outside a vertex mode is ignored, not stored.** Stored, it is a
ghost — invisible now, and back on the next mode change pointing at whatever was selected
minutes ago. And **changing the feature selection clears the vertex selection**, because those
vertices belonged to the features that were selected.

**A blocked mode switch is blocked, not auto-applied.** §5.2 says pick one and hold it, and
the one that cannot silently write to the dirty buffer is the one that cannot surprise anybody.
`canSwitchMode` is exported so the toolbar greys the buttons rather than letting a user click
one and watch nothing happen.

`selectionScope` derives what the command registry reads rather than storing it. Two fields
that could disagree about what is selected is the shape that produces a Delete which deletes
the wrong thing.

---

## 5. Two levels of commitment

Preserved in the UI. Conflating them makes undo granularity ambiguous.

### 5.1 Operation level — Apply / Cancel

Transient state of one tool while it runs: the in-progress split polyline, a dragged-but-not-
dropped vertex, pasted features awaiting placement, an uncommitted smoothing factor.

Nothing has been written to the dirty buffer, and **nothing enters the undo stack.** `Esc`
cancels with no confirmation, because nothing is lost; `Enter` applies. A **contextual
operation bar** (§10.2) shows the parameters and Apply / Cancel, and disappears the instant the
operation resolves.

### 5.2 Session level — Save / Discard

Applied operations not yet persisted.

One applied operation = one dirty-buffer write = **one undo entry**. Save is a single backend
transaction: if any feature fails validation or the version check, nothing lands. Discard
requires confirmation and clears the buffer and both stacks.

Save is disabled while an operation bar is showing — or auto-applies first. Pick one and hold
it; alternating is what makes a tool feel unpredictable.

### 5.3 Concurrency — the pointer commits, the diff explains

**Optimistic concurrency lives on the dataset version pointer and only there.**
[`adr/0005`](adr/0005-single-editor-persistence.md) settled this: features are immutable
GeoParquet objects, so two editors cannot corrupt each other's writes — they produce two
separate objects and contend on one pointer. A version column on every feature and a conflict
path per row buys nothing the immutable objects have not already bought.

The client sends the `baseVersion` it read at session start. The commit is:

```sql
UPDATE dataset
SET parquet_key = :new_key, version = version + 1, updated_at = now()
WHERE id = :dataset_id AND version = :expected_version
RETURNING version;
```

Zero rows means someone committed while this batch was in flight.

**On conflict, the 409 names the features, not just the version.** Both versions are immutable
objects that still exist, so the server diffs them and returns the ids that actually changed
underneath — no version column required, because the answer was already in storage. The UI then
offers **Refresh** (discard local changes to those features and rebase the rest) or **Force**
(overwrite). Never silently overwrite.

This is the useful half of a per-feature conflict model at none of its cost, and it is the
second amendment to `adr/0005`.

### 5.4 Crash durability

Mirror the dirty buffer and undo stack to IndexedDB on every applied operation, debounced at
~500 ms. On load, if a dirty buffer exists for the layer, offer recovery.

A geologist doing a forty-five minute boundary cleanup must not lose it to a browser refresh.
This is cheap and it is the difference between a tool people trust with real work and one they
do not.

---

## 6. Snapping engine

The feature that decides whether the editor is usable. Without it every shared boundary is a
source of slivers.

### 6.1 Algorithm — screen space

**All snap math happens in pixels.** This avoids geodesic distance entirely and makes tolerance
handling trivial. An earlier revision of this section converted a ground tolerance to degrees
per operation at the current latitude; that works, but it puts a trigonometric conversion in the
inner loop of a 60 Hz path to reach the same answer.

On pointer move, throttled to `requestAnimationFrame`:

1. Compute pixel tolerances from the configured ground tolerances (§6.3).
2. `map.queryRenderedFeatures(bbox, { layers: snappableLayerIds })`, the pointer expanded by
   `max(vertexPx, edgePx)`.
3. **Vertex pass first.** Project each coordinate, take squared pixel distance, keep the minimum
   under `vertexPx`.
4. **Edge pass only if no vertex hit.** For each consecutive pair, the clamped perpendicular
   foot; keep the minimum under `edgePx`.
5. **Vertex always beats edge**, which falls out of checking vertices first — do not compare the
   two distances against each other.
6. `map.unproject` the winner.

```ts
interface SnapResult {
  lngLat: LngLat;
  type: 'vertex' | 'edge' | 'intersection' | 'midpoint';
  featureId: FeatureId;
  layerId: string;
  vertexRef?: VertexRef;
  segmentIndex?: number;
  isExact: boolean;                // false when still tile-derived (§6.6)
}
```

Squared distances throughout — no `Math.sqrt` in the inner loop — and a cheap bbox rejection per
segment before the perpendicular math.

### 6.2 Snap types

```ts
interface LayerSnapConfig {
  vertex: boolean;                 // default true
  edge: boolean;                   // default true
  intersection: boolean;           // default false
  midpoint: boolean;               // default false
  vertexToleranceFt: number;
  edgeToleranceFt: number;
}
```

Global: `snapEnabled` (toolbar master toggle) and `snapToSelf` (other vertices of the feature
being edited, default true).

**Priority: vertex → intersection → midpoint → edge.** An intersection is a more meaningful
place to land than an arbitrary point on an edge, and a geologist digitising a fault network
depends on it.

### 6.3 Tolerance, and the unit the user thinks in

The user configures **feet**, because a geologist says "snap within 50 feet". The engine works
in pixels. Convert once per frame:

```ts
const metersPerPixel = 156543.03392 * Math.cos(lat * Math.PI / 180) / Math.pow(2, zoom);
const px = clamp(feet * 0.3048 / metersPerPixel, MIN_PX, MAX_PX);   // 4, 20
```

A fixed ground tolerance degrades badly across zoom: 10 ft is roughly 6 px at z18 and well
under 1 px at z15, at which point **snapping silently stops working**. The clamp prevents that.

**Surface the degradation.** When the unclamped tolerance falls below `MIN_PX`, badge the snap
control to say the pixel floor is in force rather than the configured distance. A silent
behaviour change generates bug reports; a badge generates none.

### 6.4 Exclusions

- The vertex being dragged **and its two ring neighbours** are excluded. Without this the drag
  handle sticks to itself.
- Dirty features are snapped against their **dirty** geometry, not their tile geometry.
- MapLibre excludes `visibility: none` layers from `queryRenderedFeatures`. To make a layer
  snappable but invisible, use zero opacity instead — zero-opacity layers are still returned.

### 6.5 Performance

In priority order, and the order matters — the first item is worth more than the rest combined:

1. **Throttle to `requestAnimationFrame`.** Raw `pointermove` fires far more often than paint.
2. **Cache projections during a drag.** The camera does not move mid-drag, so projections are
   stable. Build the candidate set on drag start; invalidate on map `move`.
3. **A spatial index only above ~50k segments in view.** Build a `flatbush` index of segment
   bounding boxes on `moveend`; a pointer move is then one bbox query. Below that threshold the
   naive loop beats the rebuild cost. **Do not build this preemptively** — an earlier revision
   of this section specified an always-on RBush index, which is work for nothing at the sizes
   most edit sessions actually see.

Budget: the full snap pass under **4 ms**, to hold 60 fps alongside rendering.

### 6.6 Exact coordinate resolution

Tile geometry drives the *visual* indicator; exact geometry drives every *committed* coordinate.

**Tile configuration is the primary mitigation, not the resolution protocol.** Serve snap-target
layers **unsimplified at editing zooms** (z15+): `tolerance: 0` on GeoJSON sources, simplification
disabled above the minimum edit zoom in the tile generator, and a larger tile `buffer` to reduce
seam clipping. With that configured, tile geometry equals source geometry apart from tile-boundary
clipping and the two agree by construction — far less code than reconciling them afterwards.

For the residual cases:

1. **Prefetch on hover.** A candidate against a feature not in `exactCache` fires an async fetch.
   Do not block the indicator on it.
2. **Resolve on `pointerdown`, not on drop.** Re-run the snap against exact geometry the moment
   the gesture starts. Waiting until commit produces a visible jump.
3. **Never match by vertex index.** Simplification drops vertices, so index *i* in the tile is
   not index *i* in the source. Match by coordinate proximity with a tight epsilon (1e-7 deg).
4. **Fail loudly on ambiguity.** Two exact vertices within epsilon of the tile vertex → abort the
   snap and log. Do not guess.
5. **Prefer server-provided identity.** The exact-geometry endpoint returns `(feature_id, ring,
   ordinal)` alongside coordinates, so resolution is a lookup rather than a geometric match.
6. **Edge snaps cannot be resolved by lookup** — there is no exact coordinate to match. Recompute
   the perpendicular foot against the exact geometry.

Set `isExact` accordingly and render the indicator differently — hollow while tile-derived,
filled once exact. Useful in development, harmless in production.

### 6.7 Indicator rendering

A dedicated GeoJSON source with one point feature, updated by `setData`. Style by type: square
for vertex, circle for edge, X for intersection, triangle for midpoint. **Visual feedback is
mandatory** — without it users cannot tell whether a snap occurred and stop trusting the tool.

---

## 7. Topological editing

A toolbar toggle, **off by default**, persisted per project. When on, an edit propagates to
coincident vertices. See [`adr/0013`](adr/0013-topological-editing-within-the-active-layer.md).

### 7.1 Scope — the active layer only

Cross-layer propagation would mean editing a layer the user did not make active, breaking the
§3.1 invariant. Within-layer coincidence covers the common case — adjacent parcels, adjacent
units. Cross-layer coincidence is Align's job (§11.2).

If cross-layer propagation is ever added, introduce an explicit **editable set** rather than
quietly relaxing the invariant.

### 7.2 Tolerance — much tighter than snapping

Default **0.01 ft**, configurable, and deliberately three orders of magnitude below the snap
tolerance.

Snap tolerance is a *UI affordance* measured in pixels (§6.3). Topological coincidence is a
*property of the data* and must be near-exact. Reusing the snap tolerance would make a vertex
drag move an unrelated vertex twelve feet away — which is the kind of bug that gets reported as
"the editor corrupted my layer."

### 7.3 Which operations honour the toggle

| Operation | Propagates | Why |
|---|---|---|
| Vertex move | **Yes** | Move all coincident vertices together |
| Vertex delete | **Yes** | Delete coincident vertices in neighbours |
| Vertex add | **Yes** | Insert the matching vertex in every feature sharing the edge |
| Split | **Yes** | Node the neighbours at the cut |
| Reshape | **Yes** | Propagate the new boundary to the adjacent feature |
| Feature move | **No** | Ambiguous — does the neighbour stretch or translate? |
| Rotate / Scale | **No** | Same ambiguity |

The two `No` rows are explicit non-topological operations and their tooltips say so.

**Vertex add must not be skipped.** Inserting a vertex on a shared edge in only one polygon does
not create a gap immediately — it guarantees one on the next drag. This is the most common
source of slivers in tools that implement topological editing halfway, and it is invisible until
someone runs Validate a week later.

### 7.4 Implementation

Exact coincidence needs no spatial index. Build a coordinate hash when the session opens, over
the active layer's features in view:

```ts
type CoincidenceIndex = Map<string, VertexRef[]>;
const key = (c: Position) => `${c[0].toFixed(7)},${c[1].toFixed(7)}`;
```

O(1) per drag. Rebuild on `moveend` and after each save. **This index is also the gap detector
for §12.2**, so it earns its keep twice.

### 7.5 Atomicity

One drag mutates N features: **one undo entry, one transaction.** `Command.deltas` is already a
list (§3.4), so this costs nothing structurally — but the vertex-drag handler must collect the
whole delta set rather than a single feature, and that is easy to get wrong under a deadline.

### 7.6 Discoverability nudge

If the toggle is **off** and an applied edit creates a gap or overlap above an area threshold
(default 1 sq ft), show a dismissible notification offering to enable topological editing and
re-run the edit.

Users who need this feature reliably forget it exists until they have made thirty edits and run
Validate.

### 7.7 Its relationship to Align

**Align creates coincidence. Topological editing preserves it.** Data that has never been
aligned will not propagate, and users will report the toggle as broken. Say this in the toggle's
tooltip and in the Align dialog — it is the single most likely misunderstanding in this section.

---

## 8. Command registry

**Every command is defined exactly once.** The menu bar, command palette, toolbar and context
menu all render from this registry. This is the highest-leverage structural decision in the
editing UI and it must exist before any surface is built — retrofitting it means finding four
copies of every command's enabled-state logic.

```ts
interface CommandDef {
  id: string;                            // 'edit.split', 'transform.rotate'
  label: string;
  description?: string;                  // shown in the palette
  icon?: React.ComponentType;
  group: CommandGroup;
  shortcut?: string;                     // 'mod+X' — registered via useHotkeys
  keywords?: string[];                   // extra palette search terms

  enabled(state: EditState): boolean;
  visible?(state: EditState): boolean;
  checked?(state: EditState): boolean;
  run(state: EditState): void | Promise<void>;

  contextMenu?: ContextScope[];
}

type ContextScope = 'feature' | 'vertex' | 'edge' | 'empty' | 'selection';
```

- `enabled` runs on every render of every surface. Keep it cheap and pure.
- A disabled command still appears in the **menu**, greyed, so it stays discoverable and its
  shortcut is visible. It is **excluded from the palette**, where a disabled row is noise.
- Shortcuts derive from the registry. Never register a hotkey independently.
- Use `mod`, not `ctrl`/`cmd`; `useHotkeys` handles the platform difference.

**Built** as `apps/web/src/editing/{types,predicates,registry}.ts`, ahead of every surface as
this section requires. Three things worth recording:

**A shortcut may be shared, and `conflicts(state)` is what makes that safe.** `Delete` is
Delete Feature under Edit and Delete Selected Vertices under Vertices, which is right — but
only because no state enables both. `bindings()` therefore maps a key to *candidates* and the
hotkey layer picks the enabled one, and `conflicts` returns any key with two live candidates.
It found one immediately: with vertices selected, both were enabled, so `Delete` meant
whichever the hotkey layer found first. Delete Feature now requires a feature-scoped selection
— with vertices selected, Delete means the vertices, as it does in every editor, and because
deleting the whole feature is a far larger action to trigger by accident.

**A command with no handler is still registered and still enabled.** It is defined, its
shortcut is reserved, and it appears in every surface; running it does nothing. That is the
honest state of a command whose implementation has not landed. Hiding it would make the menu a
moving target as features arrive, and disabling it would claim the *state* is wrong when the
state is fine.

**`run` takes the state it was enabled against** rather than reading it again. Between a click
and its handler a background refresh can change what is selected, and a command that re-read
would act on something the user did not see when they chose it.

The §9.1 naming rules are enforced by tests rather than by convention: neither Combine nor
Dissolve is labelled "Merge", both carry it as a *keyword*, and both descriptions say what
happens to the geometry — so a search for the word a user arrived with returns the pair, with
the sentence that distinguishes them.

---

## 9. Menu structure

Composed from `Menu` instances (§2.2). Shortcuts render in `rightSection` with `Kbd`.

```
File      Save (mod+S) · Discard Changes · Export Active Layer…

Layers    Add ▸ (Blank · From File… · From Buffer… · Grid Layer…)
          Duplicate Layer · Delete Layer…
          Style… · Properties… · Attribute Table
          Validate Topology…  (async, §12.2)   Align to Layer…  (async, §11.2)

Select    ◉ Click (1) · Rectangle (2) · Lasso (3)
          Select All (mod+A) · None (mod+shift+A) · Invert (mod+I)
          Zoom to Selection (Z)

Edit      Undo (mod+Z) · Redo (mod+shift+Z)
          Cut (mod+X) · Copy (mod+C) · Paste (mod+V) · Delete (Del)
          Split… · Reshape…
          Combine · Explode · Dissolve
          Clip… · Erase… · Intersect…
          Attributes…

Vertices  Edit Vertices (V) · Add Vertex (shift+V)
          Delete Selected Vertices (Del) · Select All Vertices
          Enter Coordinates…

Transform Move (M) · Rotate… (R) · Scale…
          Offset / Buffer… · Smooth… · Simplify…
          Reverse Direction · Trim / Extend to Feature

Snap      ☑ Enable Snapping (S) · ☑ Topological Editing (T) · ☑ Angle Constraint (A)
          Snap Settings…

View      ☑ Show Vertices · ☑ Show Measurements · ☑ Show Validation Errors · Basemap ▸
```

### 9.1 Naming rules — do not rename these

| Label | Means | Not |
|---|---|---|
| **Combine** | Wrap several features into one multi-part feature. **Geometry unchanged.** | Dissolve |
| **Dissolve** | True union. Interior shared boundaries removed. **Geometry changes.** | Combine |
| **Explode** | Multi-part → several single-part features | — |
| **Style** | Symbology and rendering of a layer (`08`) | A file format |
| **Move** | Under Transform, beside Rotate and Scale, because that is what it is | — |

`Combine` / `Dissolve` / `Explode` is the standard triad and users from QGIS or ArcGIS will
recognise it. **Never label two commands "Merge."** The ambiguity between multi-part wrapping
and true union is a real and common usability defect, and it produces silent data loss in
whichever direction the user did not intend.

### 9.2 Notes on specific items

- **Reshape** — draw a line across a boundary; the portion between the two intersections is
  replaced. In parcel and lease work this is used more than anything except vertex dragging, so
  it is not optional. It reuses most of Split's machinery.
- **Simplify** ships with **Smooth**. Users expect the pair.
- **Explode** is needed the first time someone Combines by accident, which will be week one.
- **Trim / Extend to Feature** — CAD-style; extend or trim a line endpoint to its intersection
  with another feature. Directly relevant to well sticks (§18).
- **Reverse Direction** — line direction affects labelling and downstream operations.
- **Undo / Redo** tooltips show the specific command label: "Undo Move Vertex".

---

## 10. Toolbar and status surfaces

Target: **about ten controls on the edit toolbar.** Hold this line. Anything that does not earn
a slot lives in the menu and the palette, which is what they are for.

### 10.1 Persistent edit toolbar

Always visible during a session, ~40 px, below the menu bar.

- **Left — mode.** `SegmentedControl`: Select | Vertex | Draw | Move. When Select is active, a
  secondary group for Click / Rectangle / Lasso. When Draw is active, a `Menu` for the geometry
  type.
- **Centre — constraints**, each a split button (toggle + caret opening a `Popover`): **Snap**
  (badged when the tolerance is pixel-clamped, §6.3), **Topology** (tolerance), **Angle
  constraint** (15° / 30° / 45° / custom, plus perpendicular and parallel-to-previous).
- **Right — history and session.** Undo / Redo with command-label tooltips; Save / Discard; a
  dirty badge showing the unsaved edit count.
- **Far right.** The **active layer selector** — prominent, because it is the most consequential
  piece of state in the subsystem — and the validation status with next/previous navigation.

### 10.2 Contextual operation bar

Appears **only** while a modal operation runs (§5.1). Operation name, its parameters, **Apply**
and **Cancel**, and a one-line hint: *"Click to add points along the cut line. Enter to apply,
Esc to cancel."*

Visually distinct from the persistent toolbar — a different background — so the two levels of
commitment are never confused. It disappears the instant the operation resolves.

### 10.3 Async operation feedback

For backend jobs (Validate, Align, Split, Dissolve, Smooth): a progress indicator with
**Cancel**; on completion a **preview** — features affected, area delta, a diff overlay —
requiring explicit confirmation before anything enters the dirty buffer. The whole result is
**one undo entry**. `@mantine/notifications` for completion and failure.

### 10.4 Status strip

Bottom-left, read-only. Telemetry, not controls — which is what keeps it out of the ten-control
budget.

- **Cursor coordinates** in the working CRS, with a CRS indicator.
- **Snap state** — what is snapped and to which layer: `⊾ vertex · Leases`, or `no snap`. This
  is the difference between "the tool is broken" and "I am snapped to the wrong layer."
- **Live measurement** — length while drawing a line, area while drawing a polygon, and a
  selection summary when idle: `3 features · 640.2 ac`. Unit configurable. In land and lease
  work, acreage is the number everyone is actually watching.
- **Selection count with a clear button** — `3 selected ✕`. Doubles as Select None and explains
  why commands are greyed.
- **Zoom level and scale bar.**

### 10.5 Numeric and bearing entry

A hotkey (`` ` ``) opens an inline input for the next vertex, accepting absolute coordinates in
the working CRS, or **bearing and distance from the previous vertex**: `N 45°30' E, 1320`.

This is the clean way to hit exact coordinates without fighting pixel tolerance, and anywhere
near metes-and-bounds descriptions it is the feature that decides whether people adopt the tool
or stay where they are. Parse tolerantly — quadrant bearings, azimuths, decimal degrees — and
**echo the parsed interpretation back before applying**, because a misread bearing is a silent
error the user will not catch.

### 10.6 Validation status

A live linter for geometry: the count of unresolved topology errors on the active layer, with
next/previous navigation that zooms to each. **The menu item runs the check; the toolbar
indicator shows the standing result.** Re-evaluated after each save.

---

## 11. Operations catalog

For each: where it runs, what it requires, and how it enters the undo stack.

### 11.1 Split

- **Runs:** client builds the cut line, backend performs the split (Shapely `ops.split`)
- **Requires:** ≥1 selected feature in the active layer
- **Behaviour:** each click adds a vertex to the cutting polyline, snapped normally. On apply,
  every selected feature is split along it.
- **Edge cases:** the cut must fully cross a polygon — a partial cut is rejected with a message,
  not silently ignored; lines split at each intersection; one cut may produce more than two parts
- **Attributes:** duplicated to all parts, derived fields recomputed, and a `split_from_id`
  provenance field written
- **Undo:** one entry containing the delete and every creation

### 11.2 Align

- **Runs:** backend, async
- **Inputs:** the active layer, a target layer, a tolerance
- **Behaviour:** snap vertices of the active layer within tolerance to the target's vertices and
  edges
- **Preview required:** yes — this can touch thousands of features
- **Implementation:** **DuckDB** computes the candidate set — dump the active layer's vertices,
  spatial-join against the target within tolerance, return only pairs that will actually move.
  That is the expensive step and it stays out of Python. Then apply per candidate feature with
  Shapely `snap` and `nearest_points`.
- **The candidate set is also the preview payload.** Compute it once, show it, reuse it on
  confirm rather than recomputing — otherwise the preview and the result can disagree.
- The dialog states that Align creates the coincidence topological editing later preserves (§7.7).

### 11.3 Reshape

- **Requires:** exactly one selected feature
- **Behaviour:** the drawn line must cross the boundary at **exactly two points**; the boundary
  between them is replaced. More or fewer intersections → reject with an explanatory message.
- **Topological:** yes, propagates to the adjacent feature when the toggle is on

### 11.4 Combine / Explode / Dissolve

| Operation | Runs | Requires | Behaviour |
|---|---|---|---|
| Combine | Client | ≥2 selected, same geometry kind | Wrap into a multi-part feature. Geometry unchanged. First feature's attributes win, configurable. |
| Explode | Client | ≥1 multi-part selected | Split into single-part features; attributes duplicated |
| Dissolve | Backend | ≥2 selected polygons | True union, interior boundaries removed |

Dissolve **must** specify attribute handling — sum numerics, take the largest-area feature's
values for text, or prompt. Default to prompting, with a preview. Leaving this unspecified is
silent data loss.

### 11.5 Smooth / Simplify

Smooth is Chaikin corner-cutting (iterations, tension); Simplify is Douglas-Peucker or
Visvalingam (tolerance in feet). Both preview live on the map as the parameters change.

**Both break topological coincidence with neighbours.** With the topology toggle on, either
propagate to shared boundaries or warn and require confirmation. Silently desyncing a shared
boundary is exactly the failure §7 exists to prevent.

> `webmap_geo.contour.smooth` already implements Chaikin for contour lines, with a cap above
> which a smoothed line drifts measurably off the value it claims (`05` §7). Editing should reuse
> that function rather than growing a second implementation — a shared boundary smoothed by two
> different rules is the same class of bug as the fill-versus-line divergence that module's
> docstring describes.

### 11.6 Vertex operations

| Operation | Trigger | Notes |
|---|---|---|
| Move | Drag a handle | Snaps; resolves exact coordinates on `pointerdown` (§6.6); topological if enabled |
| Delete | `Del` with vertices selected, or **double-click a handle** | Rejected if it would drop a ring below its minimum (3 for a polygon ring, 2 for a line) |
| Add | Vertex-add mode, click | Project onto the nearest edge of a selected feature, splice at the correct segment index. Topological if enabled. |
| Nudge | Arrow keys | 1 px, 10 px with shift. **Snapping is disabled during a nudge** — the point of nudging is to land somewhere snapping will not let you. |
| Enter coordinates | `` ` `` or the menu | §10.5 |

Vertex handles render as **squares** from a dedicated GeoJSON source; selected vertices in a
distinct colour. Multi-select by rubber band within vertex mode, plus shift-click to toggle.

### 11.7 Clipboard

- **Cut / Copy** — serialise geometry and attributes to an internal clipboard, and write GeoJSON
  to the system clipboard for interoperability.
- **Paste** — insert with a visible offset and enter `paste-place`, an operation-level state with
  the Apply / Cancel bar showing. New ids generated, attributes copied, unique-constrained fields
  cleared.
- **Cross-layer paste** — only into the active layer, and only when geometry kinds are compatible.

---

## 12. Validation

### 12.1 Per-geometry, on change

Runs on every geometry change, before save. **Errors block; warnings do not.**

```python
# python/webmap_geo/src/webmap_geo/validate.py

class Severity(StrEnum):
    ERROR = "error"      # blocks save
    WARNING = "warning"  # allows save, surfaces in the UI


@dataclass(frozen=True)
class ValidationIssue:
    severity: Severity
    code: str
    message: str
    location: tuple[float, float] | None = None
    feature_id: int | None = None


def validate_geometry(
    geom: BaseGeometry,
    frame: AnalysisFrame,
    sliver_threshold: float = 0.05,
    min_segment_length: float | None = None,
) -> list[ValidationIssue]:
    """Validate a single geometry in the analysis frame.

      ERROR   self-intersection (invalid per OGC)
      ERROR   unclosed ring
      ERROR   fewer than 3 distinct vertices in a polygon ring
      ERROR   zero-length linestring
      ERROR   NaN or infinite coordinate
      WARNING sliver polygon (thinness ratio below threshold)
      WARNING duplicate consecutive vertices
      WARNING segment shorter than min_segment_length
      WARNING self-touching ring (valid, but usually a mistake)
    """
```

`shapely.validation.explain_validity` returns the reason **and** its location, which is what
makes an error navigable from the status strip rather than merely reported.

### 12.2 Validate Topology — whole layer, async

- **Runs:** backend job (`10-jobs-async.md`)
- **Detects:** gaps, overlaps, self-intersections, duplicate vertices, null geometry
- **Implementation:** **DuckDB** for the scan — a self-join over the layer for overlapping pairs,
  and a union-versus-envelope comparison for gaps. The whole layer never enters Python.
  **Shapely** for repair: `make_valid` per offending feature, then re-check.
- **Auto-fix** is optional and bounded by a configurable gap/overlap area threshold.
- **Preview required**, and the whole fix is **one undo entry**.

### 12.3 Fault network validation

Fault layers get the extra checks from `05` §3, run on save rather than at grid time. Catching a
dangling fault while the geologist is looking at it is far better than failing a kriging job
forty minutes later.

---

## 13. Persistence

Features are immutable GeoParquet objects (`02` §3.5.1), so an edit writes a new version rather
than mutating anything.

A flushed batch reads the current object, applies the coalesced changes in memory, writes
`features/ds_<hex>/v<N+1>.parquet`, inserts a `dataset_version` row, and then advances the
pointer (§5.3). **Advancing the pointer is the commit** — until it moves, the new object is
invisible and a crash leaves an orphan the retention job collects. Readers never see a
half-written layer; that falls out of the storage model rather than being a property to preserve.

Tile caches key on `(dataset_id, version, z, x, y)` (`06` §7), so advancing the pointer
invalidates exactly this layer and nothing else. No purge step.

**Edits accumulate and flush on a debounce or explicit save.** A vertex drag fires dozens of
change events; one request per event would overwhelm the API and make undo incoherent. Coalesce
by feature id — last state wins within a batch.

**Never write in place.** Editing a dataset sourced from a file share creates a new versioned
output; the source file is never modified (`03` §8). A geologist losing a partner-delivered
shapefile is unrecoverable, and no editing convenience is worth that.

**Retention.** Every version for 30 days, matching the soft-delete window, then thinned to daily.
Storage grows with edit count — that is the cost of this model and it needs the cron slot in
`10` §3.

---

## 14. Attribute handling

Every geometry operation has an attribute consequence, and leaving one unspecified is silent
data loss. Each of these is implemented explicitly:

| Event | Behaviour |
|---|---|
| Create | Apply the layer's configured defaults |
| Split | Duplicate to all parts; recompute derived fields; write `split_from_id` |
| Combine | First feature's values by default, configurable |
| Dissolve | Prompt for a strategy — sum / largest-area / manual |
| Paste | Copy all, clear unique-constrained fields |

The **Attributes panel** edits the selection: field types come from `dataset.attribute_schema`
and drive the control. A multi-feature edit sets a field across the selection, and **mixed values
show as indeterminate, not blank** — blank reads as "empty" and overwrites on the next keystroke.

**Derived fields — area, perimeter, length — are recomputed server-side on save**, never
maintained client-side, so the displayed value and the stored value cannot drift.

**Shapefile warning at schema-edit time.** If the dataset may be exported to shapefile, warn when
a field name exceeds 10 characters *while it is being named*, not at export when it is too late
to choose a shorter one (`11` §4).

---

## 15. Keyboard map

Registered from the command registry (§8) via `useHotkeys`, with `mod` for platform independence.

| Key | Action |
|---|---|
| `Esc` | Cancel the operation; a second press returns to Select |
| `Enter` | Apply the operation |
| `mod+Z` / `mod+shift+Z` | Undo / Redo |
| `mod+S` | Save |
| `mod+X` / `mod+C` / `mod+V` | Cut / Copy / Paste |
| `Del` | Delete selection — features, or vertices in vertex mode |
| `mod+A` / `mod+shift+A` / `mod+I` | Select All / None / Invert |
| `1` / `2` / `3` | Click / Rectangle / Lasso |
| `V` / `shift+V` | Vertex edit / Vertex add |
| `M` / `R` | Move / Rotate |
| `S` / `T` / `A` | Toggle snapping / topology / angle constraint |
| **`Alt` (held)** | **Temporarily suspend snapping** |
| `mod` / `shift` (held) | Invert selection behaviour during a gesture |
| Arrow keys | Nudge selected vertices (shift = 10×) |
| `` ` `` | Numeric / bearing entry |
| `Z` | Zoom to selection |
| `mod+K` | Command palette |

**Hold-Alt-to-suspend-snapping is not optional.** Every mature editor has this escape hatch and
users reach for it constantly — it is how you place a vertex *near* an existing one on purpose.

Command palette: `@mantine/spotlight` on `mod+K`, generated from the registry, grouped, showing
each shortcut, with `description` and `keywords` so `"union"` finds Dissolve and `"acreage"`
finds the measurement toggle. **Disabled commands are excluded**, unlike the menu.

Context menu: right-click, hit-tested to a scope, populated from `CommandDef.contextMenu`. Keep
each context under about eight items and nest transforms in a submenu. It is the most-used
surface in a GIS editor precisely because it is pre-filtered to what is under the cursor.

---

## 16. Backend API surface

```
GET    /api/v1/layers/{id}/features/{fid}/exact   exact geometry + vertex identity + version
POST   /api/v1/layers/{id}/features/exact         batch exact fetch

POST   /api/v1/layers/{id}/save                   transactional commit of FeatureDelta[]
                                                  409 + changed feature ids on version mismatch
                                                  200 + affected_layer_ids on success

POST   /api/v1/ops/split | dissolve | smooth | simplify | buffer      [Shapely]
POST   /api/v1/ops/clip | erase | intersect                           [Shapely]

POST   /api/v1/jobs/validate_topology             async     [DuckDB scan + Shapely repair]
POST   /api/v1/jobs/align                         async     [DuckDB candidates + Shapely snap]
```

Conventions:

- **`{id}` is a layer id, and the layer is resolved to its dataset.** Features and versions
  belong to the `dataset` (`02` §3.5.1); a layer is a dataset plus how it is drawn, and
  several layers may reference one dataset (`07` §6.1 — the same shapefile styled two ways is
  legitimately two layers). The path stays layer-oriented because a layer is what the user
  selected in the tree.
- **A dataset with several layers is editable, and every dependent layer refreshes.** The edit
  is not blocked and the siblings are not left stale: the save response carries
  `affected_layer_ids` — every layer referencing that dataset, including the one edited — and
  the client refreshes all of them. Tile caches key on `(dataset_id, version, z, x, y)`
  (`06` §7), so advancing the pointer already invalidates every one of them; this makes the
  client-side consequence explicit rather than leaving a sibling layer drawing last version's
  geometry until something else happens to reload it.
- The synchronous `/ops` endpoints are per-feature work and therefore all Shapely; the `/jobs`
  endpoints are whole-layer work and lead with DuckDB (§2.3).
- **Jobs go through the existing job path** (`10-jobs-async.md`) — submit, poll with the returned
  backoff, cancel — rather than a second async mechanism. They must return a **preview** before
  mutating anything.
- **Synchronous ops return geometry and do not persist.** Persistence happens only through
  `/save`. This is what keeps the two levels of commitment (§5) real rather than nominal.
- All geometry in and out is at full precision (§2.4).
- **Endpoints must not leak the engine choice into their contract.** Swapping a Shapely
  implementation for a DuckDB one must not change the request or the response.

---

## 17. Layer source switching and the working set

Editing needs GeoJSON sources for immediate feedback; large layers need MVT for performance.

1. Switch the edit target to a GeoJSON source holding only the viewport's features, **capped at
   5,000**.
2. Keep the MVT source visible for context, filtered to exclude those ids so nothing renders
   twice.
3. On flush, invalidate the affected tiles and switch back.

Panning re-queries the viewport; unflushed edits are retained and re-applied to the new working
set. Above 5,000 features in view the UI requires zooming in first, and **says so plainly rather
than degrading silently**.

> **The cap applies to the client working set, not to backend operations.** Align and Validate
> Topology run over the whole layer in DuckDB (§11.2, §12.2) and are unaffected — they never
> materialise the layer in the browser. The cap exists because a whole-object rewrite is the cost
> of copy-on-write (`adr/0005`), and because 5,000 editable handles is already past what anyone
> can work with.

---

## 18. Parameterised geometry — well sticks

Deferred, but the data model accommodates it now to avoid a painful retrofit.

A well stick is a **parameterised geometry**, not a freely drawn linestring: surface location,
azimuth, length, and for a deviated well a survey of measured-depth / inclination / azimuth
stations. **Store the parameters as authoritative and derive the linestring for display** — not
the other way around. Editing a stick means editing its parameters, with the geometry
regenerated.

This implies a feature-level `geometry_source` discriminator, `'drawn' | 'derived'`
(`02-data-model.md` §3.5.1). Derived features are **not vertex-editable** through the normal
handles — the handles are hidden and a parameter panel appears instead. Flag the discriminator
in the model now even though `'derived'` is unused at launch; adding it later means migrating
every feature table.

---

## 19. Testing

| Concern | Approach |
|---|---|
| Validation rules | pytest over geometry fixtures, including known-bad shapes |
| Snapping correctness | Vitest, synthetic geometries with exact expected snap points |
| Snap performance | Benchmark: the full pass under 4 ms with 50k segments in view |
| Precision | **Round-trip a feature through every engine hop and assert bit-identical coordinates** (§2.4) |
| Coincidence index | Property test: after a topological vertex move, every previously coincident vertex is still coincident |
| Vertex add propagation | The §7.3 case that silently creates slivers — assert the neighbour gained a vertex |
| Version commit | Integration: two sessions commit against one version; one wins, the other gets a 409 **naming the changed features**. And: kill the process between the write and the pointer advance; assert no orphan is visible and the pointer did not move |
| Undo/redo | Property test — random command sequences, assert `undo(apply(s)) == s` |
| Command registry | Every command appears in exactly one registry entry; every shortcut is unique |
| Edit → render | E2E: edit a fault, re-grid, confirm the surface changed at the fault |

That last test is the one that matters most. It verifies the whole chain — edit, persist,
invalidate, re-interpolate with the new constraint geometry — and it is the chain most likely to
break silently.

---

## 20. Implementation order

Each phase independently shippable.

1. **Foundation** — command registry, mode state machine, menus rendered from the registry,
   `useHotkeys`, command palette. No operations yet.
2. **Selection** — feature store, both selection scopes, click/rectangle/lasso, modifier
   inversion, active-layer plumbing, the status strip's selection count.
3. **Edit session** — dirty buffer, `Command`/`FeatureDelta`, undo/redo, Save/Discard, IndexedDB
   durability, the version check. **Before any mutating operation exists.**
4. **Snapping** — the screen-space engine, tolerance conversion, indicator, settings popover,
   status-strip snap state. Configure unsimplified tiles at edit zooms.
5. **Vertex editing** — handles, vertex scope, move/add/delete, the exact-resolution protocol.
6. **Drawing** — Terra Draw for new geometry, wired to the snap engine through `toCustom`.
7. **Clipboard and simple transforms** — cut/copy/paste, move, rotate, scale.
8. **Topological editing** — coincidence index, toggle, propagation, the discoverability nudge.
9. **Backend operations** — split, reshape, combine/explode/dissolve, smooth/simplify, overlay.
10. **Async jobs** — validate topology, align, preview and progress, the validation indicator.
11. **Attributes** — panel, bulk edit, resolution strategies.
12. **Precision entry** — bearing/distance input, angle constraint, arrow-key nudge.

**Phase 3 is the one most likely to be skipped under schedule pressure and the most expensive to
retrofit.** Undo built in from the first mutation costs almost nothing; undo added afterwards
means revisiting every operation that already exists.
