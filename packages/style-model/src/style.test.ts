/**
 * Style assembly. `07-frontend.md` §3.1.
 *
 * The properties here are the ones a broken map does not announce: a layer
 * painted under the basemap, a hidden layer still fetching tiles, an opacity
 * slider that erased a deliberate styling choice. Each renders as something
 * that looks like a data problem.
 */

import { describe, expect, it } from 'vitest';

import { compileStyle } from './style.js';
import type { Basemap, StyleLayer } from './style.js';
import type { Palette, Symbology } from './symbology.js';

const VIRIDIS: Palette = {
  id: 'viridis',
  name: 'Viridis',
  isContinuous: true,
  interpolation: 'linear',
  stops: [
    { position: 0, color: '#440154' },
    { position: 0.5, color: '#21918c' },
    { position: 1, color: '#fde725' },
  ],
};

const POLYGON: Symbology = {
  type: 'single',
  symbol: {
    geometry: 'polygon',
    fillColor: '#88a0c8',
    fillOpacity: 0.8,
    outlineColor: '#213547',
    outlineWidth: 1,
  },
};

const POINTS: Symbology = {
  type: 'single',
  symbol: {
    geometry: 'point',
    marker: 'circle',
    size: 4,
    color: '#e41a1c',
    strokeColor: '#ffffff',
    strokeWidth: 1,
    opacity: 1,
  },
};

function vectorLayer(id: string, overrides: Partial<StyleLayer> = {}): StyleLayer {
  return {
    id,
    source: {
      datasetId: `ds-${id}`,
      kind: 'vector',
      url: `https://webmap.test/tiles/ds-${id}/{z}/{x}/{y}.mvt?token=abc`,
      sourceLayer: 'features',
    },
    symbology: POLYGON,
    ...overrides,
  };
}

const BASEMAP: Basemap = {
  id: 'grey',
  sources: { basemap: { type: 'raster', tiles: ['https://tiles.test/{z}/{x}/{y}.png'] } },
  layers: [{ id: 'basemap-raster', type: 'raster', source: 'basemap' }],
};

describe('compileStyle', () => {
  it('produces a style MapLibre will accept', () => {
    const style = compileStyle({ layers: [vectorLayer('a')], palettes: {} });

    expect(style.version).toBe(8);
    expect(Object.keys(style.sources)).toEqual(['src-a']);
    expect(style.layers.map((l) => l.id)).toEqual(['a-fill', 'a-outline']);
  });

  it('paints the basemap underneath everything', () => {
    // Not a layer in the session's list: a geologist reordering their layers
    // must not be able to put the basemap on top of their data.
    const style = compileStyle({
      layers: [vectorLayer('a', { z: -100 })],
      palettes: {},
      basemap: BASEMAP,
    });

    expect(style.layers[0]!.id).toBe('basemap-raster');
    expect(style.sources).toHaveProperty('basemap');
  });

  it('orders layers by z, ascending', () => {
    const style = compileStyle({
      layers: [vectorLayer('top', { z: 10 }), vectorLayer('bottom', { z: 1 })],
      palettes: {},
    });

    expect(style.layers.map((l) => l.id)).toEqual([
      'bottom-fill',
      'bottom-outline',
      'top-fill',
      'top-outline',
    ]);
  });

  it('is byte-identical for the same session compiled twice', () => {
    // What lets the render service's output be compared against the browser's,
    // and what stops a useMemo recompile reordering layers under the cursor.
    const layers = [vectorLayer('b', { z: 1 }), vectorLayer('a', { z: 1 })];

    const first = compileStyle({ layers, palettes: {} });
    const second = compileStyle({ layers: [...layers].reverse(), palettes: {} });

    expect(JSON.stringify(first)).toBe(JSON.stringify(second));
  });

  it('omits a hidden layer entirely rather than setting visibility none', () => {
    // A hidden layer still costs tile requests while it is in the style. A
    // geologist unticking a 500k-feature layer expects it to stop loading.
    const style = compileStyle({
      layers: [vectorLayer('shown'), vectorLayer('hidden', { visible: false })],
      palettes: {},
    });

    expect(style.layers.map((l) => l.id)).toEqual(['shown-fill', 'shown-outline']);
    expect(style.sources).not.toHaveProperty('src-hidden');
  });

  it('gives every layer its own source, so one dataset cannot alias another', () => {
    const style = compileStyle({
      layers: [vectorLayer('a'), vectorLayer('b')],
      palettes: {},
    });

    expect(Object.keys(style.sources).sort()).toEqual(['src-a', 'src-b']);
    expect(style.layers.every((l) => l.source === `src-${l.id[0]}`)).toBe(true);
  });
});

describe('sources', () => {
  it('passes the tile URL through untouched, credentials and all', () => {
    // This package knows nothing about API base URLs or tile tokens. Rewriting
    // a URL here would strip the scoped token and every tile would 401.
    const url = 'https://webmap.test/tiles/ds-a/{z}/{x}/{y}.mvt?token=abc.def';
    const style = compileStyle({
      layers: [vectorLayer('a', { source: { ...vectorLayer('a').source, url } })],
      palettes: {},
    });

    expect((style.sources['src-a'] as { tiles: string[] }).tiles).toEqual([url]);
  });

  it('declares raster tiles at 256px, not 512', () => {
    // TiTiler's WebMercatorQuad renders 256px tiles. Declaring 512 stretches
    // every tile to double size, which reads as a blurry grid rather than as
    // a configuration mistake.
    const style = compileStyle({
      layers: [
        {
          id: 'grid',
          source: {
            datasetId: 'ds-grid',
            kind: 'raster',
            url: 'https://webmap.test/cog/ds-grid/{z}/{x}/{y}.png',
          },
          symbology: { type: 'continuous_raster', paletteId: 'viridis', range: null, clamp: true, opacity: 1 },
        },
      ],
      palettes: { viridis: VIRIDIS },
    });

    expect(style.sources['src-grid']).toMatchObject({ type: 'raster', tileSize: 256 });
  });

  it('gives a geojson source a URL rather than inlining the features', () => {
    const style = compileStyle({
      layers: [
        {
          id: 'picks',
          source: {
            datasetId: 'ds-picks',
            kind: 'geojson',
            url: 'https://webmap.test/features/ds-picks.geojson',
          },
          symbology: POINTS,
        },
      ],
      palettes: {},
    });

    expect(style.sources['src-picks']).toEqual({
      type: 'geojson',
      data: 'https://webmap.test/features/ds-picks.geojson',
    });
    // No source-layer: a GeoJSON source has exactly one, unnamed.
    expect(style.layers[0]).not.toHaveProperty('source-layer');
  });
});

describe('layer opacity', () => {
  it('multiplies the symbology opacity rather than replacing it', () => {
    // A polygon styled at 0.8 fill in a layer set to 0.5 lands at 0.4.
    // Replacing it would make the slider erase a deliberate styling choice
    // the moment it was touched.
    const style = compileStyle({
      layers: [vectorLayer('a', { opacity: 0.5 })],
      palettes: {},
    });

    const fill = style.layers.find((l) => l.id === 'a-fill')!;
    expect(fill.paint!['fill-opacity']).toBeCloseTo(0.4);
  });

  it('scales a data-driven opacity expression instead of discarding it', () => {
    const symbology: Symbology = {
      type: 'graduated',
      field: 'confidence',
      method: 'manual',
      classCount: 2,
      breaks: [0.5],
      paletteId: 'viridis',
      vary: 'color',
      baseSymbol: {
        geometry: 'polygon',
        fillColor: '#cccccc',
        fillOpacity: 1,
        outlineColor: '#000000',
        outlineWidth: 0,
      },
    };
    const style = compileStyle({
      layers: [vectorLayer('a', { symbology, opacity: 0.5 })],
      palettes: { viridis: VIRIDIS },
    });

    const fill = style.layers.find((l) => l.id === 'a-fill')!;
    // fill-opacity is a plain number here; fill-color is the expression. The
    // point is that neither is lost.
    expect(fill.paint!['fill-opacity']).toBeCloseTo(0.5);
    expect(Array.isArray(fill.paint!['fill-color'])).toBe(true);
  });

  it('leaves paint untouched at full opacity', () => {
    const opaque = compileStyle({ layers: [vectorLayer('a', { opacity: 1 })], palettes: {} });
    const unset = compileStyle({ layers: [vectorLayer('a')], palettes: {} });

    expect(JSON.stringify(opaque)).toBe(JSON.stringify(unset));
  });

  it('applies to a raster layer through raster-opacity', () => {
    const style = compileStyle({
      layers: [
        {
          id: 'grid',
          source: {
            datasetId: 'ds-grid',
            kind: 'raster',
            url: 'https://webmap.test/cog/{z}/{x}/{y}.png',
          },
          symbology: { type: 'continuous_raster', paletteId: 'viridis', range: null, clamp: true, opacity: 0.9 },
          opacity: 0.5,
        },
      ],
      palettes: { viridis: VIRIDIS },
    });

    expect(style.layers[0]!.paint!['raster-opacity']).toBeCloseTo(0.45);
  });
});

describe('zoom range', () => {
  it('applies the layer zoom range when the symbology sets none', () => {
    const style = compileStyle({
      layers: [vectorLayer('a', { minzoom: 6, maxzoom: 16 })],
      palettes: {},
    });

    expect(style.layers.every((l) => l.minzoom === 6 && l.maxzoom === 16)).toBe(true);
  });

  it('lets a rule-based symbology keep its own zoom range', () => {
    // A rule saying "labels above zoom 12" is more specific than a layer-wide
    // range, and the layer range must not overwrite it.
    const symbology: Symbology = {
      type: 'rules',
      rules: [
        { filter: ['==', ['get', 'kind'], 'major'], symbol: POINTS.symbol, label: 'Major', minZoom: 12 },
      ],
    };
    const style = compileStyle({
      layers: [vectorLayer('a', { symbology, minzoom: 6 })],
      palettes: {},
    });

    expect(style.layers[0]!.minzoom).toBe(12);
  });
});

describe('fonts', () => {
  it('carries the glyph endpoint through', () => {
    // MapLibre renders no label at all without this, and says nothing about
    // why — the labels are simply absent.
    const style = compileStyle({
      layers: [vectorLayer('a')],
      palettes: {},
      glyphs: 'https://webmap.test/fonts/{fontstack}/{range}.pbf',
    });

    expect(style.glyphs).toBe('https://webmap.test/fonts/{fontstack}/{range}.pbf');
  });

  it('omits glyphs and sprite when unset rather than emitting undefined', () => {
    const style = compileStyle({ layers: [vectorLayer('a')], palettes: {} });

    expect('glyphs' in style).toBe(false);
    expect('sprite' in style).toBe(false);
  });
});
