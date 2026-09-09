/**
 * Symbology editor. `08-styling-palettes.md` §2.
 *
 * The property worth protecting is that this panel edits the *model*: what it
 * emits must be something `compileSymbology` accepts and `deriveLegend`
 * describes. So the tests compile and derive from its output rather than
 * inspecting the object shape, which would pass while producing a symbology
 * the map cannot draw.
 */

import type { Symbology } from '@webmap/style-model';
import { compileSymbology, deriveLegend } from '@webmap/style-model';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { SymbologyPanel } from './SymbologyPanel.js';

afterEach(cleanup);

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

const POINT: Symbology = {
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

const META = { name: 'Wolfcamp A', valueRange: { min: 0, max: 1 } };

function renderPanel(symbology: Symbology = POLYGON) {
  const onChange = vi.fn();
  render(
    <SymbologyPanel layerName="Wolfcamp A" symbology={symbology} onChange={onChange} />,
  );
  return { onChange };
}

describe('empty state', () => {
  it('asks for a layer rather than showing an empty form', () => {
    render(<SymbologyPanel layerName={null} symbology={null} onChange={vi.fn()} />);

    expect(screen.getByText(/Select a layer/)).toBeDefined();
  });
});

describe('geometry-aware fields', () => {
  it('offers fill and outline for a polygon', () => {
    renderPanel(POLYGON);

    expect(screen.getByLabelText('Fill')).toBeDefined();
    expect(screen.getByLabelText('Outline width')).toBeDefined();
  });

  it('offers no fill for a point, because points have no fill', () => {
    // The geometry-aware union doing real work: the type system prevents this
    // editor from offering a property the symbol does not have, which is more
    // reliable than a runtime check.
    renderPanel(POINT);

    expect(screen.queryByLabelText('Fill opacity')).toBeNull();
    expect(screen.getByLabelText('Size')).toBeDefined();
  });
});

describe('editing', () => {
  it('emits a symbology the compiler accepts', () => {
    // Not "emits an object with fillColor set" — that passes while producing
    // something the map cannot draw.
    const { onChange } = renderPanel(POLYGON);

    fireEvent.change(screen.getByLabelText('Fill'), { target: { value: '#ff0000' } });

    const emitted = onChange.mock.calls[0]![0] as Symbology;
    const compiled = compileSymbology(emitted, { sourceId: 's', palettes: {} });
    expect(compiled.find((l) => l.type === 'fill')!.paint!['fill-color']).toBe('#ff0000');
  });

  it('emits a symbology the legend can describe', () => {
    const { onChange } = renderPanel(POLYGON);

    fireEvent.change(screen.getByLabelText('Fill'), { target: { value: '#00ff00' } });

    const emitted = onChange.mock.calls[0]![0] as Symbology;
    const legend = deriveLegend(emitted, META, {});
    expect(legend.kind).toBe('classes');
  });

  it('shows the hex alongside the swatch', () => {
    // §10: colour is never the only channel, and a geologist matching a
    // partner's map needs the value rather than the appearance.
    renderPanel(POLYGON);

    expect(screen.getByText('#88a0c8')).toBeDefined();
  });

  it('ignores a mid-typing empty number rather than setting width to zero', () => {
    // An empty field parses as 0, which would silently set a line width to
    // nothing while the user was still typing.
    const { onChange } = renderPanel(POLYGON);

    fireEvent.change(screen.getByLabelText('Outline width'), { target: { value: '' } });

    expect(onChange).not.toHaveBeenCalled();
  });

  it('ignores a number outside the allowed range', () => {
    const { onChange } = renderPanel(POLYGON);

    fireEvent.change(screen.getByLabelText('Fill opacity'), { target: { value: '5' } });

    expect(onChange).not.toHaveBeenCalled();
  });
});

describe('changing symbology type', () => {
  it('keeps the existing symbol rather than resetting to a default', () => {
    // A geologist changing type has already chosen a colour and a size.
    // Discarding them makes the switch feel like losing work.
    const { onChange } = renderPanel(POLYGON);

    fireEvent.change(screen.getByLabelText('Symbology type'), {
      target: { value: 'categorized' },
    });

    const emitted = onChange.mock.calls[0]![0] as Symbology;
    expect(emitted.type).toBe('categorized');
    if (emitted.type !== 'categorized') return;
    expect(emitted.other).toEqual(POLYGON.type === 'single' ? POLYGON.symbol : undefined);
  });

  it('produces something drawable the instant the type changes', () => {
    // A categorized symbology with no categories yet still has to compile, or
    // the map goes blank between choosing the type and choosing the field.
    const { onChange } = renderPanel(POLYGON);
    fireEvent.change(screen.getByLabelText('Symbology type'), {
      target: { value: 'categorized' },
    });

    const emitted = onChange.mock.calls[0]![0] as Symbology;

    expect(() => compileSymbology(emitted, { sourceId: 's', palettes: {} })).not.toThrow();
  });

  it('converts back to a single symbol using the first category', () => {
    const categorized: Symbology = {
      type: 'categorized',
      field: 'fault_type',
      categories: [
        {
          value: 'normal',
          label: 'Normal',
          symbol: {
            geometry: 'line',
            color: '#e41a1c',
            width: 1.5,
            opacity: 1,
            cap: 'round',
            join: 'round',
          },
        },
      ],
    };
    const { onChange } = renderPanel(categorized);

    fireEvent.change(screen.getByLabelText('Symbology type'), { target: { value: 'single' } });

    const emitted = onChange.mock.calls[0]![0] as Symbology;
    expect(emitted.type).toBe('single');
    if (emitted.type !== 'single') return;
    expect(emitted.symbol).toEqual(categorized.categories[0]!.symbol);
  });
});

describe('categories', () => {
  const categorized: Symbology = {
    type: 'categorized',
    field: 'fault_type',
    categories: [
      {
        value: 'normal',
        label: 'Normal',
        symbol: {
          geometry: 'line',
          color: '#e41a1c',
          width: 1.5,
          opacity: 1,
          cap: 'round',
          join: 'round',
        },
      },
      {
        value: 'reverse',
        label: 'Reverse',
        symbol: {
          geometry: 'line',
          color: '#377eb8',
          width: 1.5,
          opacity: 1,
          cap: 'round',
          join: 'round',
        },
      },
    ],
  };

  it('lists every category with its label and colour', () => {
    renderPanel(categorized);

    expect(screen.getByLabelText('Label for normal')).toBeDefined();
    expect(screen.getByLabelText('Colour for reverse')).toBeDefined();
  });

  it('recolours one category without disturbing the others', () => {
    const { onChange } = renderPanel(categorized);

    fireEvent.change(screen.getByLabelText('Colour for normal'), {
      target: { value: '#00ff00' },
    });

    const emitted = onChange.mock.calls[0]![0] as Symbology;
    if (emitted.type !== 'categorized') throw new Error('expected categorized');
    expect(emitted.categories[1]).toEqual(categorized.categories[1]);
    const compiled = compileSymbology(emitted, { sourceId: 's', palettes: {} });
    expect(JSON.stringify(compiled)).toContain('#00ff00');
  });

  it('reports the legend entry count from the shared function', () => {
    // Read from `entryCount`, the same function the legend and the compiler
    // use — §8's single source of truth for how many entries exist.
    renderPanel(categorized);

    expect(screen.getByText(/2 legend entries/)).toBeDefined();
  });

  it('says "entry" for one and "entries" for several', () => {
    renderPanel(POLYGON);

    expect(screen.getByText(/1 legend entry$/)).toBeDefined();
  });
});
