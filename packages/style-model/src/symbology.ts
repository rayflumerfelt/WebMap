/**
 * The symbology model — what geologists think in. `08-styling-palettes.md` §2.
 *
 * Two layers resolve the "implement every MapLibre formatting option" trap:
 * this model covers 95% of real use with concepts borrowed from QGIS, and a
 * schema-driven raw property editor covers the rest. Higher-level concepts
 * are *compilers* that emit Style JSON, never a parallel representation of
 * appearance (`01-architecture.md` §4.2).
 *
 * This package imports no React and no MapLibre GL. It is pure data
 * transformation, shared with tooling and testable in Node — enforced by
 * `boundaries/external` in eslint.config.js.
 */

export type ClassificationMethod =
  | 'equal_interval'
  | 'quantile'
  | 'natural_breaks' // Jenks
  | 'standard_deviation'
  | 'pretty' // round numbers — what geologists usually want
  | 'manual';

/** A data-driven value: read this attribute rather than a constant. */
export interface FieldRef {
  field: string;
}

/**
 * Symbol specs are geometry-aware, which resolves "points and lines have no
 * fill" structurally rather than with conditional UI. A `PointSymbol` has no
 * `fillColor` because points have no fill, so the type system prevents the
 * editor from offering one — more reliable than a runtime check.
 */
export interface PointSymbol {
  geometry: 'point';
  marker: 'circle' | 'square' | 'triangle' | 'cross' | 'sprite';
  size: number;
  color: string;
  strokeColor: string;
  strokeWidth: number;
  opacity: number;
  spriteName?: string; // when marker === 'sprite'
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
  /**
   * A second pass beneath the main line — casings for roads, fault
   * decorations, index-contour emphasis.
   */
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

/**
 * How a label behaves as the map zooms. Both are expressible in MapLibre and
 * neither is guessed — `08` §2.2 records the measurements.
 */
export type LabelSizeMode =
  /** The same size on **screen** at every zoom: 12 pt stays 12 pt. A constant
   *  `text-size`, and MapLibre's own default. */
  | { mode: 'fixed' }
  /** The same size on the **ground** — a reference scale. `size` is the size
   *  at `referenceZoom`; the label doubles with each zoom level in and halves
   *  with each one out, so it always covers the same distance. */
  | { mode: 'scale-with-map'; referenceZoom: number };

export interface LabelSymbol {
  geometry: 'label';
  /** Which attribute is drawn. Without one there is nothing to label. */
  field: string;
  /** **Points**, not pixels. MapLibre's `text-size` is in pixels, so the
   *  compiler converts at 96/72 — and the render service scales again for its
   *  2x output rather than baking a device ratio in here. */
  size: number;
  sizeMode: LabelSizeMode;
  color: string;
  /** Zero by default: halos muddy dense line work, and the imported labelling
   *  spec calls for none. Kept as a field because over a colour-filled grid a
   *  halo is the only thing that keeps text legible. See `08` §2.4. */
  haloColor: string;
  haloWidth: number;
  /** A MapLibre *font stack*, e.g. `['Oswald Bold']`. Bold and italic are
   *  separate stacks, not properties — see `08` §2.3. */
  font: string[];
  placement: 'point' | 'line' | 'line-center';
  /** Sets `text-allow-overlap` **and** `text-ignore-placement`. Both, or the
   *  layer keeps its own labels but still displaces another layer's. */
  allowOverlap: boolean;
  /** Zoom visibility, compiled to the layer's `minzoom`/`maxzoom`. With
   *  collision off this is the user's only thinning control. */
  minZoom?: number;
  maxZoom?: number;
}

export type SymbolSpec = PointSymbol | LineSymbol | PolygonSymbol | LabelSymbol;

export interface SingleSymbol {
  type: 'single';
  symbol: SymbolSpec;
}

export interface Categorized {
  type: 'categorized';
  field: string;
  categories: Array<{
    value: string | number | null;
    symbol: SymbolSpec;
    label: string;
  }>;
  other?: SymbolSpec; // fallback for unlisted values
}

export interface Graduated {
  type: 'graduated';
  field: string;
  method: ClassificationMethod;
  /**
   * The single source of truth for how many entries exist, in both the
   * compiler and the legend. `classify()` returns n-1 interior breaks for n
   * classes, so anything that iterates `breaks` renders a 5-class map with a
   * 4-entry legend (`08-styling-palettes.md` §8).
   */
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

export type Symbology =
  | SingleSymbol
  | Categorized
  | Graduated
  | RuleBased
  | ContinuousRaster;

export interface Palette {
  id: string;
  name: string;
  isContinuous: boolean;
  /** Positions normalised 0..1, ascending. */
  stops: Array<{ position: number; color: string }>;
  interpolation: 'linear' | 'discrete';
}

/**
 * How many legend entries and compiled classes a symbology produces.
 *
 * Exists so the compiler and the legend cannot disagree about the count —
 * the drift `08-styling-palettes.md` §8 calls out, where a 5-class map gets a
 * 4-entry legend because something mapped over `breaks` instead of
 * `classCount`.
 */
export function entryCount(symbology: Symbology): number {
  switch (symbology.type) {
    case 'single':
      return 1;
    case 'categorized':
      return symbology.categories.length + (symbology.other ? 1 : 0);
    case 'graduated':
      return symbology.classCount;
    case 'rules':
      return symbology.rules.length;
    case 'continuous_raster':
      // A colour bar is one continuous entry, not a count of swatches.
      return 1;
  }
}
