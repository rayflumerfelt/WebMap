/**
 * The command registry. `09-editing.md` §8, §9.
 *
 * What is asserted here is the set of claims the registry makes that a surface
 * would otherwise have to re-make: that the menu matches §9's structure, that a
 * disabled command is greyed in the menu and absent from the palette, that no
 * two commands claim the same key in the same state, and that Combine and
 * Dissolve are distinguishable to somebody searching for "merge".
 *
 * The last one is not pedantry. `09` §9.1: never label two commands "Merge" —
 * the ambiguity between multi-part wrapping and true union produces silent data
 * loss in whichever direction the user did not intend.
 */

import { describe, expect, it, vi } from 'vitest';

import {
  GROUP_ORDER,
  bindings,
  buildRegistry,
  byId,
  COMMANDS,
  conflicts,
  contextEntries,
  menus,
  paletteEntries,
} from './registry.js';
import { IDLE } from './types.js';
import type { EditState } from './types.js';

function editing(overrides: Partial<EditState> = {}): EditState {
  return {
    ...IDLE,
    activeLayerId: 'layer-1',
    activeLayerGeometry: 'polygon',
    canEdit: true,
    ...overrides,
  };
}

describe('definition', () => {
  it('defines every command exactly once', () => {
    const ids = COMMANDS.map((command) => command.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('covers every group in §9 and no others', () => {
    const groups = new Set(COMMANDS.map((command) => command.group));
    expect([...groups].sort()).toEqual([...GROUP_ORDER].sort());
  });

  it('uses `mod` rather than ctrl or cmd', () => {
    // The platform difference belongs to the hotkey layer. Writing it here
    // means writing it twice, and two spellings drift.
    const wrong = COMMANDS.filter((command) =>
      /(^|\+)(ctrl|cmd|meta)\+/i.test(command.shortcut ?? ''),
    );
    expect(wrong.map((command) => command.id)).toEqual([]);
  });

  it('dispatches to the handler its id names', () => {
    const split = vi.fn();
    const registry = buildRegistry({ 'edit.split': split });
    const state = editing({ selectedFeatureCount: 1, selection: 'feature' });

    byId(registry).get('edit.split')?.run(state);
    expect(split).toHaveBeenCalledExactlyOnceWith(state);
  });

  it('registers a command with no handler rather than hiding it', () => {
    // A command whose implementation has not landed is still defined, still
    // reserves its shortcut, and still appears. Running it does nothing, which
    // is the honest state; hiding it would make the menu a moving target.
    const state = editing({ selectedFeatureCount: 2, selection: 'features' });
    const dissolve = byId().get('edit.dissolve');

    expect(dissolve).toBeDefined();
    expect(dissolve?.enabled(state)).toBe(true);
    expect(() => dissolve?.run(state)).not.toThrow();
  });
});

describe('the menu', () => {
  it('is in §9 order', () => {
    const order = menus(IDLE).map((menu) => menu.group);
    expect(order).toEqual(GROUP_ORDER);
  });

  it('keeps a disabled command visible and greyed', () => {
    // `09` §8: it stays discoverable and its shortcut stays visible.
    const editMenu = menus(IDLE).find((menu) => menu.group === 'edit');
    const undo = editMenu?.items.find((item) => item.id === 'edit.undo');

    expect(undo).toBeDefined();
    expect(undo?.enabled(IDLE)).toBe(false);
    expect(undo?.shortcut).toBe('mod+z');
  });
});

describe('the palette', () => {
  it('excludes disabled commands, where a greyed row is noise', () => {
    const ids = paletteEntries(IDLE, '').map((command) => command.id);
    expect(ids).not.toContain('edit.undo');
    expect(ids).not.toContain('edit.dissolve');
  });

  it('finds Combine and Dissolve when somebody searches for "merge"', () => {
    // Neither is *labelled* Merge — `09` §9.1 forbids it — so a search that
    // returned nothing would leave a user from another tool with no way in.
    const state = editing({ selectedFeatureCount: 3, selection: 'features' });
    const found = paletteEntries(state, 'merge').map((command) => command.id);

    expect(found).toContain('edit.combine');
    expect(found).toContain('edit.dissolve');
  });

  it('describes the two so the difference is readable in the row', () => {
    const combine = byId().get('edit.combine');
    const dissolve = byId().get('edit.dissolve');

    expect(combine?.description).toMatch(/geometry unchanged/i);
    expect(dissolve?.description).toMatch(/union/i);
    expect(dissolve?.description).toMatch(/geometry changes/i);
  });

  it('matches on the label too, not only on keywords', () => {
    const state = editing({ selectedFeatureCount: 1, selection: 'feature' });
    const found = paletteEntries(state, 'rotate').map((command) => command.id);
    expect(found).toContain('transform.rotate');
  });
});

describe('shortcuts', () => {
  it('is the single registrar', () => {
    // Every shortcut in the editor comes from here. A second registrar is how
    // two commands come to share a key with nothing to notice it.
    const keys = [...bindings().keys()];
    expect(keys).toContain('mod+s');
    expect(keys).toContain('v');
    expect(keys).toContain('m');
  });

  it('lets two commands share a key only when no state enables both', () => {
    // `Delete` is Delete Feature under Edit and Delete Vertices under
    // Vertices. Both are defined; the state decides which is live.
    const shared = bindings().get('Delete');
    expect(shared).toEqual(['edit.delete', 'vertex.delete']);
  });

  it.each([
    ['nothing open', IDLE],
    ['a layer, nothing selected', editing()],
    ['one feature selected', editing({ selectedFeatureCount: 1, selection: 'feature' })],
    ['several features', editing({ selectedFeatureCount: 4, selection: 'features' })],
    [
      'vertices selected',
      editing({
        selectedFeatureCount: 1,
        selection: 'vertices',
        selectedVertexCount: 3,
        activeLayerGeometry: 'line',
      }),
    ],
    ['an operation running', editing({ operationActive: true, dirty: true })],
    ['dirty, ready to save', editing({ dirty: true, canUndo: true })],
    ['read-only', editing({ canEdit: false, selectedFeatureCount: 2 })],
    ['a point layer', editing({ activeLayerGeometry: 'point', selectedFeatureCount: 1 })],
  ])('resolves unambiguously with %s', (_name, state) => {
    expect(conflicts(state)).toEqual([]);
  });
});

describe('what the state allows', () => {
  it('enables nothing that edits with no active layer', () => {
    const mutating = COMMANDS.filter(
      (command) => command.group === 'edit' || command.group === 'vertices',
    );
    const live = mutating.filter((command) => command.enabled(IDLE));
    expect(live.map((command) => command.id)).toEqual([]);
  });

  it('greys the editing commands for a viewer', () => {
    // A viewer opens the tools and finds them greyed, which is more
    // informative than not having them at all.
    const viewer = editing({ canEdit: false, selectedFeatureCount: 2, selection: 'features' });
    expect(byId().get('edit.delete')?.enabled(viewer)).toBe(false);
    expect(byId().get('edit.dissolve')?.enabled(viewer)).toBe(false);
  });

  it('still allows Copy for a viewer', () => {
    // Copy reads. Copying a boundary into your own layer is the ordinary way
    // work gets reused, and requiring edit rights on the *source* would stop it.
    const viewer = editing({ canEdit: false, selectedFeatureCount: 1, selection: 'feature' });
    expect(byId().get('edit.copy')?.enabled(viewer)).toBe(true);
  });

  it('needs two features for Combine and Dissolve', () => {
    // A "merge" of one feature is a no-op that looks like it worked — for
    // Dissolve, a geologist believing two leases were unioned.
    const one = editing({ selectedFeatureCount: 1, selection: 'feature' });
    expect(byId().get('edit.combine')?.enabled(one)).toBe(false);
    expect(byId().get('edit.dissolve')?.enabled(one)).toBe(false);
  });

  it('offers no vertex editing on a point layer', () => {
    // A point has one vertex and moving it is Move. A mode that does nothing
    // reads as the tool being broken.
    const points = editing({
      activeLayerGeometry: 'point',
      selectedFeatureCount: 1,
      selection: 'feature',
    });
    expect(byId().get('vertex.mode')?.enabled(points)).toBe(false);
    expect(byId().get('vertex.add')?.enabled(points)).toBe(false);
  });

  it('offers no dissolve on a line layer', () => {
    const lines = editing({
      activeLayerGeometry: 'line',
      selectedFeatureCount: 2,
      selection: 'features',
    });
    expect(byId().get('edit.dissolve')?.enabled(lines)).toBe(false);
    expect(byId().get('edit.combine')?.enabled(lines)).toBe(true);
  });

  it('blocks a new operation while one is running', () => {
    // `09` §5.1 keeps operation state out of the dirty buffer, so a second
    // operation would leave the first with nowhere to go.
    const busy = editing({
      operationActive: true,
      selectedFeatureCount: 2,
      selection: 'features',
    });
    expect(byId().get('edit.combine')?.enabled(busy)).toBe(false);
    expect(byId().get('transform.move')?.enabled(busy)).toBe(false);
  });

  it('disables Save while an operation bar is showing', () => {
    // `09` §5.2 says to pick disable-or-auto-apply and hold it. Alternating is
    // what makes a tool feel unpredictable.
    const busy = editing({ dirty: true, operationActive: true });
    expect(byId().get('session.save')?.enabled(busy)).toBe(false);
    expect(byId().get('session.save')?.enabled(editing({ dirty: true }))).toBe(true);
  });

  it('makes the select mode a radio group rather than three toggles', () => {
    const state = editing({ selectMode: 'lasso' });
    const modes = ['select.mode.click', 'select.mode.rectangle', 'select.mode.lasso'];
    const checked = modes.filter((id) => byId().get(id)?.checked?.(state));
    expect(checked).toEqual(['select.mode.lasso']);
  });
});

describe('context menus', () => {
  it('drops what does not apply rather than greying it', () => {
    // A context menu is opened *at* something. A greyed list of things that do
    // not apply to it is a worse answer than a short list that does.
    const state = editing({ selectedFeatureCount: 1, selection: 'feature' });
    const entries = contextEntries('feature', state);

    expect(entries.every((command) => command.enabled(state))).toBe(true);
    expect(entries.map((command) => command.id)).toContain('edit.copy');
  });

  it('offers vertex commands only in the vertex scope', () => {
    const state = editing({
      activeLayerGeometry: 'line',
      selectedFeatureCount: 1,
      selection: 'vertices',
      selectedVertexCount: 1,
    });
    expect(contextEntries('vertex', state).map((command) => command.id)).toContain(
      'vertex.delete',
    );
    expect(contextEntries('empty', state).map((command) => command.id)).not.toContain(
      'vertex.delete',
    );
  });

  it('offers Paste on empty space when there is something to paste', () => {
    const withClipboard = editing({ clipboardCount: 3 });
    expect(contextEntries('empty', withClipboard).map((c) => c.id)).toContain('edit.paste');
    expect(contextEntries('empty', editing()).map((c) => c.id)).not.toContain('edit.paste');
  });
});
