/**
 * The formatting dialog. `07-frontend.md` §6.2.
 *
 * Behaviour, not markup. What is asserted is the set of claims the section
 * makes that a screenshot would not catch: that a section only appears where
 * the geometry supports it, that the reference zoom is revealed by the mode
 * that needs it, that the halo starts at none and says why that matters, and
 * that switching colour mode keeps the symbol rather than resetting the layer
 * to a default.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import type { LabelSymbol, Palette, PolygonSymbol, Symbology } from '@webmap/style-model';
import type { FontFamily } from '@webmap/ui';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { FormattingDialog } from './FormattingDialog.js';

afterEach(cleanup);

const PALETTES: Record<string, Palette> = {
  viridis: {
    id: 'viridis',
    name: 'Viridis',
    isContinuous: true,
    interpolation: 'linear',
    stops: [
      { position: 0, color: '#440154' },
      { position: 1, color: '#fde725' },
    ],
  },
  spectral: {
    id: 'spectral',
    name: 'Spectral',
    isContinuous: true,
    interpolation: 'linear',
    stops: [
      { position: 0, color: '#3288bd' },
      { position: 1, color: '#d53e4f' },
    ],
  },
};

const FONTS: FontFamily[] = [
  { name: 'Inter', stacks: ['Inter Regular', 'Inter Bold', 'Inter Italic'] },
  { name: 'Oswald', stacks: ['Oswald Regular', 'Oswald Bold'] },
];

const FIELDS = [
  { name: 'formation', type: 'text' as const },
  { name: 'porosity', type: 'number' as const },
];

const POLYGON_SYMBOL: PolygonSymbol = {
  geometry: 'polygon',
  fillColor: '#88aacc',
  fillOpacity: 0.6,
  outlineColor: '#333333',
  outlineWidth: 1,
};

const POLYGON: Symbology = { type: 'single', symbol: POLYGON_SYMBOL };

const LINE: Symbology = {
  type: 'single',
  symbol: {
    geometry: 'line',
    color: '#aa3333',
    width: 2,
    opacity: 1,
    cap: 'butt',
    join: 'miter',
  },
};

const LABEL_SYMBOL: LabelSymbol = {
  geometry: 'label',
  field: 'formation',
  size: 12,
  sizeMode: { mode: 'fixed' },
  color: '#222222',
  haloColor: '#ffffff',
  haloWidth: 0,
  font: ['Inter Regular'],
  placement: 'point',
  allowOverlap: true,
};

function renderDialog(symbology: Symbology, overrides: Record<string, unknown> = {}) {
  const onChange = vi.fn();
  render(
    <FormattingDialog
      layerName="Leases"
      symbology={symbology}
      onChange={onChange}
      fields={FIELDS}
      palettes={PALETTES}
      fonts={FONTS}
      {...overrides}
    />,
  );
  return onChange;
}

describe('geometry-specific sections', () => {
  it('offers a fill for a polygon and not for a line', () => {
    renderDialog(POLYGON);
    expect(screen.getByRole('heading', { name: 'Fill' })).toBeTruthy();

    cleanup();
    renderDialog(LINE);
    // The `SymbolSpec` union is what makes this true — a line symbol has no
    // `fillColor`, so the fill section is unreachable rather than hidden.
    expect(screen.queryByRole('heading', { name: 'Fill' })).toBeNull();
    expect(screen.getByRole('heading', { name: 'Line' })).toBeTruthy();
  });

  it('offers the marker section only for a point', () => {
    renderDialog(POLYGON);
    expect(screen.queryByRole('heading', { name: 'Marker' })).toBeNull();
  });
});

describe('label formatting', () => {
  const labelled: Symbology = { type: 'single', symbol: LABEL_SYMBOL };

  it('hides the reference zoom in fixed mode and reveals it in the other', () => {
    // A reference zoom is meaningless in fixed mode, and a control that is
    // always visible but only sometimes effective is how people conclude a
    // feature does not work.
    renderDialog(labelled);
    expect(screen.queryByLabelText('At zoom')).toBeNull();

    cleanup();
    renderDialog({
      type: 'single',
      symbol: { ...LABEL_SYMBOL, sizeMode: { mode: 'scale-with-map', referenceZoom: 14 } },
    });
    expect((screen.getByLabelText('At zoom') as HTMLInputElement).value).toBe('14');
  });

  it('carries the reference zoom across a mode switch', () => {
    const onChange = renderDialog({
      type: 'single',
      symbol: { ...LABEL_SYMBOL, sizeMode: { mode: 'scale-with-map', referenceZoom: 9 } },
    });

    fireEvent.change(screen.getByLabelText('Behaviour'), { target: { value: 'fixed' } });
    fireEvent.change(screen.getByLabelText('Behaviour'), {
      target: { value: 'scale-with-map' },
    });

    // The first call is the switch to fixed; asserting the *second* is the
    // point — toggling to look at the preview and back must not reset the zoom.
    const last = onChange.mock.calls.at(-1)?.[0] as Symbology;
    expect(last).toMatchObject({
      symbol: { sizeMode: { mode: 'scale-with-map', referenceZoom: 9 } },
    });
  });

  it('starts with no halo and says when that will cost legibility', () => {
    renderDialog(labelled);
    expect((screen.getByLabelText('Width') as HTMLInputElement).value).toBe('0');
    expect(screen.getByText(/usually what makes the text readable/)).toBeTruthy();
  });

  it('explains that the zoom window is the only thinning control', () => {
    renderDialog(labelled);
    expect(screen.getByText(/collision detection is off/)).toBeTruthy();
  });

  it('clears a zoom limit to no limit rather than to zero', () => {
    // Zero is a real zoom. A cleared field has to mean "no limit", and the
    // compiler reads that as an absent `minZoom`.
    const onChange = renderDialog({
      type: 'single',
      symbol: { ...LABEL_SYMBOL, minZoom: 8 },
    });
    fireEvent.change(screen.getByLabelText('Show from'), { target: { value: '' } });

    const next = onChange.mock.calls[0]?.[0] as { symbol: LabelSymbol };
    expect('minZoom' in next.symbol).toBe(false);
  });
});

describe('colour mode', () => {
  it('keeps the symbol when switching from fixed to categories', () => {
    // Switching mode is a change of how the colour is decided, not a reset of
    // the layer — the outline width and marker shape someone set survive it.
    const onChange = renderDialog(POLYGON);
    fireEvent.change(screen.getByLabelText('Colour by'), { target: { value: 'categories' } });

    const next = onChange.mock.calls[0]?.[0] as Symbology;
    expect(next.type).toBe('categorized');
    expect(next).toMatchObject({ other: { geometry: 'polygon', outlineWidth: 1 } });
  });

  it('disables the modes the layer has no column for', () => {
    render(
      <FormattingDialog
        layerName="Leases"
        symbology={POLYGON}
        onChange={vi.fn()}
        fields={[{ name: 'formation', type: 'text' }]}
        palettes={PALETTES}
        fonts={FONTS}
      />,
    );
    const options = screen.getAllByRole('option') as HTMLOptionElement[];
    const gradient = options.find((option) => option.value === 'gradient');
    expect(gradient?.disabled).toBe(true);
  });

  it('warns about a perceptually poor ramp instead of removing it', () => {
    // `08` §5.1: geologists expect a spectral ramp on a structure map, so the
    // option stays and the caveat is a tooltip rather than a refusal.
    renderDialog(
      {
        type: 'graduated',
        field: 'porosity',
        method: 'quantile',
        classCount: 5,
        breaks: [],
        paletteId: 'spectral',
        vary: 'color',
        baseSymbol: POLYGON_SYMBOL,
      },
      { summary: { domain: [6, 14] as [number, number], histogram: [1, 4, 9, 2] } },
    );
    expect(screen.getByText(/not perceptually uniform/)).toBeTruthy();
  });

  it('says what Other does not catch', () => {
    renderDialog(POLYGON, { nullColor: '#cccccc', onNullColorChange: vi.fn() });
    expect(screen.getByText(/a well with no porosity reading is not zero/)).toBeTruthy();
  });
});
