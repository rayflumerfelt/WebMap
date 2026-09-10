/**
 * The menu bar and the command palette. `09-editing.md` §8, §9.
 *
 * The two surfaces exist to answer different questions and the tests here are
 * mostly about that difference: a menu is where you find out what a tool can
 * do, so a disabled command stays visible with its shortcut; a palette is where
 * you go when you already know, so a row you can land on and not run is noise.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { CommandPalette } from './CommandPalette.js';
import { MenuBar } from './MenuBar.js';
import { buildRegistry } from './registry.js';
import { IDLE } from './types.js';
import type { EditState } from './types.js';

afterEach(cleanup);

const COMMANDS = buildRegistry();

function editing(overrides: Partial<EditState> = {}): EditState {
  return {
    ...IDLE,
    activeLayerId: 'leases',
    activeLayerGeometry: 'polygon',
    canEdit: true,
    ...overrides,
  };
}

describe('the menu bar', () => {
  it('lists §9 groups in order', () => {
    render(<MenuBar state={IDLE} commands={COMMANDS} onRun={vi.fn()} />);
    const labels = screen.getAllByRole('menuitem').map((node) => node.textContent);

    expect(labels).toEqual([
      'File',
      'Layers',
      'Select',
      'Edit',
      'Vertices',
      'Transform',
      'Snap',
      'View',
    ]);
  });

  it('keeps a disabled command visible with its shortcut', () => {
    // The difference from the palette. A menu is where you learn what the tool
    // can do, and a greyed Undo with `Ctrl+Z` beside it teaches the shortcut.
    render(<MenuBar state={IDLE} commands={COMMANDS} onRun={vi.fn()} />);
    fireEvent.click(screen.getByRole('menuitem', { name: 'Edit' }));

    const undo = screen.getByRole('menuitem', { name: /Undo/ }) as HTMLButtonElement;
    expect(undo.disabled).toBe(true);
    expect(undo.textContent).toMatch(/Z$/);
  });

  it('shows a toggle as checked', () => {
    render(
      <MenuBar state={editing({ snapEnabled: true })} commands={COMMANDS} onRun={vi.fn()} />,
    );
    fireEvent.click(screen.getByRole('menuitem', { name: 'Snap' }));

    const item = screen.getByRole('menuitemcheckbox', { name: /Enable Snapping/ });
    expect(item.getAttribute('aria-checked')).toBe('true');
  });

  it('runs a command and closes', () => {
    const onRun = vi.fn();
    render(<MenuBar state={editing()} commands={COMMANDS} onRun={onRun} />);
    fireEvent.click(screen.getByRole('menuitem', { name: 'Snap' }));
    fireEvent.click(screen.getByRole('menuitemcheckbox', { name: /Enable Snapping/ }));

    expect(onRun).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('moves between menus with the arrow keys and closes on Escape', () => {
    // `07` §10: every mouse-reachable action is keyboard-reachable, and a menu
    // bar is where that is usually half-done.
    render(<MenuBar state={editing()} commands={COMMANDS} onRun={vi.fn()} />);
    fireEvent.click(screen.getByRole('menuitem', { name: 'File' }));
    expect(screen.getByRole('menu', { name: 'File' })).toBeTruthy();

    fireEvent.keyDown(screen.getByRole('menubar'), { key: 'ArrowRight' });
    expect(screen.getByRole('menu', { name: 'Layers' })).toBeTruthy();

    fireEvent.keyDown(screen.getByRole('menubar'), { key: 'Escape' });
    expect(screen.queryByRole('menu')).toBeNull();
  });
});

describe('the command palette', () => {
  function palette(state: EditState, overrides: Record<string, unknown> = {}) {
    const props = {
      open: true,
      state,
      commands: COMMANDS,
      onRun: vi.fn(),
      onClose: vi.fn(),
      ...overrides,
    };
    render(<CommandPalette {...(props as Parameters<typeof CommandPalette>[0])} />);
    return props;
  }

  it('excludes what cannot run', () => {
    // A greyed row you can land on and not run is noise on the one surface
    // where speed is the whole point.
    palette(IDLE);
    expect(screen.queryByRole('option', { name: /Undo/ })).toBeNull();
  });

  it('finds Combine and Dissolve when a user types "merge"', () => {
    // §9.1 forbids labelling either of them Merge, because the ambiguity
    // produces silent data loss. This is what has to catch the word a user
    // arrives with — and show the sentence that distinguishes the two.
    palette(editing({ selectedFeatureCount: 3, selection: 'features' }));
    fireEvent.change(screen.getByLabelText('Search commands'), {
      target: { value: 'merge' },
    });

    const labels = screen.getAllByRole('option').map((node) => node.textContent);
    expect(labels.some((text) => text?.startsWith('Combine'))).toBe(true);
    expect(labels.some((text) => text?.startsWith('Dissolve'))).toBe(true);
    expect(screen.getByText(/Geometry unchanged/)).toBeTruthy();
    expect(screen.getByText(/Geometry changes/)).toBeTruthy();
  });

  it('runs the highlighted row on Enter', () => {
    const props = palette(editing({ selectedFeatureCount: 2, selection: 'features' }));
    const field = screen.getByLabelText('Search commands');

    fireEvent.change(field, { target: { value: 'dissolve' } });
    fireEvent.keyDown(field, { key: 'Enter' });

    expect(props.onRun).toHaveBeenCalledTimes(1);
    expect((props.onRun as ReturnType<typeof vi.fn>).mock.calls[0]?.[0]?.id).toBe(
      'edit.dissolve',
    );
  });

  it('moves the highlight with the arrow keys', () => {
    palette(editing({ selectedFeatureCount: 2, selection: 'features' }));
    const field = screen.getByLabelText('Search commands');

    fireEvent.change(field, { target: { value: 'select' } });
    const before = screen.getAllByRole('option')[0]?.getAttribute('aria-selected');
    fireEvent.keyDown(field, { key: 'ArrowDown' });
    const after = screen.getAllByRole('option')[0]?.getAttribute('aria-selected');

    expect(before).toBe('true');
    expect(after).toBe('false');
  });

  it('says why nothing matched rather than showing an empty box', () => {
    // The usual reason is a state the user can fix — nothing selected, no
    // active layer — rather than a command that does not exist.
    palette(IDLE);
    fireEvent.change(screen.getByLabelText('Search commands'), {
      target: { value: 'dissolve' },
    });

    expect(screen.getByText(/need a selection or an active layer/)).toBeTruthy();
  });

  it('closes on Escape', () => {
    const props = palette(editing());
    fireEvent.keyDown(screen.getByLabelText('Search commands'), { key: 'Escape' });
    expect(props.onClose).toHaveBeenCalledTimes(1);
  });

  it('renders nothing while closed', () => {
    palette(editing(), { open: false });
    expect(screen.queryByRole('dialog')).toBeNull();
  });
});
