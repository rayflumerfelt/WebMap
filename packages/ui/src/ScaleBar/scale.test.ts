/**
 * Scale bar arithmetic. `06-rendering.md` §9.
 *
 * The test that matters is `varies with latitude`. Everything else here is
 * rounding and formatting; that one is the difference between a measuring
 * instrument and a decoration.
 */

import { describe, expect, it } from 'vitest';

import { metersPerPixel, scaleBar } from './scale.js';

const MIDLAND_LAT = 31.99;

describe('metersPerPixel', () => {
  it('matches the known Web Mercator resolution at the equator', () => {
    // 40075016.686 / 2^(0+9) = 78271.5169... m/px at zoom 0. A standard
    // number, checked against the specification rather than against this
    // implementation's own output.
    expect(metersPerPixel(0, 0)).toBeCloseTo(78271.5169, 3);
    expect(metersPerPixel(0, 10)).toBeCloseTo(76.437, 3);
  });

  it('shrinks by cos(latitude)', () => {
    // **The property the whole file exists for.** A bar computed from zoom
    // alone would return the equatorial value everywhere.
    const equator = metersPerPixel(0, 12);
    const midland = metersPerPixel(MIDLAND_LAT, 12);

    expect(midland / equator).toBeCloseTo(Math.cos((MIDLAND_LAT * Math.PI) / 180), 9);
    expect(midland).toBeLessThan(equator);
  });

  it('is symmetric about the equator', () => {
    expect(metersPerPixel(45, 10)).toBeCloseTo(metersPerPixel(-45, 10), 9);
  });

  it('clamps beyond the Mercator limit rather than returning nonsense', () => {
    // Web Mercator is undefined past ±85.051129°. A map dragged to the pole
    // should give a usable bar, not a negative one.
    expect(metersPerPixel(89, 10)).toBe(metersPerPixel(85.051129, 10));
    expect(metersPerPixel(89, 10)).toBeGreaterThan(0);
  });
});

describe('scaleBar', () => {
  it('varies with latitude at a fixed zoom', () => {
    // **The bug this guards.** At 32°N a zoom-derived bar overstates distance
    // by 1/cos(32°) ≈ 18% — on a map whose purpose is measuring things, with
    // nothing on screen to suggest anything is wrong.
    const equator = scaleBar(0, 12, 120, 'metric');
    const midland = scaleBar(MIDLAND_LAT, 12, 120, 'metric');

    expect(midland.distance).not.toBe(equator.distance);
  });

  it('never exceeds the width it was given', () => {
    // A bar wider than its box is clipped by the map frame, and a clipped bar
    // lies about the distance it spans.
    for (let zoom = 1; zoom <= 20; zoom += 1) {
      for (const unit of ['metric', 'imperial'] as const) {
        const bar = scaleBar(MIDLAND_LAT, zoom, 120, unit);
        expect(bar.widthPx).toBeLessThanOrEqual(120);
        expect(bar.widthPx).toBeGreaterThan(0);
      }
    }
  });

  it('uses a good share of the space it has', () => {
    // A bar occupying a quarter of its allowance is harder to read off and
    // looks like a rendering fault. The {1,2,3,5} family guarantees at least
    // half of the previous step, so a third of the allowance is the floor.
    for (let zoom = 1; zoom <= 20; zoom += 1) {
      const bar = scaleBar(MIDLAND_LAT, zoom, 120, 'metric');
      expect(bar.widthPx).toBeGreaterThan(120 / 3);
    }
  });

  it('labels a round number, never a computed one', () => {
    for (let zoom = 4; zoom <= 18; zoom += 1) {
      const bar = scaleBar(MIDLAND_LAT, zoom, 120, 'metric');
      const mantissa = bar.distance / 10 ** Math.floor(Math.log10(bar.distance));
      expect([1, 2, 3, 5]).toContain(Math.round(mantissa));
    }
  });

  it('switches from metres to kilometres at 1000 m', () => {
    const wide = scaleBar(MIDLAND_LAT, 8, 120, 'metric');
    const close = scaleBar(MIDLAND_LAT, 18, 120, 'metric');

    expect(wide.unit).toBe('km');
    expect(close.unit).toBe('m');
  });

  it('stays in feet until a whole mile fits', () => {
    // A Permian well spacing is quoted in feet, and "0.19 mi" helps nobody.
    const close = scaleBar(MIDLAND_LAT, 16, 120, 'imperial');

    expect(close.unit).toBe('ft');
    expect(close.label).toMatch(/ ft$/);
  });

  it('uses miles once the map is wide enough', () => {
    const wide = scaleBar(MIDLAND_LAT, 9, 120, 'imperial');

    expect(wide.unit).toBe('mi');
  });

  it('separates thousands so the number is read rather than counted', () => {
    const bar = scaleBar(MIDLAND_LAT, 13, 200, 'imperial');

    if (bar.distance >= 1000) expect(bar.label).toContain(',');
  });

  it('returns an empty bar for a zero-width container rather than throwing', () => {
    // A decoration must not be able to take the map down with it.
    expect(scaleBar(MIDLAND_LAT, 12, 0).widthPx).toBe(0);
    expect(scaleBar(MIDLAND_LAT, 12, 0).label).toBe('');
  });

  it('agrees with a hand-computed distance', () => {
    // Zoom 12 at 32°N: 40075016.686 * cos(32°) / 2^21 = 16.206 m/px.
    // 120 px therefore spans 1,944 m, and the largest {1,2,3,5}×10^n at or
    // below that is 1,000 m — so a 1 km bar, 61.7 px wide.
    const bar = scaleBar(32, 12, 120, 'metric');

    expect(bar.label).toBe('1 km');
    expect(bar.widthPx).toBeCloseTo(1000 / 16.2064, 1);
  });
});
