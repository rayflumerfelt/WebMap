/**
 * Legend derivation. `08-styling-palettes.md` §8.
 *
 * The centrepiece is `legends and compiled layers agree on the class count`,
 * run over every shared vector — the assertion §8 asks for by name. A legend
 * that disagrees with the map it describes is the failure mode this whole
 * area of the codebase is arranged to prevent.
 */

import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { classLabel, deriveLegend, legendEntryCount, prettyTicks } from './legend.js';
import type { Palette, Symbology } from './symbology.js';
import { entryCount } from './symbology.js';

const VECTOR_DIR = join(dirname(fileURLToPath(import.meta.url)), '..', 'test-vectors');

interface Vector {
  name: string;
  symbology: Symbology;
  palettes: Record<string, Palette>;
  expected_layers: Array<{ id: string }>;
}

const vectors: Vector[] = readdirSync(VECTOR_DIR)
  .filter((n) => n.endsWith('.json'))
  .sort()
  .map((n) => JSON.parse(readFileSync(join(VECTOR_DIR, n), 'utf-8')) as Vector);

const META = { name: 'Porosity', valueRange: { min: 4.1, max: 21.8, unit: '%' } };

describe('deriveLegend', () => {
  it.each(vectors.map((v) => [v.name, v] as const))(
    '%s: the legend and the compiled style agree on how many classes exist',
    (_name, vector) => {
      // §8, verbatim: "A shared test vector should assert that
      // compileSymbology(...).length and deriveLegend(...).entries.length
      // agree for every graduated vector." Widened to every vector, because
      // the off-by-one is not specific to graduated — it is specific to
      // anything that iterates a derived list instead of the class count.
      const legend = deriveLegend(vector.symbology, META, vector.palettes);
      const expected = entryCount(vector.symbology);

      if (legend.kind === 'classes') {
        expect(legend.entries).toHaveLength(expected);
      } else {
        // A colour bar is one continuous entry, not a count of swatches.
        expect(expected).toBe(1);
      }
      expect(legendEntryCount(vector.symbology)).toBe(expected);
    },
  );

  it('gives a graduated legend one entry per class, not one per break', () => {
    // The specific bug §8 names. Five classes, four breaks: a legend built by
    // mapping over `breaks` has four entries and is wrong on a slide before
    // anyone notices.
    const graduated = vectors.find((v) => v.name === 'graduated_polygons')!;

    const legend = deriveLegend(graduated.symbology, META, graduated.palettes);

    expect(legend.kind).toBe('classes');
    if (legend.kind !== 'classes') return;
    expect(legend.entries).toHaveLength(5);
    expect((graduated.symbology as { breaks: number[] }).breaks).toHaveLength(4);
  });

  it('takes swatch colours from the same ramp the compiler samples', () => {
    // Not merely the same count — the same colours. A legend whose swatches
    // came from a second sampling pass would drift the moment either changed.
    const graduated = vectors.find((v) => v.name === 'graduated_polygons')!;
    const compiled = graduated.expected_layers.find((l) => l.id.endsWith('-fill'))!;
    const step = (compiled as unknown as { paint: Record<string, unknown[]> }).paint[
      'fill-color'
    ]!;
    // ['step', ['get', f], c0, b0, c1, b1, ...] — colours sit at even indices
    // from 2, breaks at the odd ones between them.
    const fromStyle = step.filter((_, i) => i >= 2 && i % 2 === 0);

    const legend = deriveLegend(graduated.symbology, META, graduated.palettes);

    if (legend.kind !== 'classes') throw new Error('expected a class legend');
    expect(legend.entries.map((e) => e.swatch)).toEqual(fromStyle);
  });

  it('labels the fallback category rather than leaving it unexplained', () => {
    // A reader seeing an unlabelled colour on the map has no way to learn it
    // means "everything else".
    const symbology: Symbology = {
      type: 'categorized',
      field: 'fault_type',
      categories: [{ value: 'normal', symbol: line('#e41a1c'), label: 'Normal' }],
      other: line('#999999'),
    };

    const legend = deriveLegend(symbology, { name: 'Faults' }, {});

    if (legend.kind !== 'classes') throw new Error('expected a class legend');
    expect(legend.entries.map((e) => e.label)).toEqual(['Normal', 'Other']);
  });

  it('carries the unit into the title and the class labels', () => {
    const graduated = vectors.find((v) => v.name === 'graduated_polygons')!;

    const legend = deriveLegend(graduated.symbology, META, graduated.palettes);

    if (legend.kind !== 'classes') throw new Error('expected a class legend');
    expect(legend.title).toBe('Porosity (%)');
    expect(legend.entries[0]!.label).toContain('%');
  });

  it('names the palette it was not given rather than drawing grey swatches', () => {
    const graduated = vectors.find((v) => v.name === 'graduated_polygons')!;

    expect(() => deriveLegend(graduated.symbology, META, {})).toThrow(/was not supplied/);
  });

  it('refuses a colour bar with no range instead of inventing 0..1', () => {
    const raster = vectors.find((v) => v.name === 'continuous_raster')!;
    const unbounded = { ...raster.symbology, range: null } as Symbology;

    expect(() => deriveLegend(unbounded, { name: 'Top Wolfcamp' }, raster.palettes)).toThrow(
      /value range/,
    );
  });
});

describe('classLabel', () => {
  const breaks = [5, 10, 15, 20];

  it('leaves the end classes open, because the classification does', () => {
    // A feature above the top break has no upper bound. Writing the data
    // maximum there would claim a precision the classification does not have,
    // and would be wrong the moment a new well is added.
    expect(classLabel(breaks, 0, 5, '%')).toBe('< 5 %');
    expect(classLabel(breaks, 4, 5, '%')).toBe('≥ 20 %');
  });

  it('gives interior classes both bounds', () => {
    expect(classLabel(breaks, 1, 5, '%')).toBe('5 – 10 %');
    expect(classLabel(breaks, 3, 5, '%')).toBe('15 – 20 %');
  });

  it('omits the unit when the layer has none', () => {
    expect(classLabel(breaks, 1, 5)).toBe('5 – 10');
  });

  it('trims floating-point dust without inventing precision', () => {
    expect(classLabel([0.1 + 0.2], 0, 2)).toBe('< 0.3');
    expect(classLabel([12.34567], 0, 2)).toBe('< 12.34567');
  });
});

describe('prettyTicks', () => {
  it('puts ticks at round numbers a reader can locate on a map', () => {
    // 8,237–9,614 ft TVDSS. Even fifths would give 8,512.4.
    expect(prettyTicks(8237, 9614)).toEqual([8500, 9000, 9500]);
  });

  it('keeps every tick inside the range', () => {
    for (const [low, high] of [
      [0, 100],
      [4.1, 21.8],
      [-3.2, 3.2],
      [0.002, 0.031],
    ] as const) {
      const ticks = prettyTicks(low, high);
      expect(ticks.every((t) => t >= low && t <= high)).toBe(true);
    }
  });

  it('returns nothing for a degenerate range rather than dividing by zero', () => {
    expect(prettyTicks(5, 5)).toEqual([]);
    expect(prettyTicks(9, 3)).toEqual([]);
  });
});

function line(color: string) {
  return {
    geometry: 'line',
    color,
    width: 1.5,
    opacity: 1,
    cap: 'round',
    join: 'round',
  } as const;
}

describe('an unclassified graduated layer', () => {
  it('labels a class by its position rather than crashing', () => {
    // `Graduated.breaks` is present *after* classification runs, so an empty
    // array is the state a layer is in between choosing the mode and the
    // classifier returning. Reading `breaks[0]` there used to throw inside
    // `formatBreak`, which took the whole formatting dialog down.
    expect(classLabel([], 0, 5)).toBe('Class 1');
    expect(classLabel([], 2, 5)).toBe('Class 3');
    expect(classLabel([], 4, 5)).toBe('Class 5');
  });

  it('still labels a classified layer from its breaks', () => {
    expect(classLabel([10, 20, 30, 40], 0, 5)).toBe('< 10');
    expect(classLabel([10, 20, 30, 40], 2, 5, 'ft')).toBe('20 – 30 ft');
    expect(classLabel([10, 20, 30, 40], 4, 5)).toBe('≥ 40');
  });
});
