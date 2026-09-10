/**
 * The edit session as React sees it. `09-editing.md` §3, §4, §8.
 *
 * The store's job is to hold four independent pieces together without letting
 * them disagree, so these tests are mostly about the seams: that a mutation of
 * the session is signalled, that a layer switch cannot silently drop edits, and
 * that the snapshot the command registry reads matches what is actually true.
 */

import { beforeEach, describe, expect, it } from 'vitest';

import { handleId } from '../editing/overlay.js';
import type { Command, Feature } from '../editing/session.js';
import {
  editState,
  handleFeatures,
  selectedVertices,
  useEditStore,
} from './editStore.js';

const SQUARE: Feature = {
  id: 'lease',
  geometry: {
    type: 'Polygon',
    coordinates: [
      [
        [0, 0],
        [1, 0],
        [1, 1],
        [0, 0],
      ],
    ],
  },
  properties: {},
};

const MOVED: Feature = {
  ...SQUARE,
  geometry: {
    type: 'Polygon',
    coordinates: [
      [
        [0, 0],
        [2, 0],
        [1, 1],
        [0, 0],
      ],
    ],
  },
};

function moveCommand(): Command {
  return {
    id: 'command-1',
    label: 'Move Vertex',
    deltas: [{ featureId: 'lease', before: SQUARE, after: MOVED }],
    timestamp: 0,
  };
}

function open() {
  useEditStore.getState().activateLayer({
    layerId: 'leases',
    baseVersion: 7,
    geometry: 'polygon',
    canEdit: true,
    features: [SQUARE],
  });
}

beforeEach(() => {
  useEditStore.setState({
    session: null,
    revision: 0,
    canEdit: false,
    activeLayerGeometry: null,
    clipboardCount: 0,
    topologicalEditing: false,
  });
  useEditStore.getState().dispatch({ type: 'setActiveLayer', layerId: null });
});

describe('activateLayer', () => {
  it('opens a session on the layer at the version it read', () => {
    open();
    const store = useEditStore.getState();

    expect(store.session?.activeLayerId).toBe('leases');
    expect(store.session?.baseVersion).toBe(7);
    expect(store.mode.activeLayerId).toBe('leases');
  });

  it('refuses to switch away from unsaved edits, naming the count', () => {
    // §4 ends the session on a layer switch and §5.2 requires the buffer to be
    // saved or discarded first. Discarding silently here would throw away the
    // user's work with no prompt; this store has no way to ask them.
    open();
    useEditStore.getState().applyCommand(moveCommand());

    expect(() =>
      useEditStore.getState().activateLayer({
        layerId: 'faults',
        baseVersion: 1,
        geometry: 'line',
        canEdit: true,
      }),
    ).toThrow(/1 unsaved edit/);
  });

  it('allows the switch once the buffer is clean', () => {
    open();
    useEditStore.getState().applyCommand(moveCommand());
    useEditStore.getState().undo();

    expect(() =>
      useEditStore.getState().activateLayer({
        layerId: 'faults',
        baseVersion: 1,
        geometry: 'line',
        canEdit: true,
      }),
    ).not.toThrow();
  });
});

describe('the revision counter', () => {
  it('advances on every session mutation', () => {
    // The session is mutated in place — zustand notices new identities, not
    // mutations, so without this nothing rendering from the dirty buffer ever
    // updates during a drag.
    open();
    const opened = useEditStore.getState().revision;

    useEditStore.getState().applyCommand(moveCommand());
    const applied = useEditStore.getState().revision;
    useEditStore.getState().undo();
    const undone = useEditStore.getState().revision;
    useEditStore.getState().redo();

    expect(applied).toBeGreaterThan(opened);
    expect(undone).toBeGreaterThan(applied);
    expect(useEditStore.getState().revision).toBeGreaterThan(undone);
  });

  it('stays put when there is nothing to undo', () => {
    // An undo that did nothing must not trigger a re-render of the map.
    open();
    const before = useEditStore.getState().revision;

    useEditStore.getState().undo();

    expect(useEditStore.getState().revision).toBe(before);
  });
});

describe('applyCommand', () => {
  it('refuses to edit with no session open', () => {
    expect(() => useEditStore.getState().applyCommand(moveCommand())).toThrow(
      /Make a layer active/,
    );
  });
});

describe('editState', () => {
  it('is idle before a layer is active', () => {
    const state = editState(useEditStore.getState());

    expect(state).toMatchObject({
      activeLayerId: null,
      canEdit: false,
      selection: 'none',
      dirty: false,
      canUndo: false,
      canRedo: false,
    });
  });

  it('reports the dirty buffer and both undo stacks', () => {
    open();
    useEditStore.getState().applyCommand(moveCommand());

    expect(editState(useEditStore.getState())).toMatchObject({
      dirty: true,
      canUndo: true,
      canRedo: false,
    });

    useEditStore.getState().undo();

    expect(editState(useEditStore.getState())).toMatchObject({
      dirty: false,
      canUndo: false,
      canRedo: true,
    });
  });

  it('derives the selection scope rather than storing it', () => {
    open();
    const store = useEditStore.getState();
    store.dispatch({ type: 'selectFeatures', ids: ['lease', 'other'] });

    expect(editState(useEditStore.getState()).selection).toBe('features');
    expect(editState(useEditStore.getState()).selectedFeatureCount).toBe(2);
  });

  it('carries the toolbar toggles the registry reads', () => {
    useEditStore.getState().toggle('topologicalEditing');
    useEditStore.getState().setSnap({ enabled: false });

    expect(editState(useEditStore.getState())).toMatchObject({
      topologicalEditing: true,
      snapEnabled: false,
    });
  });
});

describe('selectedVertices', () => {
  it('reads the handle ids back into vertex addresses', () => {
    open();
    const store = useEditStore.getState();
    store.dispatch({ type: 'setMode', mode: 'vertex' });
    store.dispatch({
      type: 'selectVertices',
      ids: [handleId({ featureId: 'lease', ring: 0, ordinal: 2 })],
    });

    expect(selectedVertices(useEditStore.getState())).toEqual([
      { featureId: 'lease', ring: 0, ordinal: 2 },
    ]);
  });

  it('drops an id that is not a handle instead of addressing a wrong vertex', () => {
    open();
    const store = useEditStore.getState();
    store.dispatch({ type: 'setMode', mode: 'vertex' });
    store.dispatch({ type: 'selectVertices', ids: ['nonsense'] });

    expect(selectedVertices(useEditStore.getState())).toEqual([]);
  });
});

describe('handleFeatures', () => {
  it('gives no handles outside a vertex mode', () => {
    // Handles in select mode invite a drag no mode supports.
    open();
    useEditStore.getState().dispatch({ type: 'selectFeatures', ids: ['lease'] });

    expect(handleFeatures(useEditStore.getState())).toEqual([]);
  });

  it('gives handles for the selected features in vertex mode', () => {
    open();
    const store = useEditStore.getState();
    store.dispatch({ type: 'selectFeatures', ids: ['lease'] });
    store.dispatch({ type: 'setMode', mode: 'vertex' });

    expect(handleFeatures(useEditStore.getState()).map((feature) => feature.id)).toEqual([
      'lease',
    ]);
  });

  it('reads the edited geometry, not the geometry the session started with', () => {
    // Handles on the original vertices after an edit would put a drag handle
    // where the vertex no longer is.
    open();
    const store = useEditStore.getState();
    store.dispatch({ type: 'selectFeatures', ids: ['lease'] });
    store.dispatch({ type: 'setMode', mode: 'vertex' });
    store.applyCommand(moveCommand());

    expect(handleFeatures(useEditStore.getState())[0]!.geometry).toEqual(MOVED.geometry);
  });

  it('skips a feature that has been deleted in this session', () => {
    open();
    const store = useEditStore.getState();
    store.dispatch({ type: 'selectFeatures', ids: ['lease'] });
    store.dispatch({ type: 'setMode', mode: 'vertex' });
    store.applyCommand({
      id: 'command-2',
      label: 'Delete Feature',
      deltas: [{ featureId: 'lease', before: SQUARE, after: null }],
      timestamp: 0,
    });

    expect(handleFeatures(useEditStore.getState())).toEqual([]);
  });
});
