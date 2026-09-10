/**
 * The edit session, as React sees it. `09-editing.md` §3, §4, §8.
 *
 * Everything the editing subsystem needs is already written as pure functions:
 * `modes.ts` reduces the mode machine, `session.ts` keeps the dirty buffer and
 * the undo stacks, `snapPipeline.ts` holds the snap settings. This store is the
 * one place they are held together, and it exists for a single reason — the
 * command registry (§8) reads a **flat snapshot**, and something has to build
 * that snapshot from four independent pieces without letting them disagree.
 *
 * **The session is mutated in place and signalled with a counter.** `session.ts`
 * mutates because a vertex drag produces one command per pointer move and
 * copying a whole layer's dirty buffer at that rate is the difference between a
 * drag that tracks the cursor and one that does not. Zustand notices new object
 * identities, not mutations, so `revision` is bumped on every session change and
 * anything that renders from the session subscribes to that. It is bookkeeping,
 * and it is cheaper than the alternative by two orders of magnitude.
 *
 * **The mode machine stays a reducer.** Actions here dispatch into `reduce`
 * rather than reimplementing its rules: which selection survives a mode change
 * is decided in one place, tested in one place, and §4 is emphatic that the
 * transitions are the content of that machine.
 */

import { create } from 'zustand';

import { parseHandleId } from '../editing/overlay.js';
import type { SelectedVertex } from '../editing/overlay.js';
import { INITIAL, isVertexMode, reduce, selectionScope } from '../editing/modes.js';
import type { ModeEvent, ModeState } from '../editing/modes.js';
import {
  apply,
  current,
  isDirty,
  openSession,
  redo as redoSession,
  undo as undoSession,
} from '../editing/session.js';
import type { Command, EditSession, Feature } from '../editing/session.js';
import { DEFAULT_SNAP_SETTINGS } from '../editing/snapPipeline.js';
import type { SnapSettings } from '../editing/snapPipeline.js';
import type { EditState, GeometryKind } from '../editing/types.js';

/** What opening a session on a layer needs to know. */
export interface LayerContext {
  layerId: string;
  /** The dataset version read at session start. §5.3: optimistic concurrency
   *  lives on this pointer and only here. */
  baseVersion: number;
  geometry: GeometryKind | null;
  /** Editable in the permission sense. A viewer gets the tools greyed rather
   *  than absent, which says more. */
  canEdit: boolean;
  /** Exact geometry for the working set (§17), if it has been fetched. */
  features?: readonly Feature[];
}

export interface EditStoreState {
  session: EditSession | null;
  /** Bumped on every in-place mutation of `session`. Subscribe to this, not to
   *  `session`, when rendering anything derived from the dirty buffer. */
  revision: number;
  mode: ModeState;
  snap: SnapSettings;
  activeLayerGeometry: GeometryKind | null;
  canEdit: boolean;

  topologicalEditing: boolean;
  angleConstraint: boolean;
  showVertices: boolean;
  showMeasurements: boolean;
  showValidationErrors: boolean;
  clipboardCount: number;

  /** Open a session on a layer. Refuses while edits are pending. */
  activateLayer(context: LayerContext): void;
  /** Close without saving. The caller has already decided; §5.2 makes that
   *  decision the UI's, not this store's. */
  closeSession(): void;
  applyCommand(command: Command): void;
  undo(): void;
  redo(): void;
  dispatch(event: ModeEvent): void;
  setSnap(patch: Partial<SnapSettings>): void;
  toggle(flag: EditFlag): void;
  setClipboardCount(count: number): void;
}

export type EditFlag =
  | 'topologicalEditing'
  | 'angleConstraint'
  | 'showVertices'
  | 'showMeasurements'
  | 'showValidationErrors';

export const useEditStore = create<EditStoreState>()((set, get) => ({
  session: null,
  revision: 0,
  mode: INITIAL,
  snap: DEFAULT_SNAP_SETTINGS,
  activeLayerGeometry: null,
  canEdit: false,
  topologicalEditing: false,
  angleConstraint: false,
  showVertices: true,
  showMeasurements: false,
  showValidationErrors: true,
  clipboardCount: 0,

  activateLayer(context) {
    const existing = get().session;
    if (existing && isDirty(existing)) {
      // §4: switching the active layer ends the session, and §5.2 requires the
      // buffer to be saved or discarded first. Throwing rather than discarding
      // silently — the edits are the user's work, and this store has no way to
      // ask them.
      throw new Error(
        `There are ${existing.dirty.size} unsaved edit(s) on layer ` +
          `'${existing.activeLayerId}'. Save or discard them before making ` +
          `'${context.layerId}' the active layer.`,
      );
    }

    set((state) => ({
      session: openSession(context.layerId, context.baseVersion, context.features ?? []),
      revision: state.revision + 1,
      mode: reduce(state.mode, { type: 'setActiveLayer', layerId: context.layerId }),
      activeLayerGeometry: context.geometry,
      canEdit: context.canEdit,
    }));
  },

  closeSession() {
    set((state) => ({
      session: null,
      revision: state.revision + 1,
      mode: reduce(state.mode, { type: 'setActiveLayer', layerId: null }),
      activeLayerGeometry: null,
      canEdit: false,
    }));
  },

  applyCommand(command) {
    const session = get().session;
    if (!session) {
      throw new Error(
        `The command "${command.label}" was run with no edit session open. ` +
          `Make a layer active before editing.`,
      );
    }
    apply(session, command);
    set((state) => ({ revision: state.revision + 1 }));
  },

  undo() {
    const session = get().session;
    if (!session || !undoSession(session)) return;
    set((state) => ({ revision: state.revision + 1 }));
  },

  redo() {
    const session = get().session;
    if (!session || !redoSession(session)) return;
    set((state) => ({ revision: state.revision + 1 }));
  },

  dispatch(event) {
    set((state) => ({ mode: reduce(state.mode, event) }));
  },

  setSnap(patch) {
    set((state) => ({ snap: { ...state.snap, ...patch } }));
  },

  toggle(flag) {
    set((state) => ({ [flag]: !state[flag] }) as Pick<EditStoreState, EditFlag>);
  },

  setClipboardCount(count) {
    set({ clipboardCount: count });
  },
}));

/**
 * The flat snapshot every command predicate reads. `09` §8.
 *
 * Derived on demand rather than stored, for the reason `selectionScope` is
 * derived: two fields that could disagree about what is selected is the shape
 * that produces a Delete which deletes the wrong thing.
 */
export function editState(store: EditStoreState): EditState {
  const session = store.session;
  return {
    activeLayerId: store.mode.activeLayerId,
    activeLayerGeometry: store.activeLayerGeometry,
    canEdit: store.canEdit,
    selection: selectionScope(store.mode),
    selectedFeatureCount: store.mode.selectedFeatureIds.length,
    selectedVertexCount: store.mode.selectedVertexIds.length,
    operationActive: store.mode.operationActive,
    dirty: session ? isDirty(session) : false,
    canUndo: (session?.undoStack.length ?? 0) > 0,
    canRedo: (session?.redoStack.length ?? 0) > 0,
    clipboardCount: store.clipboardCount,
    snapEnabled: store.snap.enabled,
    topologicalEditing: store.topologicalEditing,
    angleConstraint: store.angleConstraint,
    showVertices: store.showVertices,
    showMeasurements: store.showMeasurements,
    showValidationErrors: store.showValidationErrors,
    selectMode: store.mode.selectTool,
  };
}

/** The selected vertices, as the overlay addresses them. */
export function selectedVertices(store: EditStoreState): SelectedVertex[] {
  const vertices: SelectedVertex[] = [];
  for (const id of store.mode.selectedVertexIds) {
    const vertex = parseHandleId(id);
    if (vertex) vertices.push(vertex);
  }
  return vertices;
}

/**
 * The features that get vertex handles: the selection, in a vertex mode only.
 *
 * Handles outside a vertex mode invite a drag no mode supports, and handles on
 * an unselected feature invite one on geometry the user has not chosen. Read
 * through `current`, so a feature that has already been edited shows handles
 * on its *edited* vertices rather than on the ones it started with.
 */
export function handleFeatures(store: EditStoreState): Feature[] {
  const session = store.session;
  if (!session || !isVertexMode(store.mode.mode)) return [];

  const features: Feature[] = [];
  for (const id of store.mode.selectedFeatureIds) {
    const feature = current(session, id);
    if (feature) features.push(feature);
  }
  return features;
}
