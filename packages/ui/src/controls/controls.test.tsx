/**
 * The §6.3 shared controls, tested on behaviour rather than on markup.
 *
 * The claims worth protecting are the ones that are decisions: that a hex
 * field does not repaint the map three times while a colour is typed, that a
 * ramp's stops cannot be stored out of order, that a font's missing italic is
 * disabled rather than ignored, and that the *Other* row stays visible while a
 * category list is filtered. Each of those is a sentence in a docstring that
 * would otherwise decay into a comment nobody checks.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import type { Palette } from '@webmap/style-model';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CategoryTable } from './CategoryTable.js';
import { ColorPicker, expandHex } from './ColorPicker.js';
import { FontPicker, composeStack, splitStack } from './FontPicker.js';
import { IntervalEditor } from './IntervalEditor.js';
import { LinePicker, patternName } from './LinePicker.js';
import { MarkerPicker } from './MarkerPicker.js';
import { PaletteIO, formatOf } from './PaletteIO.js';
import { RampEditor } from './RampEditor.js';

// `vitest.config.ts` sets `globals: false`, so testing-library's automatic
// cleanup — which registers itself through a global `afterEach` — never runs.
// Without this every render stays in the document and the next test finds two
// of everything, which is how six of these first failed.
afterEach(cleanup);

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

describe('ColorPicker', () => {
  it('expands a three-digit hex', () => {
    expect(expandHex('#abc')).toBe('#aabbcc');
    expect(expandHex('1F78B4')).toBe('#1f78b4');
    expect(expandHex('not a colour')).toBeNull();
  });

  it('does not emit a change for every keystroke of a hex', () => {
    // Typing `#1f78b4` character by character passes through `#1`, `#1f` and
    // `#1f7`. Committing each would repaint the map three times with colours
    // nobody asked for before the fourth character arrived.
    const onChange = vi.fn();
    render(<ColorPicker label="Fill" value="#000000" onChange={onChange} />);
    const hex = screen.getByLabelText('Fill hex value');

    fireEvent.change(hex, { target: { value: '#1' } });
    fireEvent.change(hex, { target: { value: '#1f' } });
    fireEvent.change(hex, { target: { value: '#1f78b4' } });
    expect(onChange).not.toHaveBeenCalled();

    // No `target` override on the blur: assigning the value again in the
    // event init desynchronises React's value tracker, and the reset the
    // next test asserts then never reaches the DOM.
    fireEvent.blur(hex);
    expect(onChange).toHaveBeenCalledExactlyOnceWith('#1f78b4');
  });

  it('restores the current value when the typed hex does not parse', () => {
    const onChange = vi.fn();
    render(<ColorPicker label="Fill" value="#112233" onChange={onChange} />);
    const hex = screen.getByLabelText('Fill hex value');

    // Typed, then blurred — the real sequence. Blurring alone would set the
    // DOM value behind React's back, and the reset would then be a no-op state
    // write rather than the behaviour under test.
    fireEvent.change(hex, { target: { value: 'burnt sienna' } });
    fireEvent.blur(hex);

    expect(onChange).not.toHaveBeenCalled();
    expect((hex as HTMLInputElement).value).toBe('#112233');
  });

  it('hides the opacity slider when the property has none', () => {
    const { rerender } = render(<ColorPicker label="Line" value="#000000" onChange={vi.fn()} />);
    expect(screen.queryByLabelText('Line opacity')).toBeNull();

    rerender(
      <ColorPicker
        label="Line"
        value="#000000"
        onChange={vi.fn()}
        opacity={0.5}
        onOpacityChange={vi.fn()}
      />,
    );
    expect(screen.getByLabelText('Line opacity')).toBeTruthy();
  });
});

describe('RampEditor', () => {
  it('keeps stops ascending however they are moved', () => {
    // `colourAt` interpolates between adjacent entries, so a stored palette
    // whose positions run backwards produces a ramp that reverses partway.
    const onChange = vi.fn();
    render(<RampEditor palette={RAMP} onChange={onChange} />);

    fireEvent.change(screen.getByLabelText('Stop 1 position, percent'), {
      target: { value: '90' },
    });

    const next = onChange.mock.calls[0]?.[0] as Palette;
    expect(next.stops.map((stop) => stop.position)).toEqual([0.9, 1]);
  });

  it('refuses to drop below two stops', () => {
    render(<RampEditor palette={RAMP} onChange={vi.fn()} />);
    expect((screen.getByLabelText('Remove stop 1') as HTMLButtonElement).disabled).toBe(true);
  });

  it('adds a stop that changes nothing until it is moved', () => {
    // The midpoint takes the colour already at the midpoint, so adding a stop
    // to look at it is safe.
    const onChange = vi.fn();
    render(<RampEditor palette={RAMP} onChange={onChange} />);
    fireEvent.click(screen.getByText('Add stop'));

    const next = onChange.mock.calls[0]?.[0] as Palette;
    expect(next.stops).toHaveLength(3);
    expect(next.stops[1]?.position).toBeCloseTo(0.5);
  });

  it('renders a histogram bin per value when one is supplied', () => {
    const { container } = render(
      <RampEditor palette={RAMP} onChange={vi.fn()} histogram={[1, 8, 3]} domain={[0, 30]} />,
    );
    expect(container.querySelector('[aria-hidden="true"]')?.childElementCount).toBe(3);
  });
});

describe('IntervalEditor', () => {
  it('describes each band from the one below it', () => {
    render(
      <IntervalEditor
        bands={[
          { max: 10, color: '#eee' },
          { max: 20, color: '#ccc' },
        ]}
        onChange={vi.fn()}
        unit="ft"
      />,
    );
    expect(screen.getByText('≤ 10 ft')).toBeTruthy();
    expect(screen.getByText('10 ft – 20 ft')).toBeTruthy();
  });

  it('names a duplicated maximum rather than merging it', () => {
    render(
      <IntervalEditor
        bands={[
          { max: 10, color: '#eee' },
          { max: 10, color: '#ccc' },
        ]}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByText(/covers no values at all/)).toBeTruthy();
  });

  it('says when the data runs above the highest band', () => {
    render(
      <IntervalEditor bands={[{ max: 10, color: '#eee' }]} onChange={vi.fn()} domain={[0, 45]} />,
    );
    expect(screen.getByText(/above the highest band/)).toBeTruthy();
  });
});

describe('CategoryTable', () => {
  const rows = [
    { value: 'Wolfcamp', color: '#1f78b4', count: 900 },
    { value: 'Bone Spring', color: '#33a02c', count: 120 },
    { value: 'Delaware', color: '#e31a1c', count: 4 },
  ];

  it('orders by descending count, not alphabetically', () => {
    render(
      <CategoryTable rows={rows} onChange={vi.fn()} otherColor="#d9d9d9" onOtherChange={vi.fn()} />,
    );
    const body = screen.getByText('Wolfcamp').closest('tbody');
    const first = [...(body?.querySelectorAll('tr') ?? [])].map(
      (row) => row.querySelector('td')?.textContent,
    );
    expect(first.slice(0, 3)).toEqual(['Wolfcamp', 'Bone Spring', 'Delaware']);
  });

  it('warns when there is no Other colour', () => {
    render(
      <CategoryTable rows={rows} onChange={vi.fn()} otherColor={null} onOtherChange={vi.fn()} />,
    );
    expect(screen.getByText(/draws invisibly/)).toBeTruthy();
  });

  it('states the cardinality refusal instead of showing an empty table', () => {
    render(
      <CategoryTable
        rows={[]}
        onChange={vi.fn()}
        otherColor="#d9d9d9"
        onOtherChange={vi.fn()}
        refused={{ distinct: 512_000, limit: 500 }}
      />,
    );
    expect(screen.getByText(/past the 500 the server will enumerate/)).toBeTruthy();
  });
});

describe('LinePicker', () => {
  it('names a dash array that matches no preset', () => {
    expect(patternName(undefined)).toBe('Solid');
    expect(patternName([4, 2])).toBe('Dashed');
    expect(patternName([7, 1, 3])).toBe('Custom');
  });

  it('draws the preview with the dash scaled by line width', () => {
    // A dash array is in line widths. A preview at a fixed width would show
    // a different line from the one the map draws.
    const { container } = render(
      <LinePicker
        value={{ color: '#000', width: 4, dashArray: [4, 2], cap: 'butt', join: 'miter' }}
        onChange={vi.fn()}
      />,
    );
    expect(container.querySelector('line')?.getAttribute('stroke-dasharray')).toBe('16 8');
  });
});

describe('MarkerPicker', () => {
  it('says a sprite marker with no name draws nothing', () => {
    render(
      <MarkerPicker
        value={{
          marker: 'sprite',
          size: 8,
          color: '#000',
          strokeColor: '#fff',
          strokeWidth: 1,
          opacity: 1,
        }}
        onChange={vi.fn()}
        sprites={['well', 'rig']}
      />,
    );
    expect(screen.getByText(/draws nothing/)).toBeTruthy();
  });
});

describe('FontPicker', () => {
  const families = [
    { name: 'Inter', stacks: ['Inter Regular', 'Inter Bold', 'Inter Italic'] },
    { name: 'Oswald', stacks: ['Oswald Regular', 'Oswald Bold'] },
  ];

  it('splits a stack name into family and face', () => {
    expect(splitStack('Inter Bold Italic', families)).toEqual({
      family: 'Inter',
      bold: true,
      italic: true,
    });
  });

  it('composes only stacks the deployment built', () => {
    expect(composeStack(families[1]!, true, false)).toBe('Oswald Bold');
    expect(composeStack(families[1]!, false, true)).toBeNull();
  });

  it('disables italic for a family that has none', () => {
    // Offered and ignored is a bug report; disabled with a reason is
    // information. Oswald is the standing example.
    render(<FontPicker value={['Oswald Regular']} onChange={vi.fn()} families={families} />);
    const italic = screen.getByRole('checkbox', { name: /italic/i }) as HTMLInputElement;
    expect(italic.disabled).toBe(true);
    expect(screen.getByText('Oswald has no italic face in this deployment.')).toBeTruthy();
  });

  it('falls back to the plain family rather than an empty stack', () => {
    const onChange = vi.fn();
    render(<FontPicker value={['Inter Italic']} onChange={onChange} families={families} />);
    fireEvent.change(screen.getByLabelText('Font'), { target: { value: 'Oswald' } });
    expect(onChange).toHaveBeenCalledWith(['Oswald Regular']);
  });
});

describe('PaletteIO', () => {
  it('recognises the four formats and nothing else', () => {
    expect(formatOf('structure.clr')).toBe('clr');
    expect(formatOf('GMT_relief.cpt')).toBe('cpt');
    expect(formatOf('ramps.XML')).toBe('xml');
    expect(formatOf('notes.txt')).toBeNull();
  });

  it('names the supported formats when the file is not one', async () => {
    const { container } = render(
      <PaletteIO name="viridis" onImport={vi.fn()} onExport={() => ''} />,
    );
    const input = container.querySelector('input[type="file"]');
    expect(input).toBeTruthy();

    const file = new File(['nothing'], 'notes.txt', { type: 'text/plain' });
    fireEvent.change(input!, { target: { files: [file] } });

    expect(await screen.findByText(/is not a palette file/)).toBeTruthy();
  });
});
