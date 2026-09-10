/**
 * Compile a symbology model into MapLibre layers. `08-styling-palettes.md` §3.
 *
 * One symbology may produce **several** MapLibre layers — a polygon with an
 * outline is a fill layer plus a line layer, a line with casing is two line
 * layers in order. Callers must not assume a 1:1 mapping.
 *
 * **This file and `webmap_core/style/compile.py` must produce identical
 * output.** Two implementations exist because the frontend needs synchronous
 * compilation for live preview while dragging a ramp stop, and the backend
 * needs it without a JS runtime (§3.1). The shared vectors in
 * `test-vectors/` are what keeps them honest; a divergence fails CI in both
 * languages.
 */

import { sampleRamp } from './palette.js';
import type {
  Categorized,
  ContinuousRaster,
  Graduated,
  LabelSymbol,
  LineSymbol,
  Palette,
  PointSymbol,
  PolygonSymbol,
  RuleBased,
  SingleSymbol,
  SymbolSpec,
  Symbology,
} from './symbology.js';

/**
 * A compiled MapLibre layer. Deliberately loose: the authority on this shape
 * is `@maplibre/maplibre-gl-style-spec`, and mirroring it here would create a
 * second definition to keep in step.
 */
export interface CompiledLayer {
  id: string;
  type: string;
  source: string;
  'source-layer'?: string;
  filter?: unknown[];
  paint?: Record<string, unknown>;
  layout?: Record<string, unknown>;
  minzoom?: number;
  maxzoom?: number;
}

export interface CompileOptions {
  sourceId: string;
  sourceLayer?: string;
  palettes: Record<string, Palette>;
  /** Prefix for generated layer ids. Defaults to the source id. */
  idPrefix?: string;
}

export function compileSymbology(
  symbology: Symbology,
  options: CompileOptions,
): CompiledLayer[] {
  switch (symbology.type) {
    case 'single':
      return compileSingle(symbology, options);
    case 'categorized':
      return compileCategorized(symbology, options);
    case 'graduated':
      return compileGraduated(symbology, options);
    case 'rules':
      return compileRules(symbology, options);
    case 'continuous_raster':
      return compileRaster(symbology, options);
  }
}

// --- per type ---------------------------------------------------------------

function compileSingle(s: SingleSymbol, o: CompileOptions): CompiledLayer[] {
  return layersFor(s.symbol, o, prefix(o), {});
}

function compileCategorized(s: Categorized, o: CompileOptions): CompiledLayer[] {
  // `match` rather than a chain of `case`: it is a lookup rather than a
  // sequence of comparisons, and MapLibre evaluates it as one.
  const byProperty = new Map<string, unknown[]>();
  const base = s.categories[0]?.symbol ?? s.other;
  if (!base) {
    throw new Error(
      `Categorized symbology on '${s.field}' has no categories and no ` +
        `fallback symbol, so it would render nothing.`,
    );
  }

  for (const property of variableProperties(base)) {
    const expression: unknown[] = ['match', ['get', s.field]];
    for (const category of s.categories) {
      // A null category matches the absence of the attribute, which `match`
      // cannot express — those fall through to the fallback instead.
      if (category.value === null) continue;
      expression.push(category.value, readProperty(category.symbol, property));
    }
    expression.push(readProperty(s.other ?? base, property));
    byProperty.set(property, expression);
  }

  return layersFor(base, o, prefix(o), Object.fromEntries(byProperty));
}

function compileGraduated(s: Graduated, o: CompileOptions): CompiledLayer[] {
  const palette = o.palettes[s.paletteId];
  if (!palette) {
    throw new Error(
      `Graduated symbology references palette '${s.paletteId}', which was not ` +
        `supplied. Pass every palette a layer uses; compilation cannot fetch ` +
        `one.`,
    );
  }
  if (s.breaks.length !== s.classCount - 1) {
    throw new Error(
      `Graduated symbology has ${s.classCount} classes but ` +
        `${s.breaks.length} breaks. classify() returns classCount - 1 interior ` +
        `breaks; a mismatch renders a map and a legend that disagree ` +
        `(08-styling-palettes.md §8).`,
    );
  }

  const outOfOrder = s.breaks.findIndex((brk, i) => i > 0 && brk <= s.breaks[i - 1]!);
  if (outOfOrder > 0) {
    // Breaks reach here from `classify()`, which already guarantees this — but
    // also from a geologist typing them into the class table, which does not.
    // A `step` expression with non-ascending stops is rejected by MapLibre
    // outright, so the layer disappears instead of rendering wrongly, and
    // nothing on screen says why.
    throw new Error(
      `Graduated breaks must ascend strictly; break ${outOfOrder} ` +
        `(${s.breaks[outOfOrder]}) is not above the one before it ` +
        `(${s.breaks[outOfOrder - 1]}). Edit the class table so each break is ` +
        `larger than the last.`,
    );
  }

  const overrides: Record<string, unknown[]> = {};

  if (s.vary === 'color' || s.vary === 'both') {
    const colours = sampleRamp(palette, s.classCount);
    // `step`, not `interpolate`: graduated classification is discrete by
    // definition, and interpolating would blur class boundaries and make the
    // legend a lie.
    const expression: unknown[] = ['step', ['get', s.field], colours[0]];
    s.breaks.forEach((brk, i) => expression.push(brk, colours[i + 1]));
    overrides[colourProperty(s.baseSymbol)] = expression;
  }

  if ((s.vary === 'size' || s.vary === 'both') && s.sizeRange) {
    const [low, high] = s.sizeRange;
    const sizes = Array.from({ length: s.classCount }, (_, i) =>
      s.classCount === 1 ? low : low + ((high - low) * i) / (s.classCount - 1),
    );
    const expression: unknown[] = ['step', ['get', s.field], sizes[0]];
    s.breaks.forEach((brk, i) => expression.push(brk, sizes[i + 1]));
    overrides[sizeProperty(s.baseSymbol)] = expression;
  }

  return layersFor(s.baseSymbol, o, prefix(o), overrides);
}

function compileRules(s: RuleBased, o: CompileOptions): CompiledLayer[] {
  // One layer set per rule, in order, so later rules paint over earlier ones —
  // which is what a rule list means to anyone coming from QGIS.
  return s.rules.flatMap((rule, index) => {
    const layers = layersFor(rule.symbol, o, `${prefix(o)}-rule-${index}`, {});
    return layers.map((layer) => ({
      ...layer,
      filter: rule.filter,
      ...(rule.minZoom !== undefined ? { minzoom: rule.minZoom } : {}),
      ...(rule.maxZoom !== undefined ? { maxzoom: rule.maxZoom } : {}),
    }));
  });
}

function compileRaster(s: ContinuousRaster, o: CompileOptions): CompiledLayer[] {
  // The colour ramp is *not* compiled into the style: it is a TiTiler URL
  // parameter on the source, which is what makes changing a palette a
  // parameter change rather than a regrid (01-architecture.md §2.5).
  return [
    {
      id: `${prefix(o)}-raster`,
      type: 'raster',
      source: o.sourceId,
      paint: { 'raster-opacity': s.opacity, 'raster-resampling': 'linear' },
    },
  ];
}

// --- symbol to layers -------------------------------------------------------

function layersFor(
  symbol: SymbolSpec,
  o: CompileOptions,
  id: string,
  overrides: Record<string, unknown>,
): CompiledLayer[] {
  switch (symbol.geometry) {
    case 'point':
      return [withSource({ id: `${id}-circle`, type: 'circle', ...pointPaint(symbol, overrides) }, o)];
    case 'line':
      return lineLayers(symbol, o, id, overrides);
    case 'polygon':
      return polygonLayers(symbol, o, id, overrides);
    case 'label':
      return [
        withSource(
          {
            id: `${id}-label`,
            type: 'symbol',
            ...(symbol.minZoom !== undefined ? { minzoom: symbol.minZoom } : {}),
            ...(symbol.maxZoom !== undefined ? { maxzoom: symbol.maxZoom } : {}),
            ...labelSpec(symbol, overrides),
          },
          o,
        ),
      ];
  }
}

function pointPaint(s: PointSymbol, overrides: Record<string, unknown>) {
  return {
    paint: {
      'circle-radius': s.size,
      'circle-color': s.color,
      'circle-stroke-color': s.strokeColor,
      'circle-stroke-width': s.strokeWidth,
      'circle-opacity': s.opacity,
      ...overrides,
    },
  };
}

function lineLayers(
  s: LineSymbol,
  o: CompileOptions,
  id: string,
  overrides: Record<string, unknown>,
): CompiledLayer[] {
  const layers: CompiledLayer[] = [];
  if (s.casing) {
    // Emitted first so it paints beneath. A casing above its line is a
    // hairline outline instead of a road edge.
    layers.push(
      withSource(
        {
          id: `${id}-casing`,
          type: 'line',
          layout: { 'line-cap': s.cap, 'line-join': s.join },
          paint: { 'line-color': s.casing.color, 'line-width': s.casing.width },
        },
        o,
      ),
    );
  }
  layers.push(
    withSource(
      {
        id: `${id}-line`,
        type: 'line',
        layout: { 'line-cap': s.cap, 'line-join': s.join },
        paint: {
          'line-color': s.color,
          'line-width': s.width,
          'line-opacity': s.opacity,
          ...(s.dashArray ? { 'line-dasharray': s.dashArray } : {}),
          ...overrides,
        },
      },
      o,
    ),
  );
  return layers;
}

function polygonLayers(
  s: PolygonSymbol,
  o: CompileOptions,
  id: string,
  overrides: Record<string, unknown>,
): CompiledLayer[] {
  const layers: CompiledLayer[] = [
    withSource(
      {
        id: `${id}-fill`,
        type: 'fill',
        paint: {
          'fill-color': s.fillColor,
          'fill-opacity': s.fillOpacity,
          ...(s.fillPattern ? { 'fill-pattern': s.fillPattern } : {}),
          ...overrides,
        },
      },
      o,
    ),
  ];
  if (s.outlineWidth > 0) {
    // A separate line layer rather than `fill-outline-color`, which MapLibre
    // renders at exactly 1px and ignores width and dashes on.
    layers.push(
      withSource(
        {
          id: `${id}-outline`,
          type: 'line',
          paint: {
            'line-color': s.outlineColor,
            'line-width': s.outlineWidth,
            ...(s.outlineDashArray ? { 'line-dasharray': s.outlineDashArray } : {}),
          },
        },
        o,
      ),
    );
  }
  return layers;
}

/** CSS points to pixels. A cartographer specifies points; `text-size` is in
 *  pixels, and the two differ by enough to matter at label sizes. */
const PT_TO_PX = 96 / 72;

/**
 * How far a reference-scale ramp reaches either side of its reference zoom.
 *
 * `interpolate` **clamps** outside its stop range rather than extrapolating,
 * so the span has to cover every zoom the map will ever reach; MapLibre's
 * maximum is 24, so 24 levels either way can never be hit.
 *
 * The stops are written *relative to the reference zoom* so that every size
 * multiplier is an exact power of two — `2**-24`, `1`, `2**24`. Computing them
 * as `pow(2, stop - reference)` instead would put a non-integer exponent
 * through libm in two languages, and the parity vectors compare bytes.
 */
const REFERENCE_SPAN = 24;

/**
 * `text-size` for a label, in pixels.
 *
 * Ground-constant text is an `interpolate` with `["exponential", 2]` on zoom,
 * which is **exact** rather than an approximation: MapLibre's exponential
 * factor is `(b**(z-z0) - 1) / (b**(z1-z0) - 1)`, and with `b = 2` and stops
 * a power of two apart that reduces to `size * 2**(z - reference)` — measured
 * to zero relative error at integer and fractional zooms alike.
 */
function textSize(s: LabelSymbol): number | unknown[] {
  const px = s.size * PT_TO_PX;
  if (s.sizeMode.mode === 'fixed') return px;
  const reference = s.sizeMode.referenceZoom;
  const factor = Math.pow(2, REFERENCE_SPAN);
  return [
    'interpolate',
    ['exponential', 2],
    ['zoom'],
    reference - REFERENCE_SPAN,
    px / factor,
    reference,
    px,
    reference + REFERENCE_SPAN,
    px * factor,
  ];
}

/** `text-size` is a *layout* property. A graduated symbology varies size, and
 *  routing that override into `paint` produces a style MapLibre rejects
 *  outright — "unknown property text-size" — so the whole map fails to load. */
const SYMBOL_LAYOUT_OVERRIDES = new Set(['text-size', 'text-font', 'text-field']);

function labelSpec(s: LabelSymbol, overrides: Record<string, unknown>) {
  const layoutOverrides: Record<string, unknown> = {};
  const paintOverrides: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(overrides)) {
    (SYMBOL_LAYOUT_OVERRIDES.has(key) ? layoutOverrides : paintOverrides)[key] = value;
  }

  return {
    layout: {
      'text-field': ['get', s.field],
      'text-size': textSize(s),
      'text-font': s.font,
      'symbol-placement': s.placement,
      ...(s.rotateField
        ? {
            'text-rotate': ['get', s.rotateField],
            // Map-aligned, or the rotation is applied in screen space and the
            // label holds its angle while the map turns under it.
            'text-rotation-alignment': 'map',
          }
        : {}),
      // Both flags, deliberately. `text-allow-overlap` stops this layer's own
      // labels being dropped; `text-ignore-placement` keeps them out of the
      // collision index so they cannot displace another layer's. Setting only
      // the first still lets a section grid delete the labels above it.
      'text-allow-overlap': s.allowOverlap,
      'text-ignore-placement': s.allowOverlap,
      ...layoutOverrides,
    },
    paint: {
      'text-color': s.color,
      'text-halo-color': s.haloColor,
      'text-halo-width': s.haloWidth,
      ...paintOverrides,
    },
  };
}

function withSource(
  layer: Omit<CompiledLayer, 'source'>,
  o: CompileOptions,
): CompiledLayer {
  return {
    ...layer,
    source: o.sourceId,
    ...(o.sourceLayer ? { 'source-layer': o.sourceLayer } : {}),
  };
}

// --- property naming --------------------------------------------------------

/** Which paint property a data-driven colour writes to, per geometry. */
function colourProperty(symbol: SymbolSpec): string {
  switch (symbol.geometry) {
    case 'point':
      return 'circle-color';
    case 'line':
      return 'line-color';
    case 'polygon':
      return 'fill-color';
    case 'label':
      return 'text-color';
  }
}

function sizeProperty(symbol: SymbolSpec): string {
  switch (symbol.geometry) {
    case 'point':
      return 'circle-radius';
    case 'line':
      return 'line-width';
    case 'polygon':
      // Polygons have no size. Varying one by a value means the outline.
      return 'line-width';
    case 'label':
      return 'text-size';
  }
}

/** The properties a categorized symbology varies across its categories. */
function variableProperties(symbol: SymbolSpec): string[] {
  return [colourProperty(symbol)];
}

function readProperty(symbol: SymbolSpec, property: string): unknown {
  switch (property) {
    case 'circle-color':
      return (symbol as PointSymbol).color;
    case 'line-color':
      return (symbol as LineSymbol).color;
    case 'fill-color':
      return (symbol as PolygonSymbol).fillColor;
    case 'text-color':
      return (symbol as LabelSymbol).color;
    default:
      throw new Error(`No reader for varied property '${property}'.`);
  }
}

function prefix(o: CompileOptions): string {
  return o.idPrefix ?? o.sourceId;
}
