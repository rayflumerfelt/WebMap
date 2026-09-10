/**
 * The editing state a command reads, and the shape of a command.
 * `09-editing.md` §8.
 *
 * **Every command is defined exactly once.** The menu bar, command palette,
 * toolbar and context menu all render from one registry, and this file is the
 * contract between them. `09` §8 calls it the highest-leverage structural
 * decision in the editing UI, and the reason is retrofitting: without it, every
 * command's enabled-state logic exists in four places and they drift.
 *
 * `EditState` is deliberately a **plain snapshot**, not the store. A command's
 * `enabled` runs on every render of every surface — four surfaces, a hundred
 * commands — so it has to be cheap and pure, and a predicate that could reach
 * into a store could subscribe, allocate, or read something that changed
 * mid-render.
 */

import type { ReactNode } from 'react';

/** What is selected, which decides most of what is possible. */
export type SelectionScope = 'none' | 'feature' | 'features' | 'vertex' | 'vertices';

/** Where a context menu was opened. `09` §8. */
export type ContextScope = 'feature' | 'vertex' | 'edge' | 'empty' | 'selection';

/** The menu the command belongs under. `09` §9's structure, in its order. */
export type CommandGroup =
  | 'file'
  | 'layers'
  | 'select'
  | 'edit'
  | 'vertices'
  | 'transform'
  | 'snap'
  | 'view';

/** The geometry a layer holds, which decides which operations apply. */
export type GeometryKind = 'point' | 'line' | 'polygon' | 'mixed' | 'raster';

/**
 * Everything a command predicate may consult.
 *
 * Flat and shallow on purpose: a predicate that has to walk a tree to answer
 * "is anything selected" is a predicate that gets written once and copied.
 */
export interface EditState {
  /** No active layer means no editing at all — `09` §3's single-layer rule. */
  activeLayerId: string | null;
  activeLayerGeometry: GeometryKind | null;
  /** Editable in the permission sense. A viewer opens the tools and finds them
   *  greyed, which is more informative than not having them. */
  canEdit: boolean;

  selection: SelectionScope;
  /** How many features are selected. Some commands need exactly one, some need
   *  two or more, and "some" is not a useful distinction to any of them. */
  selectedFeatureCount: number;
  selectedVertexCount: number;

  /** An operation is running — a split line half drawn, a vertex mid-drag.
   *  `09` §5.1: nothing has entered the dirty buffer or the undo stack. */
  operationActive: boolean;
  /** Applied operations not yet saved. `09` §5.2. */
  dirty: boolean;
  canUndo: boolean;
  canRedo: boolean;
  /** Features on the clipboard, for Paste. */
  clipboardCount: number;

  snapEnabled: boolean;
  topologicalEditing: boolean;
  angleConstraint: boolean;
  showVertices: boolean;
  showMeasurements: boolean;
  showValidationErrors: boolean;

  /** The select mode, which is a radio group rather than a toggle. */
  selectMode: 'click' | 'rectangle' | 'lasso';
}

/**
 * One command, defined once.
 *
 * `run` takes the state it was enabled against rather than reading it again:
 * between a click and a handler, a background refresh can change what is
 * selected, and a command that re-reads would act on something the user did not
 * see when they chose it.
 */
export interface CommandDef {
  id: string;
  label: string;
  /** Shown in the palette, where a bare verb is not enough to choose between
   *  Combine and Dissolve — the pair `09` §9.1 says must never both be called
   *  "Merge". */
  description?: string;
  icon?: () => ReactNode;
  group: CommandGroup;
  /** `mod`, never `ctrl` or `cmd`: the platform difference is the hotkey
   *  layer's business, and writing it twice is how the two get out of step. */
  shortcut?: string;
  /** Extra palette search terms — the words from the other tool a user came
   *  from. Someone looking for "merge" should find Combine *and* Dissolve, and
   *  read the descriptions that distinguish them. */
  keywords?: string[];

  enabled(state: EditState): boolean;
  visible?(state: EditState): boolean;
  checked?(state: EditState): boolean;
  run(state: EditState): void | Promise<void>;

  /** Which context menus this command appears in. Absent means none. */
  contextMenu?: ContextScope[];
}

/** A state with nothing open and nothing selected. The base every test and
 *  every initial render starts from. */
export const IDLE: EditState = {
  activeLayerId: null,
  activeLayerGeometry: null,
  canEdit: false,
  selection: 'none',
  selectedFeatureCount: 0,
  selectedVertexCount: 0,
  operationActive: false,
  dirty: false,
  canUndo: false,
  canRedo: false,
  clipboardCount: 0,
  snapEnabled: true,
  topologicalEditing: false,
  angleConstraint: false,
  showVertices: true,
  showMeasurements: false,
  showValidationErrors: true,
  selectMode: 'click',
};
