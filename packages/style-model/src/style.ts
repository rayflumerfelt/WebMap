/**
 * Assemble a complete MapLibre style from a session's layers.
 * `07-frontend.md` §3.1, `08-styling-palettes.md` §3.
 *
 * **The style is derived, never stored.** Storing both a symbology model and
 * a compiled style guarantees the two drift, and the compiled one wins on
 * screen while the model wins in the legend — so the map and its legend
 * disagree. `compileStyle` is a pure function of the session, called from a
 * `useMemo`; there is nowhere for a stale copy to live.
 *
 * This is the function §3.1 names, and the reason `compileSymbology` has a
 * Python port: the render service assembles the same style from the same
 * session without a JavaScript runtime.
 */

import { compileSymbology } from './compile.js';
import type { CompiledLayer } from './compile.js';
import type { Palette, Symbology } from './symbology.js';

/** Where a layer's data comes from. One dataset, one source. */
export interface LayerSource {
  datasetId: string;
  /** 'vector' for the MVT endpoint, 'geojson' below the switch threshold,
   *  'raster' for a COG through the TiTiler proxy. */
  kind: 'vector' | 'geojson' | 'raster';
  /** Fully-formed URL template, credentials included. The app builds these:
   *  this package knows nothing about API base URLs or tile tokens. */
  url: string;
  /** MVT layer name inside the tile. Absent for geojson and raster. */
  sourceLayer?: string;
  minzoom?: number;
  maxzoom?: number;
  bounds?: [number, number, number, number];
  attribution?: string;
}

export interface StyleLayer {
  id: string;
  source: LayerSource;
  symbology: Symbology;
  /** Multiplies every opacity the symbology sets, so the layer-tree slider
   *  works without rewriting the symbology model. */
  opacity?: number;
  visible?: boolean;
  /** Draw order, ascending. Later layers paint over earlier ones. */
  z?: number;
  minzoom?: number;
  maxzoom?: number;
}

export interface Basemap {
  id: string;
  /** Sources and layers to place beneath everything else. Supplied whole
   *  rather than named, so an air-gapped deployment can point at its own. */
  sources: Record<string, unknown>;
  layers: CompiledLayer[];
}

export interface CompileStyleOptions {
  layers: StyleLayer[];
  palettes: Record<string, Palette>;
  basemap?: Basemap;
  /** Font stack endpoint. MapLibre needs this before it will render a label;
   *  without it every label silently disappears. */
  glyphs?: string;
  sprite?: string;
}

export interface CompiledStyle {
  version: 8;
  glyphs?: string;
  sprite?: string;
  sources: Record<string, unknown>;
  layers: CompiledLayer[];
}

export function compileStyle(options: CompileStyleOptions): CompiledStyle {
  const { palettes, basemap } = options;

  const sources: Record<string, unknown> = { ...(basemap?.sources ?? {}) };
  // The basemap paints first, underneath everything. Not a layer in the
  // session's list, because a geologist reordering their layers should never
  // be able to put the basemap on top of their data.
  const layers: CompiledLayer[] = [...(basemap?.layers ?? [])];

  const ordered = [...options.layers].sort(byDrawOrder);

  for (const layer of ordered) {
    if (layer.visible === false) {
      // Omitted entirely rather than emitted with `visibility: none`. A hidden
      // layer still costs tile requests when it is in the style, and a
      // geologist unticking a 500k-feature layer expects it to stop loading.
      continue;
    }

    const sourceId = `src-${layer.id}`;
    sources[sourceId] = sourceSpec(layer.source);

    const compiled = compileSymbology(layer.symbology, {
      sourceId,
      ...(layer.source.sourceLayer ? { sourceLayer: layer.source.sourceLayer } : {}),
      palettes,
      idPrefix: layer.id,
    });

    for (const compiledLayer of compiled) {
      layers.push(withLayerOverrides(compiledLayer, layer));
    }
  }

  return {
    version: 8,
    ...(options.glyphs ? { glyphs: options.glyphs } : {}),
    ...(options.sprite ? { sprite: options.sprite } : {}),
    sources,
    layers,
  };
}

/**
 * Stable draw order.
 *
 * Ties break on layer id rather than array position so that compiling the
 * same session twice gives byte-identical output — which is what lets the
 * render service's result be compared against the browser's, and what stops a
 * `useMemo` recompile from reordering layers under the user's cursor.
 */
function byDrawOrder(a: StyleLayer, b: StyleLayer): number {
  const az = a.z ?? 0;
  const bz = b.z ?? 0;
  return az - bz || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
}

function sourceSpec(source: LayerSource): Record<string, unknown> {
  const shared = {
    ...(source.attribution ? { attribution: source.attribution } : {}),
    ...(source.bounds ? { bounds: source.bounds } : {}),
  };

  switch (source.kind) {
    case 'vector':
      return {
        type: 'vector',
        tiles: [source.url],
        ...(source.minzoom !== undefined ? { minzoom: source.minzoom } : {}),
        ...(source.maxzoom !== undefined ? { maxzoom: source.maxzoom } : {}),
        ...shared,
      };
    case 'geojson':
      // `data` as a URL, not inlined: MapLibre fetches and parses it off the
      // critical path, and the app never has to hold the FeatureCollection.
      return { type: 'geojson', data: source.url, ...shared };
    case 'raster':
      return {
        type: 'raster',
        tiles: [source.url],
        // 256, not 512: TiTiler's WebMercatorQuad renders 256px tiles, and
        // declaring 512 stretches every tile to double size — which looks
        // like a blurry grid rather than like a configuration error.
        tileSize: 256,
        ...(source.minzoom !== undefined ? { minzoom: source.minzoom } : {}),
        ...(source.maxzoom !== undefined ? { maxzoom: source.maxzoom } : {}),
        ...shared,
      };
  }
}

/**
 * Apply the per-layer controls the layer tree owns: opacity and zoom range.
 *
 * Opacity **multiplies** whatever the symbology set rather than replacing it,
 * so a polygon styled at 0.8 fill in a layer set to 0.5 lands at 0.4. Setting
 * it outright would make the slider erase a deliberate styling choice the
 * moment it was touched.
 */
function withLayerOverrides(compiled: CompiledLayer, layer: StyleLayer): CompiledLayer {
  const result: CompiledLayer = { ...compiled };

  if (layer.minzoom !== undefined && result.minzoom === undefined) {
    result.minzoom = layer.minzoom;
  }
  if (layer.maxzoom !== undefined && result.maxzoom === undefined) {
    result.maxzoom = layer.maxzoom;
  }

  const opacity = layer.opacity;
  if (opacity === undefined || opacity === 1) return result;

  const property = OPACITY_PROPERTY[result.type];
  if (!property) return result;

  const paint = { ...(result.paint ?? {}) };
  const existing = paint[property];
  if (typeof existing === 'number') {
    paint[property] = existing * opacity;
  } else if (existing === undefined) {
    paint[property] = opacity;
  } else {
    // A data-driven opacity expression. Multiplying inside the expression
    // keeps both: the per-feature variation the symbology asked for, scaled
    // by the layer slider.
    paint[property] = ['*', existing, opacity];
  }
  result.paint = paint;
  return result;
}

const OPACITY_PROPERTY: Record<string, string | undefined> = {
  fill: 'fill-opacity',
  line: 'line-opacity',
  circle: 'circle-opacity',
  symbol: 'text-opacity',
  raster: 'raster-opacity',
};
