/**
 * Session store and autosave. `07-frontend.md` §3.
 *
 * The Phase 2 criterion behind most of this is "a session saved in the browser
 * reloads with identical appearance", and its neighbour "layer reorder,
 * visibility, and opacity persist across reload". Both are round-trip
 * properties, so they are tested as round trips rather than as field-by-field
 * assertions that would pass while the ordering was wrong.
 */

import type { Symbology } from '@webmap/style-model';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AUTOSAVE_DELAY_MS, startAutosave } from './autosave.js';
import { snapshotOf, useSessionStore } from './sessionStore.js';
import type { SessionLayer, SessionState } from './sessionStore.js';

const LINE: Symbology = {
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

function layer(id: string, overrides: Partial<SessionLayer> = {}): SessionLayer {
  return {
    id,
    datasetId: `ds-${id}`,
    name: id.toUpperCase(),
    symbology: LINE,
    opacity: 1,
    visible: true,
    ...overrides,
  };
}

const MIDLAND = { center: [-102.08, 31.99] as [number, number], zoom: 9.5 };

beforeEach(() => {
  useSessionStore.setState({
    sessionId: null,
    shortCode: null,
    layers: [],
    view: { center: [0, 0], zoom: 2 },
    selectedLayerId: null,
    dirty: false,
    conflict: false,
    lastSavedAt: null,
  });
});

// --- round trip -------------------------------------------------------------

describe('round trip', () => {
  it('reloads to an identical snapshot', () => {
    // The Phase 2 criterion: "a session saved in the browser reloads with
    // identical appearance." Asserted as a round trip, because a field-by-
    // field check passes while the ordering is wrong — and ordering is
    // appearance.
    const store = useSessionStore.getState();
    store.load({
      id: 's1',
      short_code: 'k3n8fq',
      layers: [layer('a'), layer('b', { opacity: 0.4, visible: false })],
      view: MIDLAND,
    });
    const saved = snapshotOf(useSessionStore.getState());

    useSessionStore.getState().load({
      id: 's1',
      short_code: 'k3n8fq',
      layers: saved.layers.map((entry, index) => ({
        id: `restored-${index}`,
        datasetId: entry.dataset_id,
        name: entry.dataset_id,
        symbology: entry.symbology_override!,
        opacity: entry.opacity,
        visible: entry.visible,
      })),
      view: saved.view,
    });

    expect(snapshotOf(useSessionStore.getState())).toEqual(saved);
  });

  it('derives z from list position rather than storing it twice', () => {
    // The store keeps draw order as list order, which is what the layer tree
    // drags around. Maintaining a separate z would give two sources of truth
    // for the same thing and one of them would be stale.
    useSessionStore.getState().load({
      id: 's1',
      short_code: 'k',
      layers: [layer('a'), layer('b'), layer('c')],
      view: MIDLAND,
    });
    useSessionStore.getState().reorderLayers(2, 0);

    const snapshot = snapshotOf(useSessionStore.getState());

    expect(snapshot.layers.map((l) => l.dataset_id)).toEqual(['ds-c', 'ds-a', 'ds-b']);
    expect(snapshot.layers.map((l) => l.z)).toEqual([0, 1, 2]);
  });

  it('preserves reorder, visibility and opacity together', () => {
    // The second criterion, in one pass: all three survive a snapshot.
    useSessionStore.getState().load({
      id: 's1',
      short_code: 'k',
      layers: [layer('a'), layer('b')],
      view: MIDLAND,
    });
    const store = useSessionStore.getState();
    store.reorderLayers(1, 0);
    store.toggleVisibility('a');
    store.setOpacity('b', 0.25);

    const snapshot = snapshotOf(useSessionStore.getState());

    expect(snapshot.layers[0]).toMatchObject({ dataset_id: 'ds-b', opacity: 0.25, z: 0 });
    expect(snapshot.layers[1]).toMatchObject({ dataset_id: 'ds-a', visible: false, z: 1 });
  });
});

// --- dirty tracking ---------------------------------------------------------

describe('dirty tracking', () => {
  it('is clean immediately after load', () => {
    // Marking a load dirty would make every session open write itself back,
    // turning a read into a write and making `updated_at` meaningless.
    useSessionStore.getState().load({
      id: 's1',
      short_code: 'k',
      layers: [layer('a')],
      view: MIDLAND,
    });

    expect(useSessionStore.getState().dirty).toBe(false);
  });

  it.each([
    ['reorder', (s: SessionState) => s.reorderLayers(1, 0)],
    ['visibility', (s: SessionState) => s.toggleVisibility('a')],
    ['opacity', (s: SessionState) => s.setOpacity('a', 0.5)],
    ['symbology', (s: SessionState) => s.updateSymbology('a', LINE)],
    ['view', (s: SessionState) => s.setView({ center: [-101, 32], zoom: 11 })],
    ['remove', (s: SessionState) => s.removeLayer('a')],
  ])('marks dirty on %s', (_name, mutate) => {
    useSessionStore.getState().load({
      id: 's1',
      short_code: 'k',
      layers: [layer('a'), layer('b')],
      view: MIDLAND,
    });

    mutate(useSessionStore.getState());

    expect(useSessionStore.getState().dirty).toBe(true);
  });

  it('does not mark dirty on selection', () => {
    // Selection is ephemeral, not session state. Two people opening the same
    // link should not fight over which layer is highlighted.
    useSessionStore.getState().load({
      id: 's1',
      short_code: 'k',
      layers: [layer('a'), layer('b')],
      view: MIDLAND,
    });

    useSessionStore.getState().select('b');

    expect(useSessionStore.getState().dirty).toBe(false);
    expect(snapshotOf(useSessionStore.getState())).not.toHaveProperty('selectedLayerId');
  });

  it('clamps opacity rather than storing a value the compiler will refuse', () => {
    useSessionStore.getState().load({
      id: 's1',
      short_code: 'k',
      layers: [layer('a')],
      view: MIDLAND,
    });

    useSessionStore.getState().setOpacity('a', 60);

    expect(useSessionStore.getState().layers[0]!.opacity).toBe(1);
  });

  it('is a no-op when a reorder does not move anything', () => {
    useSessionStore.getState().load({
      id: 's1',
      short_code: 'k',
      layers: [layer('a'), layer('b')],
      view: MIDLAND,
    });
    const before = useSessionStore.getState().layers;

    useSessionStore.getState().reorderLayers(1, 1);

    expect(useSessionStore.getState().layers).toBe(before);
  });
});

// --- autosave ---------------------------------------------------------------

describe('autosave', () => {
  interface Fake {
    dirty: boolean;
    value: number;
  }

  function harness(saveImpl?: (state: Fake) => Promise<void>) {
    const listeners = new Set<(state: Fake) => void>();
    let state: Fake = { dirty: false, value: 0 };
    const timers: Array<{ fn: () => void; id: number }> = [];
    let nextId = 1;

    const save = vi.fn(saveImpl ?? (() => Promise.resolve()));
    const onSaved = vi.fn();
    const onConflict = vi.fn();
    const onError = vi.fn();

    const handle = startAutosave<Fake>({
      subscribe: (listener) => {
        listeners.add(listener);
        return () => listeners.delete(listener);
      },
      getState: () => state,
      save,
      isDirty: (s) => s.dirty,
      onSaved,
      onConflict,
      onError,
      setTimeout: (fn) => {
        const id = nextId++;
        timers.push({ fn, id });
        return id;
      },
      clearTimeout: (id) => {
        const index = timers.findIndex((timer) => timer.id === id);
        if (index >= 0) timers.splice(index, 1);
      },
    });

    return {
      handle,
      save,
      onSaved,
      onConflict,
      onError,
      edit(value: number) {
        state = { dirty: true, value };
        for (const listener of listeners) listener(state);
      },
      settle(value: number) {
        state = { dirty: false, value };
      },
      pendingTimers: () => timers.length,
      fire() {
        const timer = timers.shift();
        timer?.fn();
      },
    };
  }

  it('saves on the trailing edge, not the leading one', async () => {
    // The value worth saving is where a drag ended. A leading-edge save writes
    // the first frame of a pan and then has to write again anyway.
    const h = harness();

    h.edit(1);
    h.edit(2);
    h.edit(3);

    expect(h.save).not.toHaveBeenCalled();
    h.fire();
    await Promise.resolve();
    expect(h.save).toHaveBeenCalledOnce();
    expect(h.save).toHaveBeenCalledWith({ dirty: true, value: 3 });
  });

  it('debounces at the documented delay', () => {
    expect(AUTOSAVE_DELAY_MS).toBe(2_000);
  });

  it('collapses a burst into a single pending save', () => {
    const h = harness();

    h.edit(1);
    h.edit(2);
    h.edit(3);

    expect(h.pendingTimers()).toBe(1);
  });

  it('does not save a clean state', async () => {
    const h = harness();
    h.edit(1);
    h.settle(1);

    h.fire();
    await Promise.resolve();

    expect(h.save).not.toHaveBeenCalled();
  });

  it('keeps one save in flight and follows it with exactly one more', async () => {
    // Overlapping PATCHes to the same session race, and the loser's
    // expected_updated_at is stale — so a burst of edits would produce a 409
    // for changes the user made themselves.
    let release: (() => void) | undefined;
    const h = harness(() => new Promise<void>((resolve) => (release = resolve)));

    h.edit(1);
    h.fire();
    await Promise.resolve();
    h.edit(2);
    h.fire();
    await Promise.resolve();

    expect(h.save).toHaveBeenCalledOnce();
    release!();
    await new Promise((resolve) => globalThis.setTimeout(resolve, 0));
    expect(h.save).toHaveBeenCalledTimes(2);
  });

  it('stops after a conflict rather than retrying forever', async () => {
    // **Retrying a 409 with the same stale timestamp fails identically,
    // forever, several times a minute.** The user has to be told instead.
    const h = harness(() => Promise.reject(Object.assign(new Error('conflict'), { status: 409 })));

    h.edit(1);
    h.fire();
    await new Promise((resolve) => globalThis.setTimeout(resolve, 0));
    h.edit(2);

    expect(h.onConflict).toHaveBeenCalledOnce();
    expect(h.pendingTimers()).toBe(0);
  });

  it('keeps trying after a transient failure', async () => {
    // A dropped packet should not end autosave for the session.
    const h = harness(() => Promise.reject(Object.assign(new Error('offline'), { status: 503 })));

    h.edit(1);
    h.fire();
    await new Promise((resolve) => globalThis.setTimeout(resolve, 0));
    h.edit(2);

    expect(h.onError).toHaveBeenCalled();
    expect(h.onConflict).not.toHaveBeenCalled();
    expect(h.pendingTimers()).toBe(1);
  });

  it('flush saves immediately and cancels the pending timer', async () => {
    // What mod+s and the unload handler use. Waiting two seconds while the tab
    // closes loses the edit.
    const h = harness();
    h.edit(1);

    await h.handle.flush();

    expect(h.save).toHaveBeenCalledOnce();
    expect(h.pendingTimers()).toBe(0);
  });

  it('stops saving once stopped', async () => {
    const h = harness();
    h.handle.stop();

    h.edit(1);
    await h.handle.flush();

    expect(h.save).not.toHaveBeenCalled();
  });
});
