/**
 * The TypeScript half of the cross-language suite. `08-styling-palettes.md` §3.1.
 *
 * Every vector in `test-vectors/` runs here and in
 * `python/webmap_core/tests/test_style_vectors.py` against the same
 * hand-written expected output. A divergence fails CI in both languages,
 * which is the mechanism that keeps two compilers from drifting.
 */

import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { createPropertyExpression, validateStyleMin } from '@maplibre/maplibre-gl-style-spec';
import { compileSymbology } from './compile.js';
import type { CompiledLayer } from './compile.js';
import { colourAt, sampleRamp } from './palette.js';
import type { LabelSymbol, Palette, Symbology } from './symbology.js';

interface Vector {
  name: string;
  description: string;
  symbology: Symbology;
  palettes: Record<string, Palette>;
  source_id: string;
  source_layer?: string;
  expected_layers: CompiledLayer[];
}

const VECTOR_DIR = join(dirname(fileURLToPath(import.meta.url)), '..', 'test-vectors');

function loadVectors(): Vector[] {
  return readdirSync(VECTOR_DIR)
    .filter((name) => name.endsWith('.json'))
    .sort()
    .map((name) => JSON.parse(readFileSync(join(VECTOR_DIR, name), 'utf-8')) as Vector);
}

/**
 * The smallest valid style that can carry a vector's layers.
 *
 * The source type is inferred from what the layers ask for rather than
 * declared in the vector: a raster layer needs a raster source, and a
 * `source-layer` reference is only legal against a vector one. Declaring the
 * wrong kind produces validation errors about the source that would be read as
 * failures of the layers.
 */
function styleAround(vector: Vector, layers: CompiledLayer[]) {
  const source = layers.some((layer) => layer.type === 'raster')
    ? { type: 'raster' as const, tiles: ['https://tiles.test/{z}/{x}/{y}.png'], tileSize: 256 }
    : vector.source_layer
      ? { type: 'vector' as const, tiles: ['https://tiles.test/{z}/{x}/{y}.mvt'] }
      : { type: 'geojson' as const, data: { type: 'FeatureCollection', features: [] } };

  return {
    version: 8 as const,
    // Without this every symbol layer is a validation error, and labels are
    // the thing most worth validating.
    glyphs: 'https://webmap.test/glyphs/{fontstack}/{range}.pbf',
    sources: { [vector.source_id]: source },
    layers,
  };
}

describe('compileSymbology', () => {
  const vectors = loadVectors();

  it('finds the shared vectors', () => {
    // A suite that silently runs zero cases passes. This is the guard against
    // the vector directory moving and nobody noticing for a month.
    expect(vectors.length).toBeGreaterThanOrEqual(9);
  });

  it.each(vectors.map((v) => [v.name, v] as const))(
    '%s compiles to the expected layers',
    (_name, vector) => {
      const layers = compileSymbology(vector.symbology, {
        sourceId: vector.source_id,
        ...(vector.source_layer ? { sourceLayer: vector.source_layer } : {}),
        palettes: vector.palettes,
      });

      expect(layers).toEqual(vector.expected_layers);
    },
  );

  it.each(vectors.map((v) => [v.name, v] as const))(
    '%s compiles to something MapLibre accepts',
    (_name, vector) => {
      // `08-styling-palettes.md` §9. The vectors above check the compiled JSON
      // against what a human wrote down; this checks it against the renderer.
      // They catch different things: a layer can match its expected output
      // exactly and still be rejected at load time, and MapLibre rejects a
      // whole *style* rather than the offending layer — so one bad property
      // means a blank map, not a wrong colour.
      //
      // A property in the wrong block is the case that motivated this. It
      // reads correctly, survives review, and MapLibre refuses the style.
      const layers = compileSymbology(vector.symbology, {
        sourceId: vector.source_id,
        ...(vector.source_layer ? { sourceLayer: vector.source_layer } : {}),
        palettes: vector.palettes,
      });

      expect(validateStyleMin(styleAround(vector, layers) as never)).toEqual([]);
    },
  );

  it.each(vectors.map((v) => [v.name, v] as const))(
    '%s produces layers in a stable order',
    (_name, vector) => {
      // Order is load-bearing: a polygon's outline must paint over its fill,
      // and a line's casing must paint under its line. Comparing the id list
      // separately makes an ordering regression report as ordering rather
      // than as a wall of paint-property diffs.
      const layers = compileSymbology(vector.symbology, {
        sourceId: vector.source_id,
        ...(vector.source_layer ? { sourceLayer: vector.source_layer } : {}),
        palettes: vector.palettes,
      });

      expect(layers.map((l) => l.id)).toEqual(vector.expected_layers.map((l) => l.id));
    },
  );
});

describe('palette sampling', () => {
  const viridis: Palette = {
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

  it('puts the first and last classes at the ends of the ramp', () => {
    const colours = sampleRamp(viridis, 5);

    expect(colours[0]).toBe('#440154');
    expect(colours[4]).toBe('#fde725');
    expect(colours).toHaveLength(5);
  });

  it('gives a single class the midpoint, not an extreme', () => {
    // The ends of a diverging ramp are its extremes. One class coloured
    // "extreme low" would read as a value judgement the data does not support.
    expect(sampleRamp(viridis, 1)).toEqual(['#21918c']);
  });

  it('clamps positions outside 0..1 rather than extrapolating', () => {
    expect(colourAt(viridis, -1)).toBe('#440154');
    expect(colourAt(viridis, 2)).toBe('#fde725');
  });

  it('steps at stop boundaries for a discrete palette', () => {
    // A class boundary is a step. Blurring it would make the legend a lie in
    // the same way interpolating a graduated ramp would.
    const discrete: Palette = { ...viridis, interpolation: 'discrete' };

    expect(colourAt(discrete, 0.25)).toBe('#440154');
    expect(colourAt(discrete, 0.75)).toBe('#21918c');
    expect(colourAt(discrete, 1)).toBe('#fde725');
  });

  it('accepts three-digit hex and normalises the spelling', () => {
    // One spelling, lowercase, so string comparison against the Python
    // implementation is meaningful.
    const short: Palette = {
      ...viridis,
      stops: [
        { position: 0, color: '#F00' },
        { position: 1, color: '#00F' },
      ],
    };

    expect(colourAt(short, 0)).toBe('#ff0000');
    expect(colourAt(short, 1)).toBe('#0000ff');
  });

  it('takes the upper colour at coincident stops instead of dividing by zero', () => {
    // Coincident stops are a legitimate way to express a hard break in an
    // otherwise continuous ramp.
    const hardBreak: Palette = {
      ...viridis,
      stops: [
        { position: 0, color: '#000000' },
        { position: 0.5, color: '#ffffff' },
        { position: 0.5, color: '#ff0000' },
        { position: 1, color: '#00ff00' },
      ],
    };

    expect(colourAt(hardBreak, 0.5)).toBe('#ff0000');
  });

  it('rejects a colour it cannot reproduce in both languages', () => {
    const named: Palette = {
      ...viridis,
      stops: [
        { position: 0, color: 'rebeccapurple' },
        { position: 1, color: '#ffffff' },
      ],
    };

    expect(() => colourAt(named, 0)).toThrow(/not a hex colour/);
  });
});

describe('graduated guards', () => {
  const base: Palette = {
    id: 'p',
    name: 'p',
    isContinuous: true,
    interpolation: 'linear',
    stops: [
      { position: 0, color: '#000000' },
      { position: 1, color: '#ffffff' },
    ],
  };

  it('refuses a break count that disagrees with the class count', () => {
    // The off-by-one 08 §8 calls out: classify() returns n-1 interior breaks
    // for n classes, and a mismatch renders a 5-class map with a 4-entry
    // legend. Caught at compile rather than discovered on a slide.
    const symbology: Symbology = {
      type: 'graduated',
      field: 'porosity',
      method: 'pretty',
      classCount: 5,
      breaks: [5, 10, 15],
      paletteId: 'p',
      vary: 'color',
      baseSymbol: {
        geometry: 'polygon',
        fillColor: '#ccc',
        fillOpacity: 1,
        outlineColor: '#000',
        outlineWidth: 0,
      },
    };

    expect(() => compileSymbology(symbology, { sourceId: 's', palettes: { p: base } })).toThrow(
      /5 classes but 3 breaks/,
    );
  });

  it('refuses breaks that do not ascend', () => {
    // Breaks reach the compiler from classify(), which guarantees this — and
    // from a geologist typing them into the class table, which does not.
    // MapLibre rejects a `step` with non-ascending stops outright, so the
    // layer vanishes and nothing on screen says why.
    const symbology: Symbology = {
      type: 'graduated',
      field: 'porosity',
      method: 'manual',
      classCount: 4,
      breaks: [5, 15, 10],
      paletteId: 'p',
      vary: 'color',
      baseSymbol: {
        geometry: 'polygon',
        fillColor: '#ccc',
        fillOpacity: 1,
        outlineColor: '#000',
        outlineWidth: 0,
      },
    };

    expect(() => compileSymbology(symbology, { sourceId: 's', palettes: { p: base } })).toThrow(
      /must ascend strictly; break 2 \(10\)/,
    );
  });

  it('names the missing palette rather than rendering grey', () => {
    const symbology: Symbology = {
      type: 'graduated',
      field: 'porosity',
      method: 'pretty',
      classCount: 2,
      breaks: [5],
      paletteId: 'absent',
      vary: 'color',
      baseSymbol: {
        geometry: 'polygon',
        fillColor: '#ccc',
        fillOpacity: 1,
        outlineColor: '#000',
        outlineWidth: 0,
      },
    };

    expect(() => compileSymbology(symbology, { sourceId: 's', palettes: {} })).toThrow(
      /palette 'absent'/,
    );
  });
});

/**
 * Labels, checked against MapLibre's own machinery. `08-styling-palettes.md` §2.2.
 *
 * The shared vectors above pin the compiled JSON. These pin what MapLibre
 * does with it — the two label failures that produce no error message: a
 * layout property emitted into `paint`, which invalidates the whole style,
 * and a size ramp that is subtly not ground-constant, which nobody can see.
 */
describe('labels', () => {
  const label = (overrides: Partial<LabelSymbol> = {}): LabelSymbol => ({
    geometry: 'label',
    field: 'name',
    size: 12,
    sizeMode: { mode: 'fixed' },
    color: '#1a1a1a',
    haloColor: '#ffffff',
    haloWidth: 0,
    font: ['Oswald Regular'],
    placement: 'point',
    allowOverlap: true,
    ...overrides,
  });

  const RAMP: Palette = {
    id: 'viridis',
    name: 'Viridis',
    isContinuous: true,
    interpolation: 'linear',
    stops: [
      { position: 0, color: '#440154' },
      { position: 1, color: '#fde725' },
    ],
  };

  const styleAround = (layers: CompiledLayer[]) => ({
    version: 8 as const,
    glyphs: 'https://webmap.test/glyphs/{fontstack}/{range}.pbf',
    sources: {
      wells: { type: 'geojson' as const, data: { type: 'FeatureCollection', features: [] } },
    },
    layers,
  });

  it('emits a style MapLibre accepts', () => {
    const layers = compileSymbology(
      { type: 'single', symbol: label({ sizeMode: { mode: 'scale-with-map', referenceZoom: 12 } }) },
      { sourceId: 'wells', palettes: {} },
    );

    expect(validateStyleMin(styleAround(layers) as never)).toEqual([]);
  });

  it('varies size through layout, not paint', () => {
    // `text-size` is a LAYOUT property. Graduated symbology varies size, and
    // an override routed into `paint` makes MapLibre reject the entire style
    // — "unknown property text-size" — so the map does not load at all. It
    // did exactly that, because no test built a label.
    const layers = compileSymbology(
      {
        type: 'graduated',
        field: 'depth_ft',
        paletteId: 'viridis',
        classCount: 3,
        method: 'equal-interval',
        breaks: [1000, 2000],
        vary: 'size',
        baseSymbol: label(),
        sizeRange: [8, 20],
      },
      { sourceId: 'wells', palettes: { viridis: RAMP } },
    );

    const [layer] = layers;
    expect(layer?.layout?.['text-size']).toBeDefined();
    expect(layer?.paint?.['text-size']).toBeUndefined();
    expect(validateStyleMin(styleAround(layers) as never)).toEqual([]);
  });

  it('holds a reference-scale label at a constant size on the ground', () => {
    // The user-visible promise: 12 pt at zoom 12 means the text covers the
    // same distance at every zoom. Evaluated through MapLibre's own
    // expression engine rather than re-implementing the interpolation here,
    // because the thing under test is agreement with MapLibre.
    const [layer] = compileSymbology(
      {
        type: 'single',
        symbol: label({ size: 12, sizeMode: { mode: 'scale-with-map', referenceZoom: 12 } }),
      },
      { sourceId: 'wells', palettes: {} },
    );

    const expression = createPropertyExpression(layer?.layout?.['text-size'], {
      type: 'number',
      'property-type': 'data-driven',
      expression: { interpolated: true, parameters: ['zoom', 'feature'] },
    } as never);
    if (expression.result !== 'success') throw new Error('text-size is not a valid expression');

    // 12 pt is 16 px at 96/72, and one zoom level is a factor of two.
    for (const zoom of [4, 11, 12, 12.5, 13, 20]) {
      expect(expression.value.evaluate({ zoom }, undefined as never)).toBeCloseTo(
        16 * Math.pow(2, zoom - 12),
        10,
      );
    }
  });

  it('holds a fixed label at a constant size on screen', () => {
    const [layer] = compileSymbology(
      { type: 'single', symbol: label({ size: 12 }) },
      { sourceId: 'wells', palettes: {} },
    );

    // A plain number, not an expression: nothing to evaluate and nothing to
    // drift. 12 pt at 96/72 is 16 px, at every zoom.
    expect(layer?.layout?.['text-size']).toBe(16);
  });
});
