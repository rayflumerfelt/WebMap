# 08 — Styling and Palettes

---

## 1. The layering problem

The requirement was "implement all formatting options available in MapLibre." Taken literally
this is a trap: the style spec has hundreds of paint and layout properties, most are
expression-valued, and a UI exposing all of them is unusable.

Resolve it with two layers:

```
     Symbology model (what geologists think in)
        Single symbol / Categorized / Graduated / Rule-based
                          ↓ compile
     MapLibre Style JSON (what renderers consume)
                          ↕ edit
     Raw property editor (schema-driven escape hatch)
```

The symbology model covers 95% of real use with concepts borrowed from QGIS. The raw property
editor covers the rest, and it is **auto-generated from the style spec** so coverage is near
complete for free.

---

## 2. Symbology model

```typescript
// packages/style-model/src/symbology.ts

export type Symbology =
  | SingleSymbol
  | Categorized
  | Graduated
  | RuleBased
  | ContinuousRaster;

export interface SingleSymbol {
  type: 'single';
  symbol: SymbolSpec;
}

export interface Categorized {
  type: 'categorized';
  field: string;
  categories: Array<{ value: string | number | null; symbol: SymbolSpec; label: string }>;
  other?: SymbolSpec;          // fallback for unlisted values
}

export interface Graduated {
  type: 'graduated';
  field: string;
  method: ClassificationMethod;
  classCount: number;
  /** Explicit breaks. Present after classification runs; editable by hand. */
  breaks: number[];
  paletteId: string;
  /** What varies across classes. Colour is usual; size for graduated symbols. */
  vary: 'color' | 'size' | 'both';
  baseSymbol: SymbolSpec;
  sizeRange?: [number, number];
}

export interface RuleBased {
  type: 'rules';
  rules: Array<{
    /** MapLibre filter expression. The escape hatch for arbitrary logic. */
    filter: unknown[];
    symbol: SymbolSpec;
    label: string;
    minZoom?: number;
    maxZoom?: number;
  }>;
}

export interface ContinuousRaster {
  type: 'continuous_raster';
  paletteId: string;
  /** Value range mapped to the palette. Null = dataset min/max. */
  range: [number, number] | null;
  /** Values outside range: clamp to end colours, or render transparent. */
  clamp: boolean;
  opacity: number;
  /** Hillshade blend for structure maps. */
  hillshade?: { azimuth: number; altitude: number; exaggeration: number };
}

export type ClassificationMethod =
  | 'equal_interval'
  | 'quantile'
  | 'natural_breaks'    // Jenks
  | 'standard_deviation'
  | 'pretty'            // round numbers — what geologists usually want
  | 'manual';
```

### 2.1 Symbol spec

Geometry-aware, which resolves the "points and lines have no fill" concern structurally rather
than by conditional UI.

```typescript
export type SymbolSpec = PointSymbol | LineSymbol | PolygonSymbol | LabelSymbol;

export interface PointSymbol {
  geometry: 'point';
  marker: 'circle' | 'square' | 'triangle' | 'cross' | 'sprite';
  size: number;
  color: string;
  strokeColor: string;
  strokeWidth: number;
  opacity: number;
  spriteName?: string;         // when marker === 'sprite'
  rotation?: number | FieldRef;
}

export interface LineSymbol {
  geometry: 'line';
  color: string;
  width: number;
  opacity: number;
  dashArray?: number[];
  cap: 'butt' | 'round' | 'square';
  join: 'bevel' | 'round' | 'miter';
  /** Second pass beneath the main line — casings for roads, fault
   *  decorations, index-contour emphasis. */
  casing?: { color: string; width: number };
}

export interface PolygonSymbol {
  geometry: 'polygon';
  fillColor: string;
  fillOpacity: number;
  fillPattern?: string;
  outlineColor: string;
  outlineWidth: number;
  outlineDashArray?: number[];
}

export interface LabelSymbol {
  geometry: 'label';
  /** Which attribute is drawn. Without one there is nothing to label. */
  field: string;
  /** Points. Compiled to pixels — see §2.2. */
  size: number;
  /** How size behaves as the map zooms. See §2.2; neither is MapLibre's
   *  default, which is fixed pixels regardless of scale. */
  sizeMode: 'fixed' | 'scale-with-map';
  color: string;
  /** Essential rather than decorative: unhaloed text over a filled grid is
   *  unreadable at any size. */
  haloColor: string;
  haloWidth: number;
  /** A MapLibre *font stack*, e.g. `['Oswald Bold']`. Bold and italic are
   *  separate stacks, not properties — see §2.3. */
  font: string[];
  placement: 'point' | 'line' | 'line-center';
  allowOverlap: boolean;
  minZoom?: number;
  maxZoom?: number;
}
```

**No conditional fields.** A `PointSymbol` has no `fillColor` because points have no fill.
The type system prevents the UI from offering it, which is more reliable than a runtime check.

### 2.2 What MapLibre cannot do, and what we do instead

Four things the formatting model has to work around rather than express directly. Each was
checked against the style spec and against a running MapLibre, not assumed.

**Polygon outlines have no width.** `fill-outline-color` is always one pixel and takes no
width property, so `PolygonSymbol.outlineWidth` compiles to a **companion `line` layer** above
the fill. Every polygon layer is therefore two MapLibre layers. The user is never shown this:
one layer in the tree, one entry in the legend, one thing to edit.

**`line-dasharray` cannot be data-driven.** It accepts no property expressions, so line *type*
by column value is not offered — colour is. It also cannot interpolate smoothly across zoom,
and its units are multiples of line width, so a data-driven width makes the dashes breathe.
A layer that genuinely needs dash-by-category expresses it as `RuleBased`, which compiles to
one MapLibre layer per pattern.

**Text size is in screen pixels and fixed by default.** `LabelSymbol.size` is in *points*, so
it compiles to pixels; `sizeMode` decides what happens as the map zooms:

- `fixed` — the same size on screen at every zoom. MapLibre's own default.
- `scale-with-map` — the same size on the ground, compiled as an `interpolate` with
  `["exponential", 2]` on zoom, which matches the doubling of scale per zoom level. It is an
  approximation; nothing in the style spec does it exactly.

The render service outputs at 2x for slides, so the point-to-pixel conversion has to scale
with the render, not be baked at authoring time.

**Grids are not coloured by MapLibre at all.** See §5.2.

### 2.3 Fonts

MapLibre has no system-font fallback. `text-font` names a **font stack** and the renderer
fetches signed-distance-field glyphs from the style's `glyphs` URL. A stack it cannot fetch
draws nothing — silently, with no console error and no failed-tile warning, so the failure
looks like a map that simply has no labels.

**Bold and italic are not properties.** Each is its own stack built from its own font file, so
"Oswald Bold" and "Oswald Regular" are two glyph sets. The formatting UI offers family, weight
and style as separate controls and composes the stack name from them.

`scripts/fetch_fonts.py` builds the roster — ten families chosen for variety, all SIL OFL or
Apache 2.0 so an internal deployment may redistribute them:

| Family | Style | Why |
|---|---|---|
| Noto Sans | Humanist sans | Broadest coverage; the default |
| Open Sans | Neutral humanist | The most common web-map label face |
| Roboto | Neo-grotesque | Tighter, more mechanical texture |
| Source Sans 3 | Humanist | Clean at small sizes |
| Lato | Warm semi-rounded | A softer texture |
| PT Sans | Slightly narrow | Dense labelling |
| Oswald | Condensed | Contour labels and tight polygons |
| Noto Serif | Serif | Traditional for physical-feature names |
| Playfair Display | Display serif | Titles and map furniture |
| Roboto Mono | Monospace | Coordinates and grid references |

**29 stacks, not 30: Oswald has no italic.** The UI disables the italic control when Oswald is
selected rather than offering one that does nothing — and a test asserts the absence, so
nobody later "fixes" it by synthesising a slant.

The API serves the ranges at `/static/glyphs/{fontstack}/{start}-{end}.pbf`, unauthenticated:
they are open-licensed outlines carrying no user data, and requiring a token would mean the
isolated render worker needed one to draw a label. `GET /static/glyphs` lists what this
deployment actually built, and the styling UI reads it rather than hard-coding a font list, so
a font that failed to build is absent rather than offered and then blank.

---

## 3. Compilation

```typescript
// packages/style-model/src/compile.ts

/**
 * Compile a symbology model into MapLibre layers.
 *
 * One symbology may produce several MapLibre layers — a polygon with an
 * outline is a fill layer plus a line layer, a line with casing is two line
 * layers in order. Callers must not assume a 1:1 mapping.
 */
export function compileSymbology(
  symbology: Symbology,
  sourceId: string,
  sourceLayer: string | undefined,
  palettes: Record<string, Palette>,
): LayerSpecification[] {
  switch (symbology.type) {
    case 'graduated': return compileGraduated(symbology, sourceId, sourceLayer, palettes);
    // ...
  }
}

function compileGraduated(s: Graduated, ...): LayerSpecification[] {
  const palette = palettes[s.paletteId];
  const colors = sampleRamp(palette, s.classCount);

  // 'step' rather than 'interpolate': graduated classification is discrete
  // by definition. Using interpolate would blur class boundaries and make
  // the legend a lie.
  const colorExpr: unknown[] = ['step', ['get', s.field], colors[0]];
  s.breaks.forEach((brk, i) => colorExpr.push(brk, colors[i + 1]));

  return [{ /* ... */ paint: { 'fill-color': colorExpr } }];
}
```

### 3.1 Cross-language parity

The backend must compile identically for the render service. Two implementations, one test
suite:

```
packages/style-model/test-vectors/
├── graduated_polygons.json     { symbology, palettes, expected_layers }
├── categorized_faults.json
├── continuous_raster.json
└── ...
```

Both `packages/style-model` (Vitest) and `python/webmap_core/style` (pytest) run the same
vectors. A divergence fails CI in both languages.

**Why two implementations rather than one?** The frontend needs synchronous compilation for
live preview while dragging a ramp stop; a round trip would be unusable. The backend needs it
without a JS runtime. The shared vectors are the cost of that.

---

## 4. Classification

```python
# python/webmap_core/style/classify.py

import numpy as np


def classify(
    values: np.ndarray, method: str, n_classes: int
) -> list[float]:
    """Compute class breaks. Returns n_classes - 1 interior breaks."""
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        raise ValueError("No finite values to classify")

    match method:
        case "equal_interval":
            return list(np.linspace(finite.min(), finite.max(), n_classes + 1)[1:-1])
        case "quantile":
            qs = np.linspace(0, 100, n_classes + 1)[1:-1]
            return list(np.percentile(finite, qs))
        case "natural_breaks":
            return jenks_breaks(finite, n_classes)
        case "standard_deviation":
            return std_dev_breaks(finite, n_classes)
        case "pretty":
            return pretty_breaks(finite.min(), finite.max(), n_classes)
        case _:
            raise ValueError(f"Unknown classification method: {method}")


def jenks_breaks(values: np.ndarray, n_classes: int, max_sample: int = 5000) -> list[float]:
    """Fisher-Jenks natural breaks.

    O(n^2 * k). Subsample above max_sample — at 500k values the exact
    computation would take hours and the breaks from a 5,000-point sample
    are indistinguishable.
    """
    if values.size > max_sample:
        rng = np.random.default_rng(0)   # deterministic; see CLAUDE.md
        values = rng.choice(values, max_sample, replace=False)
    ...


def pretty_breaks(vmin: float, vmax: float, n_classes: int) -> list[float]:
    """Round breaks at 1, 2, 2.5, or 5 x 10^n.

    Usually the right default for geological maps. A porosity range of
    4.1-21.8% classified into 5 classes gives breaks at 5, 10, 15, 20 —
    not 7.64, 11.18, 14.72, 18.26. Geologists read breaks off the legend
    and expect numbers they can hold in their head.
    """
```

---

## 5. Palettes

Mantine provides `ColorPicker` and `ColorInput` for single colors. It does **not** provide a
gradient ramp editor, which is what continuous data actually needs. That is the component to
build.

```typescript
// packages/ui/src/RampEditor/RampEditor.tsx

export interface RampEditorProps {
  palette: Palette;
  onChange(palette: Palette): void;
  /** Histogram of the data being styled, drawn behind the ramp. Lets the
   *  geologist see where their values actually concentrate while placing
   *  stops — without it, stop placement is guesswork. */
  histogram?: { bins: number[]; counts: number[] };
  breaks?: number[];
  valueRange?: [number, number];
  unit?: string;
}

export interface Palette {
  id: string;
  name: string;
  isContinuous: boolean;
  stops: Array<{ position: number; color: string }>;   // position 0..1
  interpolation: 'linear' | 'discrete';
}
```

Required behaviours:

- Drag stops along the ramp; double-click to add; drag off to remove
- Click a stop to open a Mantine `ColorPicker`
- Reverse the ramp
- Toggle linear vs discrete interpolation
- Type exact positions and hex values
- Histogram underlay aligned to the value axis

### 5.1 Import and export

**Geologists have existing palettes and will insist on using them.** Supporting only
hand-built ramps guarantees the tool is rejected.

| Format | Extension | Source |
|---|---|---|
| Surfer color spec | `.clr` | Golden Software Surfer |
| GMT color palette | `.cpt` | GMT, cpt-city |
| QGIS color ramp | `.xml` | QGIS style exports |
| WebMap native | `.json` | Round-trip |

```python
# python/webmap_core/style/palette_io.py

def read_clr(text: str) -> Palette:
    """Surfer .clr format.

    Lines of: position red green blue [alpha], positions 0-100.
    Header lines beginning with 'ColorMap' are skipped.
    """


def read_cpt(text: str) -> Palette:
    """GMT .cpt format.

    Each line: z0 r0 g0 b0 z1 r1 g1 b1, defining a slice. Also supports
    hex and named colours, and the B/F/N lines for background, foreground,
    and NaN — we map those to clamp colours and the nodata colour.

    Handles both continuous (default) and discrete (trailing ';' hard
    breaks) forms.
    """
```

Ship a default set: perceptually uniform sequential ramps (viridis, cividis) for property
maps, diverging ramps (RdBu, BrBG) for anomaly and difference maps, and a spectral ramp for
structure because that is what geologists expect even though it is perceptually poor. Include
the perceptual caveat as a tooltip rather than removing the option.

---

### 5.2 Colouring a grid

**MapLibre never colours a grid.** Raster layers have no data-driven paint, so the colour is
baked into the PNG by TiTiler before the browser sees it — MapLibre draws a picture. There are
no values left to write an expression against, which is why a grid's palette travels as a tile
parameter rather than in the style.

```
COG (float32, one band)
   -> API /api/v1/cog/{dataset}/{z}/{x}/{y}.png     permission check, colour params
      -> TiTiler                                     float -> RGBA, server side
         -> MapLibre raster layer                    draws the result
```

MapLibre still owns `raster-opacity` — the layer's fill transparency — plus resampling, draw
order and zoom range. Nothing else about the colour.

**Both scale types are expressible, and they work differently.**

| Mode | Sent as | Units | Comparable across grids? |
|---|---|---|---|
| **Interval** | a list of `[[min,max],[r,g,b,a]]` bands | **raw data** | Yes — the bands say what they mean |
| **Gradient** | a 0-255 lookup plus `rescale` | normalised | No — the same colour is a different value on every grid |

A band list is a direct lookup: no interpolation and no normalisation, so the rendered pixels
are byte-for-byte the colours the palette editor chose.

**The two must never be combined.** `rescale` normalises the data to 0-255 *before* the
colormap applies, so a band list sent alongside one has its bounds compared against 0-255 and
nothing ever matches — the whole tile renders transparent behind a 200 response. The endpoint
therefore drops its *default* rescale when the colormap is a band list, while still honouring
one a caller asked for explicitly.

**Implicit minimums must be closed at both ends.** The interval editor takes only maximums —
the minimum of each band is the maximum of the one below. The compiler extends the first band
down to the data floor and the last up to the ceiling, so a value below the lowest entry or
above the highest clamps to the end colour. Uncovered values render **transparent**, and a
hole in a grid is indistinguishable from no-data, which is the one thing the extrapolation
reporting exists to prevent.

Genuine no-data stays transparent, because it comes from the COG's nodata rather than from the
colormap. That is what lets a blanked or clipped area show the basemap through.

#### The range a gradient scales across

`dataset.value_min` / `value_max` is the **display range**, not the extremes, and the tile
endpoint uses it as the default `rescale`. Without it TiTiler stretches each tile to *that
tile's* local range and the map becomes a patchwork — for a while every grid the gridding job
produced rendered exactly that way, because the job recorded no range at all.

Derived grids record **P5-P95** of their finite cells (`gridding.DISPLAY_PERCENTILES`).
Not the minimum and maximum, because minimum curvature overshoots into extrapolated corners
and the overshoot is unbounded. Measured on one run:

| | Range | Span | Share of ramp given to the signal |
|---|---|---|---|
| Control data | -10,371 to -7,521 | 2,850 ft | — |
| Grid extremes | -10,441 to **-248** | 10,193 ft | 28% |
| **P5-P95 (stored)** | -10,007 to -6,098 | 3,909 ft | **73%** |

Values outside clamp to the end colours. The surface's true extremes are not lost — they are
in the job result's `diagnostics.output_range`, beside the input range they should be compared
against, and the legend states the percentiles so a suspiciously flat maximum is explicable
rather than mysterious.

#### Bands snap to the contour interval

When a grid is displayed with contours, the colour bands default to **the contour levels
themselves**. Unaligned bands produce colour edges that wander across the contour lines and
look wrong to people who cannot say why; aligned, the map reads as one object.

One set of levels therefore drives three renderings, with nothing to keep in sync because
there is only one list:

| Rendering | What it is |
|---|---|
| Contour lines | LineStrings at the levels |
| Colour-filled grid | Discrete colormap with bands *at* those levels |
| Polygon bands | Filled contour output between the same levels |

The snap is on by default when contours are present and can be turned off — a gradient is the
better choice when the gradient itself is the message, as for porosity or saturation, where
banding invents boundaries the data does not have.

#### Clipping

A grid can be clipped to a polygon layer, or to selected features of one, with the sense
invertible so an area can be excluded as well as included. **The clip sets cells to nodata in
the grid**, either at gridding time as a job parameter or as a small job producing a derived
grid with lineage to both inputs.

Not a per-tile mask: masking each tile as it is proxied measured at 21 ms median, and a pan
touches around twenty tiles. That buys nothing except avoiding a duplicate COG of a few
megabytes, and costs the lineage and the ability to export the clipped surface.

Nothing changes in rendering, because nodata is already transparent.

Two things a clipped grid must recompute or it lies: its **extrapolation fraction**, since
clipping away an invented corner is a legitimate way to make a grid honest, and its **display
range**, or the legend spans values no longer on the map.

Clipping is also the cleanest answer to extrapolation. A **clip to the control** preset — the
convex hull of the control points, or everything within the search radius the diagnostics
already compute — removes the unsupported area rather than warning about it.

## 6. Schema-driven property editor

The escape hatch, and the clever part of covering the style spec without hand-writing hundreds
of form controls.

`@maplibre/maplibre-gl-style-spec` exports the specification as a runtime JSON object:
property names, types, ranges, defaults, units, and per-layer-type applicability.

```typescript
// packages/ui/src/PropertyEditor/generate.ts

import { latest as styleSpec } from '@maplibre/maplibre-gl-style-spec';

/**
 * Derive editable properties for a layer type directly from the spec.
 *
 * This is why "all MapLibre formatting options" is achievable: we do not
 * enumerate them, we read them. New properties in a MapLibre upgrade appear
 * in the editor automatically.
 *
 * It also solves geometry-appropriateness for free — the spec knows
 * fill-color exists on `fill` and not on `line`, so the editor cannot offer
 * a fill colour for a line layer.
 */
export function propertiesForLayerType(layerType: LayerType): PropertyDescriptor[] {
  const paint = styleSpec[`paint_${layerType}`] ?? {};
  const layout = styleSpec[`layout_${layerType}`] ?? {};

  return [
    ...Object.entries(paint).map(([name, def]) => describe(name, def, 'paint')),
    ...Object.entries(layout).map(([name, def]) => describe(name, def, 'layout')),
  ].filter((p) => !p.name.startsWith('visibility'));
}

function describe(name: string, def: any, group: 'paint' | 'layout'): PropertyDescriptor {
  return {
    name,
    group,
    control: controlFor(def),      // color | number | enum | boolean | array | formula
    min: def.minimum,
    max: def.maximum,
    default: def.default,
    options: def.values ? Object.keys(def.values) : undefined,
    units: def.units,
    supportsDataDriven: def['property-type'] === 'data-driven',
    doc: def.doc,                  // the spec carries documentation strings
  };
}
```

The generated editor groups properties by paint/layout and renders a control per type. Every
property gets a mode toggle: **constant** or **data-driven**. Data-driven opens the symbology
model rather than raw expression editing — nobody wants to hand-write
`["interpolate", ["linear"], ["get", "porosity"], ...]`.

A raw JSON view is available per layer for power users, validated against the spec on blur.

---

## 7. Style templates

Named, reusable symbology. The mechanism by which a company standard becomes real.

```typescript
export interface StyleTemplate {
  id: string;
  name: string;                  // 'Fault Traces — Company Standard'
  description?: string;
  schemaVersion: number;
  appliesToKind: DatasetKind;
  appliesToGeometry?: GeometryKind;
  symbology: Symbology;
  /** Field names the template expects. Applying to a dataset missing these
   *  degrades gracefully to single-symbol with a warning rather than
   *  failing. */
  requiredFields?: string[];
}
```

Resolution order when a layer is added:

1. Explicit `styleTemplateId` on the layer reference
2. User's `default_templates[kind]` preference
3. Team default template for that kind
4. Built-in default for the geometry type

Templates are ordinary owned objects, so `visibility: 'org'` publishes a standard to everyone
and grants can share a draft with one reviewer.

---

## 8. Legend generation

The legend derives from the symbology model, not from the compiled style. The model knows
class labels and units; the compiled style only knows numbers.

```typescript
// packages/ui/src/Legend/derive.ts

export function deriveLegend(
  symbology: Symbology,
  meta: LayerMetadata,
  palettes: Record<string, Palette>,
): LegendSpec {
  switch (symbology.type) {
    case 'continuous_raster':
      return {
        kind: 'colorbar',
        title: meta.name,
        unit: meta.valueRange?.unit,
        range: symbology.range ?? [meta.valueRange!.min, meta.valueRange!.max],
        palette: palettes[symbology.paletteId],
        // Ticks at pretty values, not at even fractions of the range.
        ticks: prettyTicks(...),
      };
    case 'graduated': {
      // Iterate classCount, NOT breaks. classify() returns n-1 interior breaks
      // for n classes and compileGraduated emits n colours; mapping over breaks
      // renders a 5-class map with a 4-entry legend. A legend that disagrees
      // with the map is the exact failure §3 avoids by compiling to `step`.
      const swatches = sampleRamp(palettes[symbology.paletteId], symbology.classCount);
      return {
        kind: 'classes',
        title: `${meta.name} (${meta.valueRange?.unit ?? ''})`,
        entries: Array.from({ length: symbology.classCount }, (_, i) => ({
          swatch: swatches[i],
          label: formatClassLabel(symbology.breaks, i, meta.valueRange?.unit),
        })),
      };
    }
    // ...
  }
}
```

The class count is the single source of truth for how many entries exist, in both the
compiler and the legend. A shared test vector should assert that
`compileSymbology(...).length` and `deriveLegend(...).entries.length` agree for every
graduated vector — this is exactly the kind of drift §3.1's cross-language suite exists
to catch.

Rendered by `@webmap/ui`, mounted in both the SPA and the render shell. One implementation —
which is the reason the render service screenshots the page rather than the canvas.

---

## 9. Testing

```typescript
// packages/style-model/src/compile.test.ts

describe('compileSymbology', () => {
  // Every vector in test-vectors/ runs in both TypeScript and Python.
  it.each(loadTestVectors())('$name compiles to expected layers', (vector) => {
    const layers = compileSymbology(
      vector.symbology, 'src', undefined, vector.palettes
    );
    expect(layers).toEqual(vector.expectedLayers);
  });

  it('produces spec-valid output for every vector', () => {
    for (const vector of loadTestVectors()) {
      const layers = compileSymbology(vector.symbology, 'src', undefined, vector.palettes);
      for (const layer of layers) {
        expect(validateStyleLayer(layer)).toEqual([]);   // no spec errors
      }
    }
  });
});
```

Validating against the real style spec catches an entire class of bug — an expression that
looks right but that MapLibre will reject at runtime, producing a silently blank layer.
