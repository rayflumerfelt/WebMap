/**
 * The edit session. `09-editing.md` §3.4, §5.
 *
 * Two properties carry most of the weight and both are asserted directly:
 *
 * - **Undo is atomic over a command's whole delta set.** Topological editing
 *   mutates N features in one gesture, and an undo that restored two of three
 *   leaves the layer with a gap that nothing reports.
 * - **Undoing back to the start leaves the session clean.** Otherwise Save
 *   stays enabled with nothing to send and the tab warns about unsaved changes
 *   that no longer exist.
 */

import { describe, expect, it } from 'vitest';

import {
  apply,
  commit,
  current,
  discard,
  isDirty,
  isStale,
  openSession,
  pendingDeltas,
  rebase,
  redo,
  restoreSnapshot,
  snapshot,
  undo,
  UNDO_LIMIT,
} from './session.js';
import type { Command, EditSession, Feature } from './session.js';

function feature(id: string, x: number): Feature {
  return { id, geometry: { type: 'Point', coordinates: [x, 0] }, properties: { name: id } };
}

function moved(id: string, from: number, to: number): Command {
  return {
    id: `move-${id}-${to}`,
    label: 'Move Vertex',
    deltas: [{ featureId: id, before: feature(id, from), after: feature(id, to) }],
    timestamp: 1,
  };
}

function session(): EditSession {
  return openSession('layer-1', 7, [feature('a', 0), feature('b', 10), feature('c', 20)]);
}

describe('the dirty buffer', () => {
  it('reads through to the exact geometry until something is edited', () => {
    const state = session();
    expect(current(state, 'a')).toEqual(feature('a', 0));
    expect(isDirty(state)).toBe(false);
  });

  it('reads the dirty version once there is one', () => {
    // The single reader every operation uses. An operation reading the exact
    // cache would compute its change against the state before the last edit,
    // producing a second edit that silently undoes the first.
    const state = session();
    apply(state, moved('a', 0, 5));
    expect(current(state, 'a')).toEqual(feature('a', 5));
  });

  it('keeps a deleted feature in the buffer as null', () => {
    // Dropping the key instead would make a pending delete indistinguishable
    // from an untouched feature, and the delete would never be sent.
    const state = session();
    apply(state, {
      id: 'delete-b',
      label: 'Delete',
      deltas: [{ featureId: 'b', before: feature('b', 10), after: null }],
      timestamp: 1,
    });

    expect(current(state, 'b')).toBeNull();
    expect(state.dirty.has('b')).toBe(true);
  });

  it('refuses a command that changes nothing', () => {
    // It would still consume an undo press: the user hits Ctrl+Z, nothing
    // changes, and they hit it again.
    expect(() =>
      apply(session(), { id: 'x', label: 'Nothing', deltas: [], timestamp: 1 }),
    ).toThrow(/carries no feature changes/);
  });
});

describe('undo and redo', () => {
  it('treats a multi-feature command atomically', () => {
    // **The reason a command carries a set.** Topological editing moves a
    // shared boundary and both neighbours follow; an undo that restored one of
    // them leaves a gap nothing reports.
    const state = session();
    apply(state, {
      id: 'topo',
      label: 'Move Shared Boundary',
      deltas: [
        { featureId: 'a', before: feature('a', 0), after: feature('a', 1) },
        { featureId: 'b', before: feature('b', 10), after: feature('b', 11) },
      ],
      timestamp: 1,
    });

    undo(state);
    expect(current(state, 'a')).toEqual(feature('a', 0));
    expect(current(state, 'b')).toEqual(feature('b', 10));
  });

  it('leaves the session clean when everything is undone', () => {
    // Otherwise Save stays enabled with nothing to send, and the tab warns
    // about unsaved changes that no longer exist.
    const state = session();
    apply(state, moved('a', 0, 5));
    apply(state, moved('a', 5, 9));

    undo(state);
    undo(state);

    expect(isDirty(state)).toBe(false);
    expect(state.dirty.size).toBe(0);
  });

  it('is clean after a move and a move back, which is two commands', () => {
    // "Unchanged" is decided by comparing against the exact cache, not by
    // counting operations.
    const state = session();
    apply(state, moved('a', 0, 5));
    apply(state, moved('a', 5, 0));

    expect(current(state, 'a')).toEqual(feature('a', 0));
    // Still dirty: the buffer holds a feature equal to the original, and only
    // an undo prunes it. That is the honest state — the session cannot know a
    // round trip happened until it walks back through it.
    expect(state.dirty.size).toBe(1);

    undo(state);
    undo(state);
    expect(isDirty(state)).toBe(false);
  });

  it('restores a creation by removing the feature again', () => {
    const state = session();
    apply(state, {
      id: 'create',
      label: 'Draw Polygon',
      deltas: [{ featureId: 'new-1', before: null, after: feature('new-1', 99) }],
      timestamp: 1,
    });
    expect(current(state, 'new-1')).not.toBeNull();

    undo(state);
    expect(current(state, 'new-1')).toBeNull();
    expect(state.dirty.has('new-1')).toBe(false);
  });

  it('clears redo on any new command', () => {
    // `09` §3.4: replaying forward from a state the server never accepted
    // produces a layer nobody authored.
    const state = session();
    apply(state, moved('a', 0, 5));
    undo(state);
    expect(state.redoStack).toHaveLength(1);

    apply(state, moved('b', 10, 12));
    expect(state.redoStack).toHaveLength(0);
  });

  it('redoes what it undid', () => {
    const state = session();
    apply(state, moved('a', 0, 5));
    undo(state);
    redo(state);
    expect(current(state, 'a')).toEqual(feature('a', 5));
  });

  it('does nothing on an empty stack rather than throwing', () => {
    const state = session();
    expect(undo(state)).toBeNull();
    expect(redo(state)).toBeNull();
  });

  it('bounds the undo stack', () => {
    const state = session();
    for (let step = 0; step < UNDO_LIMIT + 20; step += 1) {
      apply(state, moved('a', step, step + 1));
    }
    expect(state.undoStack).toHaveLength(UNDO_LIMIT);
  });
});

describe('saving', () => {
  it('sends one delta per feature however many times it moved', () => {
    // `adr/0005`: the server writes a whole immutable object either way, and
    // four deltas for one feature is four chances to disagree about the order.
    const state = session();
    apply(state, moved('a', 0, 1));
    apply(state, moved('a', 1, 2));
    apply(state, moved('a', 2, 3));

    const deltas = pendingDeltas(state);
    expect(deltas).toHaveLength(1);
    expect(deltas[0]?.before).toEqual(feature('a', 0));
    expect(deltas[0]?.after).toEqual(feature('a', 3));
  });

  it('adopts the saved features and moves the version pointer', () => {
    const state = session();
    apply(state, moved('a', 0, 5));
    commit(state, 8);

    expect(state.baseVersion).toBe(8);
    expect(isDirty(state)).toBe(false);
    expect(state.exactCache.get('a')).toEqual(feature('a', 5));
  });

  it('clears both stacks on commit', () => {
    // After a commit there is nothing local to undo — the previous state is a
    // version on the server, and undoing into it would leave a buffer that
    // disagrees with a `baseVersion` the session has moved past.
    const state = session();
    apply(state, moved('a', 0, 5));
    commit(state, 8);

    expect(state.undoStack).toHaveLength(0);
    expect(undo(state)).toBeNull();
  });

  it('drops a deleted feature from the exact cache on commit', () => {
    const state = session();
    apply(state, {
      id: 'delete-c',
      label: 'Delete',
      deltas: [{ featureId: 'c', before: feature('c', 20), after: null }],
      timestamp: 1,
    });
    commit(state, 8);

    expect(state.exactCache.has('c')).toBe(false);
  });

  it('discards the buffer and both stacks', () => {
    const state = session();
    apply(state, moved('a', 0, 5));
    apply(state, moved('b', 10, 15));
    discard(state);

    expect(isDirty(state)).toBe(false);
    expect(state.undoStack).toHaveLength(0);
    expect(current(state, 'a')).toEqual(feature('a', 0));
  });
});

describe('conflict', () => {
  it('rebases: the named features come from the server, the rest are kept', () => {
    // `09` §5.3's **Refresh**. The 409 names the features that changed
    // underneath, and only those lose their local edits.
    const state = session();
    apply(state, moved('a', 0, 5));
    apply(state, moved('b', 10, 15));

    rebase(state, ['a'], [feature('a', 100)], 9);

    expect(current(state, 'a')).toEqual(feature('a', 100));
    expect(current(state, 'b')).toEqual(feature('b', 15));
    expect(state.baseVersion).toBe(9);
  });

  it('clears the undo stack on a rebase', () => {
    // A stack whose entries reference features just replaced underneath would
    // restore a `before` that is no longer anybody's state.
    const state = session();
    apply(state, moved('a', 0, 5));
    rebase(state, ['a'], [feature('a', 100)], 9);

    expect(state.undoStack).toHaveLength(0);
    expect(state.redoStack).toHaveLength(0);
  });
});

describe('crash durability', () => {
  it('snapshots plain data with no Maps', () => {
    // `09` §5.4 mirrors this to IndexedDB every applied operation. A Map does
    // not survive a JSON round trip, and `structuredClone` handles one only in
    // browsers that have it — plain arrays work everywhere.
    const state = session();
    apply(state, moved('a', 0, 5));

    const saved = snapshot(state);
    expect(JSON.parse(JSON.stringify(saved))).toEqual(saved);
  });

  it('leaves the exact cache out of the snapshot', () => {
    // It is a copy of what the server holds and can be fetched again;
    // persisting it multiplies the stored size by the size of the layer.
    const saved: object = snapshot(session());
    expect(Object.keys(saved)).not.toContain('exactCache');
  });

  it('restores a buffer onto a freshly loaded session', () => {
    const before = session();
    apply(before, moved('a', 0, 5));
    const saved = snapshot(before);

    const after = session();
    restoreSnapshot(after, saved);

    expect(current(after, 'a')).toEqual(feature('a', 5));
    expect(after.undoStack).toHaveLength(1);
    expect(undo(after)).not.toBeNull();
  });

  it('reports a snapshot taken against a version the layer has moved past', () => {
    // The case `09` §5.4's recovery prompt has to mention: the buffer is
    // recoverable, and it is not obviously still correct.
    const saved = snapshot(session());
    const moved_on = openSession('layer-1', 9, [feature('a', 0)]);

    expect(isStale(moved_on, saved)).toBe(true);
    expect(isStale(session(), saved)).toBe(false);
  });
});
