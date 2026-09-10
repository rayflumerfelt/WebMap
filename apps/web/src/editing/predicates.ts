/**
 * The enabled-state vocabulary. `09-editing.md` §8.
 *
 * `enabled` runs on every render of every surface, so these are pure functions
 * over a snapshot and nothing else. They exist as named predicates rather than
 * as inline arrow functions for one reason: **"can this run" is a claim about
 * the editor, and the same claim appears in a dozen commands.** Written inline,
 * a rule like "an operation is in progress, so nothing else may start" gets
 * remembered in eleven places and forgotten in the twelfth.
 *
 * Each is small enough to read at the call site, which is the point — a
 * command's `enabled` should be a sentence.
 */

import type { EditState } from './types.js';

/** There is a layer to edit and permission to edit it. */
export function editable(state: EditState): boolean {
  return state.activeLayerId !== null && state.canEdit;
}

/**
 * Editable, and no operation is half-finished.
 *
 * The base for nearly every mutating command. `09` §5.1 keeps operation state
 * out of the dirty buffer entirely, and starting a second operation while the
 * first is open would leave the first with nowhere to go.
 */
export function ready(state: EditState): boolean {
  return editable(state) && !state.operationActive;
}

export function hasSelection(state: EditState): boolean {
  return state.selectedFeatureCount > 0;
}

export function exactlyOneFeature(state: EditState): boolean {
  return state.selectedFeatureCount === 1;
}

/**
 * Two or more features — what an operation between features needs.
 *
 * Combine, Dissolve and the overlay operations all read this. A "merge" of one
 * feature is a no-op that looks like it worked, which for Dissolve means a
 * geologist believing two leases were unioned when nothing happened.
 */
export function severalFeatures(state: EditState): boolean {
  return state.selectedFeatureCount >= 2;
}

export function hasVertices(state: EditState): boolean {
  return state.selectedVertexCount > 0;
}

/**
 * Features are selected **and vertex editing is not what is happening**.
 *
 * The distinction exists for one key. `Delete` is Delete Feature under Edit and
 * Delete Vertices under Vertices, and with vertices selected both would
 * otherwise be enabled — so the key would mean whichever the hotkey layer found
 * first. With vertices selected it means the vertices, as it does in every
 * editor, and because deleting the whole feature is a far larger action to
 * trigger by accident.
 */
export function featureScope(state: EditState): boolean {
  return (
    state.selectedFeatureCount > 0 &&
    state.selection !== 'vertex' &&
    state.selection !== 'vertices'
  );
}

/** The active layer holds this geometry. */
export function geometryIs(
  state: EditState,
  ...kinds: Array<'point' | 'line' | 'polygon' | 'mixed' | 'raster'>
): boolean {
  return state.activeLayerGeometry !== null && kinds.includes(state.activeLayerGeometry);
}

/**
 * A layer whose features have vertices to edit.
 *
 * A point has one and moving it is Move, not vertex editing; a raster has
 * none. Offering "Edit Vertices" on a well layer is offering a mode that does
 * nothing, which reads as the tool being broken.
 */
export function hasEditableVertices(state: EditState): boolean {
  return geometryIs(state, 'line', 'polygon', 'mixed');
}

/** Areal geometry, for the operations that need a boundary. */
export function areal(state: EditState): boolean {
  return geometryIs(state, 'polygon', 'mixed');
}
