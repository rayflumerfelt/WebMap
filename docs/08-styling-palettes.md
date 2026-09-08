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
```

**No conditional fields.** A `PointSymbol` has no `fillColor` because points have no fill.
The type system prevents the UI from offering it, which is more reliable than a runtime check.

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
    case 'graduated':
      return {
        kind: 'classes',
        title: `${meta.name} (${meta.valueRange?.unit ?? ''})`,
        entries: symbology.breaks.map((_, i) => ({
          swatch: sampleRamp(palettes[symbology.paletteId], symbology.classCount)[i],
          label: formatClassLabel(symbology.breaks, i, meta.valueRange?.unit),
        })),
      };
    // ...
  }
}
```

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
