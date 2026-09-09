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

import { compileSymbology } from './compile.js';
import type { CompiledLayer } from './compile.js';
import { colourAt, sampleRamp } from './palette.js';
import type { Palette, Symbology } from './symbology.js';

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
