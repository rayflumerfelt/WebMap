/**
 * The mode state machine and the selection rules. `09-editing.md` §3.2, §4.
 *
 * Exactly one mode is active, always. A reducer rather than a set of flags,
 * because the interesting content of this file is the *transitions* — and
 * every one of §4's rules is a rule about what a transition does to something
 * else:
 *
 * - `Esc` always exits to `select`, and with an operation running the **first**
 *   `Esc` cancels the operation while the second returns to select. Two
 *   presses, two different things, and collapsing them means a half-drawn
 *   split line and a mode change from one keystroke.
 * - **Feature selection survives a mode change; vertex selection does not.**
 *   Losing the feature selection on every mode switch makes a multi-step edit
 *   intolerable; keeping a vertex selection into a mode with no vertices leaves
 *   handles pointing at nothing.
 * - **Entering a draw mode clears the feature selection**, because the next
 *   click means "start drawing here" rather than "add to the selection", and a
 *   surviving selection makes Delete ambiguous.
 * - **A running operation blocks a mode switch.** §5.1 keeps operation state
 *   out of the dirty buffer, so a switch would leave it nowhere to go. Blocked
 *   rather than auto-applied: §5.2 says pick one and hold it, and the one that
 *   cannot silently write is the one that cannot surprise anybody.
 * - **Switching the active layer clears both scopes and cancels any
 *   operation**, and ends the session — which is why §5.2 requires the dirty
 *   buffer to be saved or discarded first.
 */

export type EditMode =
  | 'select'
  | 'vertex'
  | 'vertex-add'
  | 'move'
  | 'draw-polygon'
  | 'draw-line'
  | 'draw-point'
  | 'draw-rectangle'
  | 'draw-freehand'
  | 'split'
  | 'reshape'
  | 'paste-place';

/** A sub-state of `select`, not a mode: it decides how a click selects. */
export type SelectTool = 'click' | 'rectangle' | 'lasso';

export const DRAW_MODES = [
  'draw-polygon',
  'draw-line',
  'draw-point',
  'draw-rectangle',
  'draw-freehand',
] as const;

/** Modes in which a vertex selection means anything. */
export const VERTEX_MODES = ['vertex', 'vertex-add'] as const;

export interface ModeState {
  mode: EditMode;
  selectTool: SelectTool;
  selectedFeatureIds: readonly string[];
  selectedVertexIds: readonly string[];
  /** An operation is mid-flight — a split line half drawn, a vertex dragged
   *  but not dropped. §5.1: nothing has reached the dirty buffer. */
  operationActive: boolean;
  activeLayerId: string | null;
}

export const INITIAL: ModeState = {
  mode: 'select',
  selectTool: 'click',
  selectedFeatureIds: [],
  selectedVertexIds: [],
  operationActive: false,
  activeLayerId: null,
};

export type ModeEvent =
  | { type: 'setMode'; mode: EditMode }
  | { type: 'setSelectTool'; tool: SelectTool }
  | { type: 'selectFeatures'; ids: readonly string[]; additive?: boolean }
  | { type: 'selectVertices'; ids: readonly string[] }
  | { type: 'clearSelection' }
  | { type: 'operationStarted' }
  | { type: 'operationResolved' }
  | { type: 'escape' }
  | { type: 'setActiveLayer'; layerId: string | null };

export function isDrawMode(mode: EditMode): boolean {
  return (DRAW_MODES as readonly string[]).includes(mode);
}

export function isVertexMode(mode: EditMode): boolean {
  return (VERTEX_MODES as readonly string[]).includes(mode);
}

/**
 * Whether a mode switch is allowed right now.
 *
 * Exported so a toolbar can grey the buttons rather than letting a user click
 * one and watch nothing happen — the difference between a tool that says no
 * and a tool that appears broken.
 */
export function canSwitchMode(state: ModeState): boolean {
  return !state.operationActive;
}

export function reduce(state: ModeState, event: ModeEvent): ModeState {
  switch (event.type) {
    case 'setMode': {
      if (!canSwitchMode(state)) return state;
      if (event.mode === state.mode) return state;

      return {
        ...state,
        mode: event.mode,
        // Vertex selection is meaningful only in a vertex mode, so it goes
        // whenever the mode does. Feature selection stays — §3.2.
        selectedVertexIds: isVertexMode(event.mode) ? state.selectedVertexIds : [],
        // A draw mode's next click means "start here", not "add to the
        // selection", and a surviving selection makes Delete ambiguous.
        selectedFeatureIds: isDrawMode(event.mode) ? [] : state.selectedFeatureIds,
      };
    }

    case 'setSelectTool':
      // A sub-state of `select`, and settable from any mode: picking Rectangle
      // from the menu while drawing should decide how selection behaves once
      // drawing ends, rather than being dropped.
      return { ...state, selectTool: event.tool };

    case 'selectFeatures': {
      const ids = event.additive
        ? [...new Set([...state.selectedFeatureIds, ...event.ids])]
        : [...event.ids];
      return {
        ...state,
        selectedFeatureIds: ids,
        // Changing which features are selected invalidates any vertex
        // selection: the vertices belonged to the features that were selected.
        selectedVertexIds: [],
      };
    }

    case 'selectVertices':
      // Ignored outside a vertex mode rather than stored: a vertex selection
      // that is invisible now and reappears on the next mode change is a
      // ghost, and the handles would point at whatever was selected minutes
      // ago.
      if (!isVertexMode(state.mode)) return state;
      return { ...state, selectedVertexIds: [...event.ids] };

    case 'clearSelection':
      return { ...state, selectedFeatureIds: [], selectedVertexIds: [] };

    case 'operationStarted':
      return { ...state, operationActive: true };

    case 'operationResolved':
      return { ...state, operationActive: false };

    case 'escape': {
      // **The two-press rule.** With an operation running the first press
      // cancels it and leaves the mode alone, so a user who mis-drew a split
      // line can redraw it without re-entering the tool. Only the second
      // returns to select.
      if (state.operationActive) return { ...state, operationActive: false };
      if (state.mode === 'select') return { ...state, selectedVertexIds: [] };
      return {
        ...state,
        mode: 'select',
        selectedVertexIds: [],
      };
    }

    case 'setActiveLayer': {
      if (event.layerId === state.activeLayerId) return state;
      // Ends the session. §5.2 requires the dirty buffer to be saved or
      // discarded before this, which is the caller's obligation — this reducer
      // cannot refuse a layer change it has no buffer to inspect.
      return {
        ...INITIAL,
        selectTool: state.selectTool,
        activeLayerId: event.layerId,
      };
    }
  }
}

/**
 * The selection scope, as the command registry reads it.
 *
 * Derived rather than stored: two fields that could disagree about what is
 * selected is the shape that produces a Delete which deletes the wrong thing.
 */
export function selectionScope(
  state: ModeState,
): 'none' | 'feature' | 'features' | 'vertex' | 'vertices' {
  if (isVertexMode(state.mode) && state.selectedVertexIds.length > 0) {
    return state.selectedVertexIds.length === 1 ? 'vertex' : 'vertices';
  }
  if (state.selectedFeatureIds.length === 0) return 'none';
  return state.selectedFeatureIds.length === 1 ? 'feature' : 'features';
}
