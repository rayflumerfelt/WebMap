import { describe, expect, it } from 'vitest';

import { entryCount } from './symbology.js';
import type { Graduated, Palette, PointSymbol } from './symbology.js';

const dot: PointSymbol = {
  geometry: 'point',
  marker: 'circle',
  size: 5,
  color: '#2166ac',
  strokeColor: '#ffffff',
  strokeWidth: 1,
  opacity: 1,
};

export const viridis: Palette = {
  id: 'viridis',
  name: 'Viridis',
  isContinuous: true,
  stops: [
    { position: 0, color: '#440154' },
    { position: 1, color: '#fde725' },
  ],
  interpolation: 'linear',
};

describe('entryCount', () => {
  it('counts graduated classes from classCount, not from breaks', () => {
    // The off-by-one 08 §8 calls out: classify() returns n-1 interior breaks
    // for n classes, so counting breaks gives a legend one entry short of
    // the map.
    const graduated: Graduated = {
      type: 'graduated',
      field: 'porosity',
      method: 'pretty',
      classCount: 5,
      breaks: [5, 10, 15, 20],
      paletteId: viridis.id,
      vary: 'color',
      baseSymbol: dot,
    };

    expect(graduated.breaks).toHaveLength(4);
    expect(entryCount(graduated)).toBe(5);
  });

  it('counts the "other" bucket as a categorized entry when present', () => {
    const categories = [
      { value: 'normal', symbol: dot, label: 'Normal' },
      { value: 'reverse', symbol: dot, label: 'Reverse' },
    ];

    expect(entryCount({ type: 'categorized', field: 'kind', categories })).toBe(2);
    expect(
      entryCount({ type: 'categorized', field: 'kind', categories, other: dot }),
    ).toBe(3);
  });

  it('treats a continuous raster as one colour bar, not a swatch list', () => {
    expect(
      entryCount({
        type: 'continuous_raster',
        paletteId: viridis.id,
        range: null,
        clamp: true,
        opacity: 1,
      }),
    ).toBe(1);
  });
});
