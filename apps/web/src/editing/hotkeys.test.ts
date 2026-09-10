/**
 * The editing hotkey layer. `09-editing.md` §8.
 *
 * The point of this module is that it *resolves* rather than *registers*, so
 * the tests are about resolution: a shared key resolving to the one command the
 * state enables, an ambiguous one resolving to nothing rather than to a guess,
 * and the two cases where a key should not be a shortcut at all.
 */

import { describe, expect, it } from 'vitest';

import { chordString, resolve, shouldPreventDefault } from './hotkeys.js';
import { IDLE } from './types.js';
import type { EditState } from './types.js';

function editing(overrides: Partial<EditState> = {}): EditState {
  return {
    ...IDLE,
    activeLayerId: 'leases',
    activeLayerGeometry: 'polygon',
    canEdit: true,
    ...overrides,
  };
}

describe('chords', () => {
  it('spells Ctrl and Meta the same way the registry does', () => {
    // `mod` matches either: a Windows user on a Mac keyboard, and a remote
    // session where the modifier does not match the host, otherwise lose every
    // shortcut. No editing shortcut distinguishes the two.
    expect(chordString({ key: 's', ctrlKey: true })).toBe('mod+s');
    expect(chordString({ key: 's', metaKey: true })).toBe('mod+s');
    expect(chordString({ key: 'Z', ctrlKey: true, shiftKey: true })).toBe('mod+shift+z');
  });

  it('keeps the casing of a named key', () => {
    expect(chordString({ key: 'Delete' })).toBe('Delete');
    expect(chordString({ key: 'Escape' })).toBe('Escape');
  });
});

describe('resolution', () => {
  it('runs the one command the state enables for a shared key', () => {
    // `Delete` is Delete Feature under Edit and Delete Selected Vertices under
    // Vertices. Both are defined; the state decides which is live.
    const withFeatures = editing({ selectedFeatureCount: 2, selection: 'features' });
    const withVertices = editing({
      activeLayerGeometry: 'line',
      selectedFeatureCount: 1,
      selection: 'vertices',
      selectedVertexCount: 3,
    });

    const first = resolve({ key: 'Delete' }, withFeatures);
    const second = resolve({ key: 'Delete' }, withVertices);

    expect(first.kind === 'command' && first.command.id).toBe('edit.delete');
    expect(second.kind === 'command' && second.command.id).toBe('vertex.delete');
  });

  it('resolves to nothing when no command is enabled', () => {
    expect(resolve({ key: 'z', ctrlKey: true }, IDLE).kind).toBe('none');
  });

  it('resolves an unbound key to nothing', () => {
    expect(resolve({ key: 'q', ctrlKey: true }, editing()).kind).toBe('none');
  });

  it('ignores an Alt chord', () => {
    // Alt+Arrow reorders layers and Alt bypasses snapping while drawing. An
    // Alt chord reaching here is meant for something else, and swallowing it
    // would break the thing it was meant for.
    expect(resolve({ key: 'm', altKey: true }, editing()).kind).toBe('none');
  });

  it('ignores everything except Escape while typing', () => {
    // Cancelling out of a field is exactly what a user expects Escape to do,
    // and it is the one key whose meaning does not change with focus.
    const state = editing({ dirty: true });
    expect(resolve({ key: 's', ctrlKey: true }, state, { typing: true }).kind).toBe('none');
    // Escape is not a registry shortcut — the mode machine owns it — so this
    // asserts that it is *let through*, not that it resolves to a command.
    expect(resolve({ key: 'Escape' }, state, { typing: true }).kind).toBe('none');
    expect(resolve({ key: 's', ctrlKey: true }, state).kind).toBe('command');
  });
});

describe('preventing the default', () => {
  it('happens only when a command actually ran', () => {
    // Swallowing keys indiscriminately breaks the browser's own Ctrl+F and
    // Ctrl+S for anyone who expected them.
    const ran = resolve({ key: 's', ctrlKey: true }, editing({ dirty: true }));
    const nothing = resolve({ key: 'f', ctrlKey: true }, editing());

    expect(shouldPreventDefault(ran)).toBe(true);
    expect(shouldPreventDefault(nothing)).toBe(false);
  });

  it('leaves an ambiguous chord to the browser', () => {
    // An ambiguous chord is a registry bug. Eating the key as well as failing
    // to act on it would turn one bug into two.
    const ambiguous = { kind: 'ambiguous' as const, commands: [] };
    expect(shouldPreventDefault(ambiguous)).toBe(false);
  });
});
