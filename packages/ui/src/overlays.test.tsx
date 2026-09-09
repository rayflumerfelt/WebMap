/**
 * Overlay components. `06-rendering.md` §9, `07-frontend.md` §11.
 *
 * Behaviour, not implementation (`CLAUDE.md` §6.1). What is asserted here is
 * what a reader of the finished map can tell: that a legend has one entry per
 * class, that a discrete palette draws bands rather than a gradient, that a
 * rotated map gets an arrow and a north-up one does not.
 */

import type { LegendSpec, Palette } from '@webmap/style-model';
import { deriveLegend } from '@webmap/style-model';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { Legend } from './Legend/Legend.js';
import { NorthArrow } from './NorthArrow/NorthArrow.js';
import { ScaleBar } from './ScaleBar/ScaleBar.js';

afterEach(cleanup);

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

const META = { name: 'Porosity', valueRange: { min: 4.1, max: 21.8, unit: '%' } };

// --- legend -----------------------------------------------------------------

describe('Legend', () => {
  it('draws one entry per class, from the same derivation the map uses', () => {
    // The §8 off-by-one, seen from the other end: five classes, four breaks.
    // A legend built by mapping over `breaks` shows four entries and is wrong
    // on a slide before anyone notices.
    const spec = deriveLegend(
      {
        type: 'graduated',
        field: 'porosity',
        method: 'pretty',
        classCount: 5,
        breaks: [5, 10, 15, 20],
        paletteId: 'viridis',
        vary: 'color',
        baseSymbol: {
          geometry: 'polygon',
          fillColor: '#cccccc',
          fillOpacity: 1,
          outlineColor: '#000000',
          outlineWidth: 0,
        },
      },
      META,
      { viridis: VIRIDIS },
    );

    render(<Legend spec={spec} />);

    expect(screen.getAllByRole('listitem')).toHaveLength(5);
    expect(screen.getByText('< 5 %')).toBeDefined();
    expect(screen.getByText('≥ 20 %')).toBeDefined();
  });

  it('shows the title with its unit', () => {
    const spec: LegendSpec = {
      kind: 'classes',
      title: 'Porosity (%)',
      entries: [{ swatch: '#440154', label: '< 5 %' }],
    };

    render(<Legend spec={spec} />);

    expect(screen.getByText('Porosity (%)')).toBeDefined();
  });

  it('renders a colour bar with ticks at the pretty values', () => {
    const spec = deriveLegend(
      { type: 'continuous_raster', paletteId: 'viridis', range: [8237, 9614], clamp: true, opacity: 1 },
      { name: 'Top Wolfcamp', valueRange: { min: 8237, max: 9614, unit: 'ft' } },
      { viridis: VIRIDIS },
    );

    render(<Legend spec={spec} />);

    // Round numbers a reader can locate on the map, not even fifths.
    expect(screen.getByText('8,500')).toBeDefined();
    expect(screen.getByText('9,000')).toBeDefined();
    expect(screen.getByText('9,500')).toBeDefined();
  });

  it('positions each tick over the colour it describes', () => {
    // Pretty ticks are unevenly spaced by design, so they are placed
    // proportionally rather than distributed. Distributing them would put the
    // label for 8,500 somewhere other than above 8,500.
    const spec = deriveLegend(
      { type: 'continuous_raster', paletteId: 'viridis', range: [0, 100], clamp: true, opacity: 1 },
      { name: 'Net to gross', valueRange: { min: 0, max: 100 } },
      { viridis: VIRIDIS },
    );

    const { container } = render(<Legend spec={spec} />);

    const ticks = [...container.querySelectorAll('span')].filter((s) => s.style.left);
    const twenty = ticks.find((s) => s.textContent === '20')!;
    expect(twenty.style.left).toBe('20%');
  });

  it('draws a discrete palette as bands, not a gradient', () => {
    // The browser would happily interpolate between the stops and draw a
    // continuous ramp for a classification that is not continuous — the same
    // lie the compiler avoids by emitting `step` rather than `interpolate`.
    const discrete: Palette = { ...VIRIDIS, interpolation: 'discrete' };
    const spec = deriveLegend(
      { type: 'continuous_raster', paletteId: 'viridis', range: [0, 1], clamp: true, opacity: 1 },
      { name: 'Facies', valueRange: { min: 0, max: 1 } },
      { viridis: discrete },
    );

    const { container } = render(<Legend spec={spec} />);

    const bar = container.querySelector('[role="img"]') as HTMLElement;
    // Each colour appears twice — the start and end of its band — which is
    // what makes the boundary a step rather than a blend.
    expect(bar.style.background.match(/#440154/g)).toHaveLength(2);
    expect(bar.style.background.match(/#21918c/g)).toHaveLength(2);
  });

  it('labels the colour bar for a screen reader with its range and unit', () => {
    const spec = deriveLegend(
      { type: 'continuous_raster', paletteId: 'viridis', range: [8237, 9614], clamp: true, opacity: 1 },
      { name: 'Top Wolfcamp', valueRange: { min: 8237, max: 9614, unit: 'ft' } },
      { viridis: VIRIDIS },
    );

    render(<Legend spec={spec} />);

    expect(screen.getByLabelText(/8237 ft to 9614 ft/)).toBeDefined();
  });
});

// --- scale bar --------------------------------------------------------------

describe('ScaleBar', () => {
  it('draws the same distance wider at latitude than at the equator', () => {
    // The property `scale.test.ts` asserts on the arithmetic, checked once
    // through the component so a refactor cannot quietly drop the latitude
    // argument on the floor.
    //
    // Asserted on the drawn width, not the label: the rounding to a nice
    // number often lands on the same distance at both latitudes, and the
    // width is where the 1/cos(φ) actually shows up. At 32°N a mile covers
    // more pixels, because each pixel covers less ground.
    const barWidth = (container: HTMLElement) =>
      Number.parseFloat((container.querySelector('[role="img"] > div') as HTMLElement).style.width);

    const { container: atEquator } = render(<ScaleBar latitude={0} zoom={12} />);
    const equatorWidth = barWidth(atEquator);
    cleanup();

    const { container: atMidland } = render(<ScaleBar latitude={31.99} zoom={12} />);

    expect(barWidth(atMidland)).toBeGreaterThan(equatorWidth);
    expect(barWidth(atMidland) / equatorWidth).toBeCloseTo(1 / Math.cos((31.99 * Math.PI) / 180), 2);
  });

  it('defaults to imperial, because the projects are in feet', () => {
    render(<ScaleBar latitude={31.99} zoom={16} />);

    expect(screen.getByLabelText(/Scale bar: .* ft/)).toBeDefined();
  });

  it('renders nothing rather than a zero-width bar', () => {
    const { container } = render(<ScaleBar latitude={31.99} zoom={12} maxWidth={0} />);

    expect(container.firstChild).toBeNull();
  });
});

// --- north arrow ------------------------------------------------------------

describe('NorthArrow', () => {
  it('is omitted on a north-up map', () => {
    // Decoration on a north-up map, and on a slide it takes space from data.
    const { container } = render(<NorthArrow bearing={0} />);

    expect(container.firstChild).toBeNull();
  });

  it('appears once the map is rotated', () => {
    // Not optional here: a reader who assumes north-up misreads every azimuth
    // on the page.
    render(<NorthArrow bearing={45} />);

    expect(screen.getByLabelText(/rotated 45 degrees/)).toBeDefined();
  });

  it('counter-rotates so the arrow keeps pointing at true north', () => {
    const { container } = render(<NorthArrow bearing={45} />);

    const svg = container.querySelector('svg') as SVGElement;
    expect(svg.style.transform).toBe('rotate(-45deg)');
  });

  it('treats a bearing just under 360 as north-up, not fully rotated', () => {
    const { container } = render(<NorthArrow bearing={359.9} />);

    expect(container.firstChild).toBeNull();
  });

  it('can be forced on for a printed map, where convention expects one', () => {
    render(<NorthArrow bearing={0} always />);

    expect(screen.getByLabelText(/north-up/)).toBeDefined();
  });
});
