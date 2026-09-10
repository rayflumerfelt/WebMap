/**
 * The edit session: dirty buffer, undo stack, Save and Discard.
 * `09-editing.md` §3.3, §3.4, §5.
 *
 * **This exists before any mutating operation**, because every operation lands
 * here and an operation written against a buffer that does not exist yet will
 * invent its own.
 *
 * Three ideas, and confusing them is what `09` §3.3 calls the primary source of
 * bugs in this subsystem:
 *
 * - **Exact geometry** is what the backend holds. Every committed coordinate
 *   comes from here.
 * - **Dirty geometry** is the local buffer. It is what renders over the tiles
 *   and what the undo stack walks.
 * - **Tile geometry** — simplified, clipped — is for hover and hit testing and
 *   is *never* committed. This module never sees it, which is the cheapest way
 *   to keep that true.
 *
 * The other structural decision is that **a command carries a set of deltas**.
 * Topological editing mutates N features in one gesture (`09` §7), and undo has
 * to treat that atomically: one drag of a shared boundary is one entry, not
 * three, or two of the three come back and the layer has a gap.
 */

export type FeatureId = string;

/** A feature as this module handles it. Geometry is opaque here on purpose:
 *  the session moves features around and never inspects a coordinate. */
export interface Feature {
  id: FeatureId;
  geometry: unknown;
  properties: Record<string, unknown>;
}

/** One feature's change. `before: null` is a creation, `after: null` a delete. */
export interface FeatureDelta {
  featureId: FeatureId;
  before: Feature | null;
  after: Feature | null;
}

export interface Command {
  id: string;
  /** Shown in the undo tooltip — "Move Vertex", "Dissolve 3 features". */
  label: string;
  deltas: FeatureDelta[];
  timestamp: number;
}

export interface EditSession {
  activeLayerId: string;
  /** Exact geometry, as read from the backend. Never written by an edit. */
  exactCache: Map<FeatureId, Feature>;
  /** Pending edits. `null` is a pending delete, which is why the value type
   *  admits it — dropping the key instead would make a deleted feature
   *  indistinguishable from an untouched one. */
  dirty: Map<FeatureId, Feature | null>;
  undoStack: Command[];
  redoStack: Command[];
  /** The dataset version read at session start. `09` §5.3 — optimistic
   *  concurrency lives on this pointer and only here. */
  baseVersion: number;
}

/** How many undo entries a session keeps. */
export const UNDO_LIMIT = 200;

export function openSession(
  activeLayerId: string,
  baseVersion: number,
  features: Iterable<Feature> = [],
): EditSession {
  return {
    activeLayerId,
    exactCache: new Map([...features].map((feature) => [feature.id, feature])),
    dirty: new Map(),
    undoStack: [],
    redoStack: [],
    baseVersion,
  };
}

export function isDirty(session: EditSession): boolean {
  return session.dirty.size > 0;
}

/**
 * The feature as it currently stands: the dirty version if there is one, else
 * the exact one.
 *
 * The single reader every operation uses. An operation that read `exactCache`
 * directly would compute its change against the state before the last edit,
 * which produces a second edit that silently undoes the first.
 */
export function current(session: EditSession, id: FeatureId): Feature | null {
  if (session.dirty.has(id)) return session.dirty.get(id) ?? null;
  return session.exactCache.get(id) ?? null;
}

/**
 * Apply a command: write every `after` into the dirty buffer, push undo.
 *
 * **Redo is cleared.** `09` §3.4: replaying forward from a state the server
 * never accepted produces a layer nobody authored.
 *
 * Mutates the session rather than returning a new one. The buffer holds a whole
 * layer's pending edits and a vertex drag produces one command per pointer
 * move; copying the map each time is the difference between a drag that tracks
 * the cursor and one that does not.
 */
export function apply(session: EditSession, command: Command): void {
  if (command.deltas.length === 0) {
    // A command with no deltas is a no-op that would still consume an undo
    // press — the user hits Ctrl+Z, nothing changes, and they hit it again.
    throw new Error(
      `The command "${command.label}" carries no feature changes. A command ` +
        `that changes nothing would still take an undo press and appear to do ` +
        `nothing when it was used.`,
    );
  }

  for (const delta of command.deltas) {
    session.dirty.set(delta.featureId, delta.after);
  }
  session.undoStack.push(command);
  if (session.undoStack.length > UNDO_LIMIT) session.undoStack.shift();
  session.redoStack.length = 0;
}

/** Undo the last command, writing every `before` back. */
export function undo(session: EditSession): Command | null {
  const command = session.undoStack.pop();
  if (!command) return null;

  for (const delta of command.deltas) {
    restore(session, delta.featureId, delta.before);
  }
  session.redoStack.push(command);
  return command;
}

export function redo(session: EditSession): Command | null {
  const command = session.redoStack.pop();
  if (!command) return null;

  for (const delta of command.deltas) {
    session.dirty.set(delta.featureId, delta.after);
  }
  session.undoStack.push(command);
  return command;
}

/**
 * Put a feature back to `before`, and **drop it from the buffer when that
 * equals what the backend holds**.
 *
 * The part that is easy to miss: undoing back to the start has to leave the
 * session *clean*, or Save stays enabled with nothing to send and the tab warns
 * about unsaved changes that no longer exist. Comparing against `exactCache` is
 * how "unchanged" is decided, rather than counting operations — a move-and-move-
 * back is two commands and zero changes.
 */
function restore(session: EditSession, id: FeatureId, before: Feature | null): void {
  const original = session.exactCache.get(id) ?? null;
  if (before === null && original === null) {
    session.dirty.delete(id);
    return;
  }
  if (before !== null && original !== null && same(before, original)) {
    session.dirty.delete(id);
    return;
  }
  session.dirty.set(id, before);
}

/**
 * Whether two features are the same for buffer purposes.
 *
 * Structural, by JSON. Geometry here is opaque and may be any shape a reader
 * produced; a deep-equality helper written for this would be a second
 * implementation of something the platform already does correctly, and the
 * features are small enough that the cost is not the thing to optimise.
 */
function same(a: Feature, b: Feature): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

/**
 * The deltas to send to `POST /api/v1/layers/{id}/save`.
 *
 * Derived from the buffer rather than accumulated as edits happen: a feature
 * moved four times is **one** delta from its original, not four, and the server
 * writes a whole immutable object either way (`adr/0005`).
 */
export function pendingDeltas(session: EditSession): FeatureDelta[] {
  const deltas: FeatureDelta[] = [];
  for (const [featureId, after] of session.dirty) {
    deltas.push({
      featureId,
      before: session.exactCache.get(featureId) ?? null,
      after,
    });
  }
  return deltas;
}

/**
 * Accept a successful save: the dirty features become the exact ones, the
 * buffer empties, and the version pointer moves.
 *
 * **Both stacks are cleared.** After a commit there is nothing local to undo —
 * the previous state is a version on the server, and undoing into it would
 * produce a local buffer that disagrees with a `baseVersion` the session has
 * already advanced past.
 */
export function commit(session: EditSession, newVersion: number): void {
  for (const [featureId, feature] of session.dirty) {
    if (feature === null) session.exactCache.delete(featureId);
    else session.exactCache.set(featureId, feature);
  }
  session.dirty.clear();
  session.undoStack.length = 0;
  session.redoStack.length = 0;
  session.baseVersion = newVersion;
}

/** `09` §5.2: Discard clears the buffer and both stacks. Confirmation is the
 *  caller's business — this is the part that cannot be undone. */
export function discard(session: EditSession): void {
  session.dirty.clear();
  session.undoStack.length = 0;
  session.redoStack.length = 0;
}

/**
 * A 409 from the save endpoint names the features that changed underneath
 * (`09` §5.3, `adr/0005` amended twice). This is the **Refresh** half of the
 * answer: drop local changes to those features, keep everything else, and adopt
 * the server's version.
 *
 * The alternative offered to the user is **Force**, which is a re-save with the
 * new version and no other change — so it needs nothing here.
 *
 * Redo is cleared, and the undo stack with it. A stack whose entries reference
 * features that have just been replaced underneath would restore a `before`
 * that is no longer anybody's state.
 */
export function rebase(
  session: EditSession,
  changed: FeatureId[],
  serverFeatures: Iterable<Feature>,
  newVersion: number,
): void {
  for (const feature of serverFeatures) {
    session.exactCache.set(feature.id, feature);
  }
  for (const featureId of changed) {
    session.dirty.delete(featureId);
  }
  session.undoStack.length = 0;
  session.redoStack.length = 0;
  session.baseVersion = newVersion;
}

/**
 * What crosses into IndexedDB. `09` §5.4.
 *
 * A geologist doing a forty-five minute boundary cleanup must not lose it to a
 * browser refresh, and this is what makes that cheap: plain data, no Maps, so
 * `structuredClone` and JSON both handle it.
 *
 * The **exact cache is not included**. It is a copy of what the server holds
 * and can be fetched again; persisting it would multiply the stored size by the
 * size of the layer, for data that is authoritative somewhere else anyway.
 */
export interface SessionSnapshot {
  activeLayerId: string;
  baseVersion: number;
  dirty: Array<[FeatureId, Feature | null]>;
  undoStack: Command[];
  redoStack: Command[];
  savedAt: number;
}

export function snapshot(session: EditSession): SessionSnapshot {
  return {
    activeLayerId: session.activeLayerId,
    baseVersion: session.baseVersion,
    dirty: [...session.dirty],
    undoStack: session.undoStack,
    redoStack: session.redoStack,
    savedAt: Date.now(),
  };
}

/**
 * Restore a snapshot onto a freshly loaded session.
 *
 * **The exact cache comes from the server, not from the snapshot**, which is
 * why this takes a session that has already loaded one. A recovered buffer is
 * rebased onto whatever the layer holds now; if the version moved while the tab
 * was closed, the caller has a version mismatch to resolve rather than a
 * silently stale baseline.
 */
export function restoreSnapshot(session: EditSession, saved: SessionSnapshot): void {
  session.dirty = new Map(saved.dirty);
  session.undoStack = [...saved.undoStack];
  session.redoStack = [...saved.redoStack];
}

/** True when a recovered snapshot was taken against a version the layer has
 *  since moved past — the case `09` §5.4's recovery prompt has to mention. */
export function isStale(session: EditSession, saved: SessionSnapshot): boolean {
  return saved.baseVersion !== session.baseVersion;
}
