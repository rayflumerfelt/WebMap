/**
 * Crash durability for the edit buffer. `09-editing.md` §5.4.
 *
 * "A geologist doing a forty-five minute boundary cleanup must not lose it to a
 * browser refresh." The buffer is mirrored to IndexedDB on every applied
 * operation, debounced, and offered back when a session opens on the same
 * layer.
 *
 * **The storage is behind an interface and the policy is not.** Which snapshot
 * is offered, when one is stale, and when a write is worth making are decided
 * here and tested against an in-memory store; IndexedDB is a thirty-line
 * adapter underneath. jsdom has no IndexedDB, so the alternative was a fake
 * database dependency and a set of rules nobody could assert.
 *
 * Three rules are worth stating because each is a way this feature goes wrong:
 *
 * **A recovered buffer is rebased onto what the server holds now, not onto
 * what it held when the tab closed.** `restoreSnapshot` takes a session whose
 * exact cache has already been loaded, and `isStale` says whether the version
 * moved — which the prompt has to mention, because recovering onto a layer
 * somebody else has since saved is how two people's work silently merges.
 *
 * **A clean session clears its snapshot.** Otherwise the next visit offers to
 * recover edits that were saved successfully, and a user who accepts gets a
 * duplicate of work already in the layer.
 *
 * **Nothing here throws.** Storage can be unavailable — a private window, a
 * browser with site data blocked, a quota that is full — and losing durability
 * is not a reason to lose the editing session with it. Failures are reported
 * once, through the same channel as any other refusal.
 */

import type { SessionSnapshot } from './session.js';

/** How long the buffer sits unwritten. §5.4 asks for about half a second: long
 *  enough that a drag writes once, short enough that a crash costs a gesture. */
export const DEBOUNCE_MS = 500;

/** Snapshots older than this are dropped rather than offered. A month-old
 *  buffer against a layer that has moved on is not a recovery, it is a
 *  surprise. */
export const MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;

export interface SnapshotStore {
  read(layerId: string): Promise<SessionSnapshot | null>;
  write(snapshot: SessionSnapshot): Promise<void>;
  clear(layerId: string): Promise<void>;
}

const DB_NAME = 'webmap-editing';
const STORE = 'sessions';
const DB_VERSION = 1;

/** IndexedDB, or `null` where there is none to have. */
export function indexedDbStore(factory: IDBFactory | undefined = globalThis.indexedDB):
  | SnapshotStore
  | null {
  if (!factory) return null;

  const open = () =>
    new Promise<IDBDatabase>((resolve, reject) => {
      const request = factory.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = () => {
        // Keyed on the layer id: one session per layer is §3.1's invariant, so
        // a second snapshot for the same layer is always the newer one.
        if (!request.result.objectStoreNames.contains(STORE)) {
          request.result.createObjectStore(STORE, { keyPath: 'activeLayerId' });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });

  const run = <T>(mode: IDBTransactionMode, act: (store: IDBObjectStore) => IDBRequest): Promise<T> =>
    open().then(
      (db) =>
        new Promise<T>((resolve, reject) => {
          const request = act(db.transaction(STORE, mode).objectStore(STORE));
          request.onsuccess = () => resolve(request.result as T);
          request.onerror = () => reject(request.error);
        }),
    );

  return {
    read: (layerId) => run<SessionSnapshot | null>('readonly', (store) => store.get(layerId)),
    write: (snapshot) => run<void>('readwrite', (store) => store.put(snapshot)),
    clear: (layerId) => run<void>('readwrite', (store) => store.delete(layerId)),
  };
}

/** An in-memory store: what tests use, and what a browser with no IndexedDB
 *  gets — durability within the tab is still better than none. */
export function memoryStore(): SnapshotStore {
  const snapshots = new Map<string, SessionSnapshot>();
  return {
    read: (layerId) => Promise.resolve(snapshots.get(layerId) ?? null),
    write: (snapshot) => {
      snapshots.set(snapshot.activeLayerId, snapshot);
      return Promise.resolve();
    },
    clear: (layerId) => {
      snapshots.delete(layerId);
      return Promise.resolve();
    },
  };
}

export interface DurabilityOptions {
  store: SnapshotStore;
  /** Reported once per failure kind. Storage being unavailable must not take
   *  the editing session down with it. */
  onError?: (message: string) => void;
  /** Injected so tests do not wait half a second. */
  schedule?: (callback: () => void, ms: number) => () => void;
  now?: () => number;
}

export interface Durability {
  /** Called after every applied operation. Writes on a debounce; a clean
   *  buffer clears instead. */
  record(snapshot: SessionSnapshot, isDirty: boolean): void;
  /** Write whatever is pending, now. For `beforeunload`, where a debounce is
   *  a promise nothing will keep. */
  flush(): void;
  /** The snapshot worth offering for this layer, or null. */
  recover(layerId: string): Promise<SessionSnapshot | null>;
  /** Forget this layer's snapshot — after a save, or after the user declines
   *  the offer. */
  forget(layerId: string): void;
}

const DEFAULT_SCHEDULE = (callback: () => void, ms: number) => {
  const handle = setTimeout(callback, ms);
  return () => clearTimeout(handle);
};

export function createDurability(options: DurabilityOptions): Durability {
  const { store } = options;
  const schedule = options.schedule ?? DEFAULT_SCHEDULE;
  const now = options.now ?? (() => Date.now());

  let pending: SessionSnapshot | null = null;
  let cancel: (() => void) | null = null;
  let reported = false;

  const report = (message: string) => {
    // Once. A failing store fails on every keystroke, and a message per
    // keystroke buries the map.
    if (reported) return;
    reported = true;
    options.onError?.(message);
  };

  const write = () => {
    cancel = null;
    const snapshot = pending;
    pending = null;
    if (!snapshot) return;
    void store.write(snapshot).catch(() =>
      report(
        'Edits are not being backed up: this browser refused local storage. ' +
          'They are still in the tab, and a refresh will lose them — save when ' +
          'you can.',
      ),
    );
  };

  return {
    record(snapshot, isDirty) {
      if (!isDirty) {
        // A saved or discarded session must not leave an offer behind, or the
        // next visit proposes recovering work that is already in the layer.
        cancel?.();
        cancel = null;
        pending = null;
        void store.clear(snapshot.activeLayerId).catch(() => undefined);
        return;
      }
      pending = snapshot;
      cancel?.();
      cancel = schedule(write, DEBOUNCE_MS);
    },

    flush() {
      cancel?.();
      write();
    },

    async recover(layerId) {
      try {
        const snapshot = await store.read(layerId);
        if (!snapshot) return null;
        if (snapshot.dirty.length === 0) return null;
        if (now() - snapshot.savedAt > MAX_AGE_MS) {
          // Old enough that offering it is a surprise rather than a rescue.
          void store.clear(layerId).catch(() => undefined);
          return null;
        }
        return snapshot;
      } catch {
        report('Saved edits could not be read back from this browser’s local storage.');
        return null;
      }
    },

    forget(layerId) {
      cancel?.();
      cancel = null;
      pending = null;
      void store.clear(layerId).catch(() => undefined);
    },
  };
}
