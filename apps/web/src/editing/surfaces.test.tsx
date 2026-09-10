/**
 * The editing surfaces. `09-editing.md` §10.
 *
 * Three claims are worth a test and the rest is markup:
 *
 * - **The toolbar budget.** §10.1 says about ten controls and "hold this line".
 *   A budget nobody counts is a budget that grows.
 * - **The toolbar reads the registry.** The toggles here and the same items in
 *   the Snap menu cannot disagree, because they read one `checked`.
 * - **The operation bar's `Esc` and `Enter`.** §5.1 gives them meanings that
 *   only hold while an operation is running, and the second of the two must
 *   not steal Enter from a textarea.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { EditStatusStrip, selectionSummary } from './EditStatusStrip.js';
import { EditToolbar, TOOLBAR_SLOTS } from './EditToolbar.js';
import { OperationBar } from './OperationBar.js';
import { buildRegistry } from './registry.js';
import { IDLE } from './types.js';
import type { EditState } from './types.js';

afterEach(cleanup);

function editing(overrides: Partial<EditState> = {}): EditState {
  return {
    ...IDLE,
    activeLayerId: 'leases',
    activeLayerGeometry: 'polygon',
    canEdit: true,
    ...overrides,
  };
}

function toolbar(overrides: Partial<Parameters<typeof EditToolbar>[0]> = {}) {
  const props = {
    state: editing(),
    mode: 'select' as const,
    selectTool: 'click' as const,
    commands: buildRegistry(),
    dirtyCount: 0,
    layers: [{ id: 'leases', name: 'Leases', canEdit: true }],
    onMode: vi.fn(),
    onSelectTool: vi.fn(),
    onRun: vi.fn(),
    onActiveLayer: vi.fn(),
    canSwitchMode: true,
    ...overrides,
  };
  render(<EditToolbar {...props} />);
  return props;
}

describe('the toolbar budget', () => {
  it('is about ten slots', () => {
    // §10.1: "Target: about ten controls on the edit toolbar. Hold this line."
    // A budget nobody counts is a budget that grows, one reasonable addition
    // at a time.
    expect(TOOLBAR_SLOTS.length).toBeLessThanOrEqual(10);
  });

  it('shows one sub-choice at a time, not all of them', () => {
    // The select tools and the draw shapes are each one slot because only the
    // active mode's sub-choice is on screen. Showing both would be how the
    // budget gets spent without anybody deciding to.
    toolbar({ mode: 'select' });
    expect(screen.getByRole('radiogroup', { name: 'Selection tool' })).toBeTruthy();
    expect(screen.queryByLabelText('Draw shape')).toBeNull();

    cleanup();
    toolbar({ mode: 'draw-polygon' });
    expect(screen.getByLabelText('Draw shape')).toBeTruthy();
    expect(screen.queryByRole('radiogroup', { name: 'Selection tool' })).toBeNull();
  });
});

describe('the toolbar reads the registry', () => {
  it('shows a constraint as pressed when the registry says it is checked', () => {
    toolbar({ state: editing({ snapEnabled: true, topologicalEditing: false }) });

    expect(screen.getByRole('button', { name: /^Snapping/ }).getAttribute('aria-pressed')).toBe(
      'true',
    );
    expect(
      screen.getByRole('button', { name: /Topological/ }).getAttribute('aria-pressed'),
    ).toBe('false');
  });

  it('greys a command the registry disables', () => {
    // Undo with nothing to undo. Greyed rather than absent, so the button and
    // its shortcut stay discoverable.
    toolbar({ state: editing({ canUndo: false }) });
    expect((screen.getByRole('button', { name: 'Undo' }) as HTMLButtonElement).disabled).toBe(
      true,
    );
  });

  it('runs the command the registry defines rather than a local handler', () => {
    const onRun = vi.fn();
    toolbar({ onRun });
    fireEvent.click(screen.getByRole('button', { name: /^Snapping/ }));

    expect(onRun).toHaveBeenCalledTimes(1);
    expect(onRun.mock.calls[0]?.[0]?.id).toBe('snap.enable');
  });

  it('badges snap when the pixel tolerance is in force', () => {
    // §6.3: a silent behaviour change generates bug reports; a badge generates
    // none. The title says which clamp bound and what it means.
    toolbar({ state: editing({ snapEnabled: true }), snapClamped: 'ceiling' });
    const snap = screen.getByRole('button', { name: /^Snapping/ });

    expect(snap.getAttribute('title')).toMatch(/maximum pixel tolerance/);
    expect(screen.getByLabelText('pixel tolerance in force')).toBeTruthy();
  });

  it('greys the mode buttons while an operation is running', () => {
    // §4 blocks the switch. Greyed rather than silently ignored: a button that
    // does nothing when clicked reads as a broken tool.
    toolbar({ canSwitchMode: false });
    expect((screen.getByRole('radio', { name: 'Vertex' }) as HTMLButtonElement).disabled).toBe(
      true,
    );
  });

  it('shows the unsaved count and keeps the layer selector prominent', () => {
    toolbar({ state: editing({ dirty: true }), dirtyCount: 7 });
    expect(screen.getByLabelText('7 unsaved edits')).toBeTruthy();
    expect(screen.getByLabelText('Editing')).toBeTruthy();
  });

  it('marks a read-only layer in the selector rather than hiding it', () => {
    // A layer the user cannot edit is still selectable, and the tools grey —
    // which says more than an absent layer would.
    toolbar({
      layers: [
        { id: 'leases', name: 'Leases', canEdit: true },
        { id: 'wells', name: 'Wells', canEdit: false },
      ],
    });
    expect(screen.getByRole('option', { name: 'Wells (read-only)' })).toBeTruthy();
  });
});

describe('the operation bar', () => {
  it('applies on Enter and cancels on Escape', () => {
    // §5.1: Esc cancels with no confirmation, because nothing is lost.
    const onApply = vi.fn();
    const onCancel = vi.fn();
    render(
      <OperationBar title="Split" hint="Click to add points. Enter applies." onApply={onApply} onCancel={onCancel} />,
    );

    fireEvent.keyDown(window, { key: 'Enter' });
    expect(onApply).toHaveBeenCalledTimes(1);

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('leaves Enter alone inside a textarea', () => {
    // Hijacking it would make a comment field unusable, and a modal operation
    // that ends when you press Return in a text box is worse than no shortcut.
    const onApply = vi.fn();
    render(
      <OperationBar title="Split" hint="…" onApply={onApply} onCancel={vi.fn()}>
        <textarea aria-label="Note" />
      </OperationBar>,
    );

    fireEvent.keyDown(screen.getByLabelText('Note'), { key: 'Enter', bubbles: true });
    expect(onApply).not.toHaveBeenCalled();
  });

  it('does not apply while the parameters are incomplete', () => {
    const onApply = vi.fn();
    render(
      <OperationBar title="Offset" hint="…" canApply={false} onApply={onApply} onCancel={vi.fn()} />,
    );

    fireEvent.keyDown(window, { key: 'Enter' });
    expect(onApply).not.toHaveBeenCalled();
    expect((screen.getByRole('button', { name: 'Apply' }) as HTMLButtonElement).disabled).toBe(
      true,
    );
  });

  it('always shows the hint, because a modal mode with no instruction is a dead end', () => {
    render(
      <OperationBar
        title="Split"
        hint="Click to add points along the cut line. Enter to apply, Esc to cancel."
        onApply={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.getByText(/Esc to cancel/)).toBeTruthy();
  });
});

describe('the status strip', () => {
  function strip(overrides: Partial<Parameters<typeof EditStatusStrip>[0]> = {}) {
    const props = {
      cursor: '1,234,567 · 892,100',
      crsLabel: 'EPSG:2277',
      snap: null,
      measurement: null,
      selectedCount: 0,
      onClearSelection: vi.fn(),
      ...overrides,
    };
    render(<EditStatusStrip {...props} />);
    return props;
  }

  it('distinguishes "no snap" from "snapping off"', () => {
    // The difference between "the tool is broken" and "I turned it off".
    strip({ snap: null });
    expect(screen.getByText('no snap')).toBeTruthy();

    cleanup();
    strip({ snap: null, snapDisabled: true });
    expect(screen.getByText('snapping off')).toBeTruthy();
  });

  it('names what is snapped and which layer', () => {
    // "I am snapped to the wrong layer" is only diagnosable if the layer is on
    // screen.
    strip({ snap: { type: 'vertex', layerName: 'Leases', isExact: true } });
    expect(screen.getByLabelText('Snap state').textContent).toMatch(/vertex · Leases/);
  });

  it('marks a tile-derived snap as approximate', () => {
    strip({ snap: { type: 'edge', layerName: 'Leases', isExact: false } });
    expect(screen.getByLabelText('Snap state').textContent).toMatch(/approx/);
  });

  it('clears the selection from the count, which is why it is not read-only', () => {
    const props = strip({ selectedCount: 3 });
    fireEvent.click(screen.getByLabelText('Clear selection'));
    expect(props.onClearSelection).toHaveBeenCalledTimes(1);
  });

  it('summarises a selection in acres to one decimal', () => {
    // A lease is quoted to the tenth of an acre. More digits implies a
    // precision the geometry does not have; fewer loses the number people
    // compare against a lease document.
    expect(selectionSummary(3, 640.23)).toBe('3 features · 640.2 ac');
    expect(selectionSummary(1, null)).toBe('1 feature');
    expect(selectionSummary(0, 12)).toBeNull();
  });
});
