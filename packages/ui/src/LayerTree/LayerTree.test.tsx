/**
 * Layer tree. `07-frontend.md` §6, §10.
 *
 * The obligations density creates are what is asserted here: a hover-revealed
 * action that a keyboard user can also reach, a context menu that also opens
 * on `Shift+F10`, a reorder that works without a mouse. Each is a rule the
 * spec states and none of them show up in a screenshot.
 */

import type { Palette, Symbology } from '@webmap/style-model';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { LayerTree } from './LayerTree.js';
import type { LayerTreeProps, SessionLayerView } from './LayerTree.js';

afterEach(cleanup);

const VIRIDIS: Palette = {
  id: 'viridis',
  name: 'Viridis',
  isContinuous: true,
  interpolation: 'linear',
  stops: [
    { position: 0, color: '#440154' },
    { position: 1, color: '#fde725' },
  ],
};

const FAULT_LINE: Symbology = {
  type: 'single',
  symbol: {
    geometry: 'line',
    color: '#e41a1c',
    width: 1.5,
    opacity: 1,
    cap: 'round',
    join: 'round',
  },
};

function layer(id: string, name: string, overrides: Partial<SessionLayerView> = {}): SessionLayerView {
  return {
    id,
    datasetId: `ds-${id}`,
    name,
    symbology: FAULT_LINE,
    opacity: 1,
    visible: true,
    ...overrides,
  };
}

function renderTree(overrides: Partial<LayerTreeProps> = {}) {
  const props: LayerTreeProps = {
    layers: [layer('a', 'Wolfcamp A Control Points'), layer('b', 'Midland Basin Faults')],
    palettes: { viridis: VIRIDIS },
    selectedId: null,
    onSelect: vi.fn(),
    onReorder: vi.fn(),
    onToggleVisibility: vi.fn(),
    onOpacityChange: vi.fn(),
    onRemove: vi.fn(),
    ...overrides,
  };
  return { props, ...render(<LayerTree {...props} />) };
}

// --- density and selection --------------------------------------------------

describe('rows', () => {
  it('lists every layer in order with its position announced', () => {
    renderTree();

    const rows = screen.getAllByRole('option');
    expect(rows).toHaveLength(2);
    expect(rows[0]!.getAttribute('aria-label')).toContain('layer 1 of 2');
  });

  it('selects on click', () => {
    const { props } = renderTree();

    fireEvent.click(screen.getByText('Midland Basin Faults'));

    expect(props.onSelect).toHaveBeenCalledWith('b');
  });

  it('selects from the keyboard, not only the mouse', () => {
    const { props } = renderTree();

    fireEvent.keyDown(screen.getAllByRole('option')[1]!, { key: 'Enter' });

    expect(props.onSelect).toHaveBeenCalledWith('b');
  });

  it('renders each row at the density token, not an ad-hoc height', () => {
    // §5.3: 26 px rows, so twenty layers fit without scrolling. Using the
    // token rather than a literal keeps density consistent across panels.
    const { container } = render(
      <LayerTree
        layers={[layer('a', 'A')]}
        palettes={{}}
        selectedId={null}
        onSelect={vi.fn()}
        onReorder={vi.fn()}
        onToggleVisibility={vi.fn()}
        onOpacityChange={vi.fn()}
        onRemove={vi.fn()}
      />,
    );

    const row = container.querySelector('[role="option"]') as HTMLElement;
    expect(row.style.height).toContain('--row-h');
  });
});

// --- visibility -------------------------------------------------------------

describe('visibility', () => {
  it('toggles without selecting the row', () => {
    // Clicking the eye is not clicking the layer. Conflating them means every
    // visibility toggle also changes what the symbology panel is editing.
    const { props } = renderTree();

    fireEvent.click(screen.getByLabelText('Hide Wolfcamp A Control Points'));

    expect(props.onToggleVisibility).toHaveBeenCalledWith('a');
    expect(props.onSelect).not.toHaveBeenCalled();
  });

  it('reports hidden state to a screen reader, not only by dimming', () => {
    // §10: colour is never the only channel.
    renderTree({ layers: [layer('a', 'Faults', { visible: false })] });

    const toggle = screen.getByRole('switch');
    expect(toggle.getAttribute('aria-checked')).toBe('false');
    expect(toggle.getAttribute('aria-label')).toBe('Show Faults');
  });
});

// --- reordering -------------------------------------------------------------

describe('reordering', () => {
  it('moves a layer with Alt+ArrowUp', () => {
    // Drag-and-drop is the fast path, not the only path. A keyboard user who
    // cannot reorder layers cannot control what paints over what.
    const { props } = renderTree();

    fireEvent.keyDown(screen.getAllByRole('option')[1]!, { key: 'ArrowUp', altKey: true });

    expect(props.onReorder).toHaveBeenCalledWith(1, 0);
  });

  it('moves a layer with Alt+ArrowDown', () => {
    const { props } = renderTree();

    fireEvent.keyDown(screen.getAllByRole('option')[0]!, { key: 'ArrowDown', altKey: true });

    expect(props.onReorder).toHaveBeenCalledWith(0, 1);
  });

  it('does not move the top layer up or the bottom layer down', () => {
    const { props } = renderTree();

    fireEvent.keyDown(screen.getAllByRole('option')[0]!, { key: 'ArrowUp', altKey: true });
    fireEvent.keyDown(screen.getAllByRole('option')[1]!, { key: 'ArrowDown', altKey: true });

    expect(props.onReorder).not.toHaveBeenCalled();
  });

  it('leaves plain arrow keys to the browser', () => {
    // Hijacking them would break a screen reader's own navigation.
    const { props } = renderTree();

    fireEvent.keyDown(screen.getAllByRole('option')[1]!, { key: 'ArrowUp' });

    expect(props.onReorder).not.toHaveBeenCalled();
  });

  it('reorders on drop', () => {
    const { props } = renderTree();
    const rows = screen.getAllByRole('option').map((row) => row.parentElement!);

    fireEvent.dragStart(rows[1]!);
    fireEvent.drop(rows[0]!);

    expect(props.onReorder).toHaveBeenCalledWith(1, 0);
  });
});

// --- hover-revealed actions -------------------------------------------------

describe('row actions', () => {
  it('are hidden until the row is hovered', () => {
    renderTree();

    expect(screen.queryByLabelText('Opacity of Wolfcamp A Control Points')).toBeNull();
  });

  it('appear on hover', () => {
    const { container } = renderTree();

    fireEvent.mouseEnter(container.querySelector('li')!);

    expect(screen.getByLabelText('Opacity of Wolfcamp A Control Points')).toBeDefined();
  });

  it('appear on focus too, so they are reachable without a mouse', () => {
    // **§10, stated as a rule:** "Row actions that appear on hover must also
    // appear on focus, and must be reachable in tab order. A hover-only
    // affordance is invisible to keyboard users."
    const { container } = renderTree();

    fireEvent.focus(container.querySelector('li')!);

    expect(screen.getByLabelText('Opacity of Wolfcamp A Control Points')).toBeDefined();
    expect(screen.getByLabelText('Remove Wolfcamp A Control Points')).toBeDefined();
  });

  it('stay visible on the selected row', () => {
    // The row being worked on is the one whose controls should not vanish
    // when the pointer moves to the symbology panel.
    renderTree({ selectedId: 'a' });

    expect(screen.getByLabelText('Opacity of Wolfcamp A Control Points')).toBeDefined();
  });

  it('change opacity without changing selection', () => {
    const { props, container } = renderTree();
    fireEvent.mouseEnter(container.querySelector('li')!);

    fireEvent.change(screen.getByLabelText('Opacity of Wolfcamp A Control Points'), {
      target: { value: '0.5' },
    });

    expect(props.onOpacityChange).toHaveBeenCalledWith('a', 0.5);
    expect(props.onSelect).not.toHaveBeenCalled();
  });
});

// --- context menu -----------------------------------------------------------

describe('context menu', () => {
  it('opens on right-click', () => {
    const { container } = renderTree({ onZoomTo: vi.fn() });

    fireEvent.contextMenu(container.querySelector('li')!);

    expect(screen.getByRole('menu')).toBeDefined();
    expect(screen.getByRole('menuitem', { name: 'Zoom to layer' })).toBeDefined();
  });

  it('opens on Shift+F10 with identical contents', () => {
    // **§10, stated as a rule:** "Every right-click context menu is also on
    // Shift+F10 / the context-menu key, with identical contents.
    // Right-click-only functionality is inaccessible."
    const { container } = renderTree({ onZoomTo: vi.fn() });

    fireEvent.keyDown(screen.getAllByRole('option')[0]!, { key: 'F10', shiftKey: true });
    const viaKeyboard = screen.getAllByRole('menuitem').map((item) => item.textContent);
    cleanup();

    const second = renderTree({ onZoomTo: vi.fn() });
    fireEvent.contextMenu(second.container.querySelector('li')!);
    const viaMouse = screen.getAllByRole('menuitem').map((item) => item.textContent);

    expect(viaKeyboard).toEqual(viaMouse);
    expect(container).toBeDefined();
  });

  it('opens on the dedicated context-menu key', () => {
    renderTree();

    fireEvent.keyDown(screen.getAllByRole('option')[0]!, { key: 'ContextMenu' });

    expect(screen.getByRole('menu')).toBeDefined();
  });

  it('closes on Escape', () => {
    renderTree();
    fireEvent.keyDown(screen.getAllByRole('option')[0]!, { key: 'F10', shiftKey: true });

    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' });

    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('removes the layer from the menu', () => {
    const { props } = renderTree();
    fireEvent.keyDown(screen.getAllByRole('option')[0]!, { key: 'F10', shiftKey: true });

    fireEvent.click(screen.getByRole('menuitem', { name: 'Remove layer' }));

    expect(props.onRemove).toHaveBeenCalledWith('a');
  });
});

// --- swatches ---------------------------------------------------------------

describe('swatches', () => {
  it('takes the colour from the compiled style, not the model', () => {
    // §6: "so the tree and the map cannot disagree". Reading the model works
    // today and drifts the first time the compiler decides something the
    // model does not spell out — as it already does for graduated fills.
    const graduated: Symbology = {
      type: 'graduated',
      field: 'porosity',
      method: 'pretty',
      classCount: 2,
      breaks: [10],
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
    const { container } = renderTree({ layers: [layer('a', 'Porosity', { symbology: graduated })] });

    const swatch = container.querySelector('span[aria-hidden="true"]') as HTMLElement;
    // The ramp's low end, which is also the legend's first entry — not the
    // model's placeholder fillColor of #cccccc.
    expect(swatch.style.background).toBe('rgb(68, 1, 84)');
  });

  it('falls back to neutral rather than crashing the panel', () => {
    // A symbology the compiler refuses — a missing palette, mismatched breaks
    // — must not take the whole layer panel down with it.
    const broken: Symbology = {
      type: 'graduated',
      field: 'porosity',
      method: 'pretty',
      classCount: 5,
      breaks: [1],
      paletteId: 'absent',
      vary: 'color',
      baseSymbol: {
        geometry: 'polygon',
        fillColor: '#cccccc',
        fillOpacity: 1,
        outlineColor: '#000000',
        outlineWidth: 0,
      },
    };

    expect(() => renderTree({ layers: [layer('a', 'Broken', { symbology: broken })] })).not.toThrow();
    expect(screen.getByText('Broken')).toBeDefined();
  });
});

// --- basemap ----------------------------------------------------------------

describe('basemap group', () => {
  it('renders separately from data layers and is not reorderable with them', () => {
    // A user preference, not a per-session choice — so it is not in the
    // sortable list at all.
    renderTree({ basemapLayers: [{ id: 'grey', name: 'Grey canvas', visible: true }] });

    expect(screen.getByLabelText('Basemap layers')).toBeDefined();
    expect(screen.getAllByRole('option')).toHaveLength(2);
  });

  it('is absent entirely when there is no basemap', () => {
    renderTree();

    expect(screen.queryByLabelText('Basemap layers')).toBeNull();
  });
});
