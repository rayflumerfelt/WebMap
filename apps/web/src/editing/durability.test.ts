/**
 * Crash durability. `09-editing.md` §5.4.
 *
 * The policy is what these test: when a write is worth making, which snapshot
 * is worth offering back, and what happens when the browser refuses storage.
 * IndexedDB itself is a thin adapter under the same interface.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

import { MAX_AGE_MS, createDurability, memoryStore } from './durability.js';
import type { SnapshotStore } from './durability.js';
import type { SessionSnapshot } from './session.js';

function snapshotOf(overrides: Partial<SessionSnapshot> = {}): SessionSnapshot {
  return {
    activeLayerId: 'leases',
    baseVersion: 7,
    dirty: [
      ['42', { id: '42', geometry: { type: 'Point', coordinates: [1, 2] }, properties: {} }],
    ],
    undoStack: [],
    redoStack: [],
    savedAt: 1_000,
    ...overrides,
  };
}

/** Runs the debounce when a test says so, rather than after half a second. */
function manualSchedule() {
  const queue: Array<() => void> = [];
  return {
    schedule: (callback: () => void) => {
      queue.push(callback);
      return () => {
        const index = queue.indexOf(callback);
        if (index >= 0) queue.splice(index, 1);
      };
    },
    run: () => {
      const pending = [...queue];
      queue.length = 0;
      for (const callback of pending) callback();
    },
    get length() {
      return queue.length;
    },
  };
}

let store: SnapshotStore;

beforeEach(() => {
  store = memoryStore();
});

describe('recording', () => {
  it('writes once for a burst of operations', () => {
    // A vertex drag applies a command per pointer move (§13). One write per
    // move is the cost the debounce exists to remove.
    const clock = manualSchedule();
    const durability = createDurability({ store, schedule: clock.schedule });
    const write = vi.spyOn(store, 'write');

    durability.record(snapshotOf(), true);
    durability.record(snapshotOf(), true);
    durability.record(snapshotOf(), true);
    clock.run();

    expect(write).toHaveBeenCalledTimes(1);
  });

  it('writes the latest state, not the first', () => {
    const clock = manualSchedule();
    const durability = createDurability({ store, schedule: clock.schedule });

    durability.record(snapshotOf({ baseVersion: 7 }), true);
    durability.record(snapshotOf({ baseVersion: 8 }), true);
    clock.run();

    return expect(store.read('leases')).resolves.toMatchObject({ baseVersion: 8 });
  });

  it('clears the snapshot when the buffer goes clean', async () => {
    // After a save. Otherwise the next visit offers to recover work that is
    // already in the layer, and accepting duplicates it.
    const clock = manualSchedule();
    const durability = createDurability({ store, schedule: clock.schedule });

    durability.record(snapshotOf(), true);
    clock.run();
    durability.record(snapshotOf({ dirty: [] }), false);

    await expect(store.read('leases')).resolves.toBeNull();
  });

  it('cancels a pending write when the buffer goes clean', async () => {
    // The race a debounce creates: a save that lands between the last edit and
    // the timer must not be followed by a write of the edits it saved.
    const clock = manualSchedule();
    const durability = createDurability({ store, schedule: clock.schedule });

    durability.record(snapshotOf(), true);
    durability.record(snapshotOf({ dirty: [] }), false);
    clock.run();

    await expect(store.read('leases')).resolves.toBeNull();
  });

  it('flushes on demand, for beforeunload', async () => {
    // A debounce is a promise nothing will keep once the tab is closing.
    const clock = manualSchedule();
    const durability = createDurability({ store, schedule: clock.schedule });

    durability.record(snapshotOf(), true);
    durability.flush();

    await expect(store.read('leases')).resolves.not.toBeNull();
  });
});

describe('recovery', () => {
  it('offers a snapshot with edits in it', async () => {
    const durability = createDurability({ store, now: () => 1_000 });
    await store.write(snapshotOf());

    await expect(durability.recover('leases')).resolves.toMatchObject({ baseVersion: 7 });
  });

  it('offers nothing for a layer with no snapshot', async () => {
    const durability = createDurability({ store });

    await expect(durability.recover('leases')).resolves.toBeNull();
  });

  it('offers nothing for an empty buffer', async () => {
    // A snapshot with no dirty features is not a recovery; it is a prompt the
    // user has to dismiss for nothing.
    const durability = createDurability({ store });
    await store.write(snapshotOf({ dirty: [] }));

    await expect(durability.recover('leases')).resolves.toBeNull();
  });

  it('drops a snapshot older than the retention window', async () => {
    // A month-old buffer against a layer that has moved on is a surprise
    // rather than a rescue.
    const durability = createDurability({ store, now: () => 1_000 + MAX_AGE_MS + 1 });
    await store.write(snapshotOf());

    await expect(durability.recover('leases')).resolves.toBeNull();
    await expect(store.read('leases')).resolves.toBeNull();
  });

  it('forgets a layer on request', async () => {
    const durability = createDurability({ store });
    await store.write(snapshotOf());

    durability.forget('leases');

    await expect(store.read('leases')).resolves.toBeNull();
  });
});

describe('when the browser refuses storage', () => {
  const broken: SnapshotStore = {
    read: () => Promise.reject(new Error('blocked')),
    write: () => Promise.reject(new Error('blocked')),
    clear: () => Promise.reject(new Error('blocked')),
  };

  it('reports once rather than per keystroke', async () => {
    // A failing store fails on every edit, and a message per edit buries the
    // map under its own warning.
    const clock = manualSchedule();
    const onError = vi.fn();
    const durability = createDurability({ store: broken, schedule: clock.schedule, onError });

    durability.record(snapshotOf(), true);
    clock.run();
    await Promise.resolve();
    durability.record(snapshotOf(), true);
    clock.run();
    await Promise.resolve();

    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError.mock.calls[0]![0]).toMatch(/still in the tab/);
  });

  it('recovers nothing rather than throwing', async () => {
    // Losing durability must not take the editing session with it.
    const durability = createDurability({ store: broken });

    await expect(durability.recover('leases')).resolves.toBeNull();
  });
});
