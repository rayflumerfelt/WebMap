/**
 * The mode state machine. `09-editing.md` §3.2, §4.
 *
 * Every test here is one of §4's transition rules, because those rules are the
 * whole content of the machine — a mode is a string, and what makes it a state
 * machine is what each transition does to the selection and the operation.
 */

import { describe, expect, it } from 'vitest';

import { INITIAL, reduce, selectionScope } from './modes.js';
import type { ModeEvent, ModeState } from './modes.js';

function run(state: ModeState, ...events: ModeEvent[]): ModeState {
  return events.reduce(reduce, state);
}

const editing: ModeState = { ...INITIAL, activeLayerId: 'layer-1' };

describe('selection across a mode change', () => {
  it('keeps the feature selection', () => {
    // Losing it on every mode switch makes a multi-step edit intolerable:
    // select, move, select again, rotate, select again.
    const after = run(
      editing,
      { type: 'selectFeatures', ids: ['f1', 'f2'] },
      { type: 'setMode', mode: 'move' },
    );
    expect(after.selectedFeatureIds).toEqual(['f1', 'f2']);
  });

  it('drops the vertex selection', () => {
    // Handles pointing at nothing, in a mode with no vertices.
    const after = run(
      editing,
      { type: 'selectFeatures', ids: ['f1'] },
      { type: 'setMode', mode: 'vertex' },
      { type: 'selectVertices', ids: ['v1', 'v2'] },
      { type: 'setMode', mode: 'move' },
    );
    expect(after.selectedVertexIds).toEqual([]);
    expect(after.selectedFeatureIds).toEqual(['f1']);
  });

  it('keeps the vertex selection between the two vertex modes', () => {
    const after = run(
      editing,
      { type: 'setMode', mode: 'vertex' },
      { type: 'selectVertices', ids: ['v1'] },
      { type: 'setMode', mode: 'vertex-add' },
    );
    expect(after.selectedVertexIds).toEqual(['v1']);
  });

  it('clears the feature selection when a draw mode starts', () => {
    // The next click means "start drawing here", and a surviving selection
    // makes Delete ambiguous.
    const after = run(
      editing,
      { type: 'selectFeatures', ids: ['f1'] },
      { type: 'setMode', mode: 'draw-polygon' },
    );
    expect(after.selectedFeatureIds).toEqual([]);
  });

  it('ignores a vertex selection outside a vertex mode', () => {
    // Stored, it would be a ghost: invisible now, and back on the next mode
    // change pointing at whatever was selected minutes ago.
    const after = run(editing, { type: 'selectVertices', ids: ['v1'] });
    expect(after.selectedVertexIds).toEqual([]);
  });

  it('drops the vertex selection when the feature selection changes', () => {
    const after = run(
      editing,
      { type: 'setMode', mode: 'vertex' },
      { type: 'selectVertices', ids: ['v1'] },
      { type: 'selectFeatures', ids: ['f9'] },
    );
    expect(after.selectedVertexIds).toEqual([]);
  });

  it('adds to the selection when asked and replaces it otherwise', () => {
    const additive = run(
      editing,
      { type: 'selectFeatures', ids: ['f1'] },
      { type: 'selectFeatures', ids: ['f2'], additive: true },
    );
    expect(additive.selectedFeatureIds).toEqual(['f1', 'f2']);

    const replaced = run(
      editing,
      { type: 'selectFeatures', ids: ['f1'] },
      { type: 'selectFeatures', ids: ['f2'] },
    );
    expect(replaced.selectedFeatureIds).toEqual(['f2']);
  });

  it('does not double an id already selected', () => {
    const after = run(
      editing,
      { type: 'selectFeatures', ids: ['f1'] },
      { type: 'selectFeatures', ids: ['f1'], additive: true },
    );
    expect(after.selectedFeatureIds).toEqual(['f1']);
  });
});

describe('escape', () => {
  it('cancels the operation on the first press and changes no mode', () => {
    // So a user who mis-drew a split line redraws it without re-entering the
    // tool. Collapsing the two presses means a half-drawn line and a mode
    // change from one keystroke.
    const after = run(
      editing,
      { type: 'setMode', mode: 'split' },
      { type: 'operationStarted' },
      { type: 'escape' },
    );
    expect(after.operationActive).toBe(false);
    expect(after.mode).toBe('split');
  });

  it('returns to select on the second press', () => {
    const after = run(
      editing,
      { type: 'setMode', mode: 'split' },
      { type: 'operationStarted' },
      { type: 'escape' },
      { type: 'escape' },
    );
    expect(after.mode).toBe('select');
  });

  it('always exits to select from any mode', () => {
    for (const mode of ['vertex', 'move', 'draw-line', 'reshape', 'paste-place'] as const) {
      const after = run(editing, { type: 'setMode', mode }, { type: 'escape' });
      expect(after.mode).toBe('select');
    }
  });

  it('keeps the feature selection', () => {
    // Escape leaves a tool; it does not undo what the user picked out.
    const after = run(
      editing,
      { type: 'selectFeatures', ids: ['f1'] },
      { type: 'setMode', mode: 'move' },
      { type: 'escape' },
    );
    expect(after.selectedFeatureIds).toEqual(['f1']);
  });
});

describe('a running operation', () => {
  it('blocks a mode switch', () => {
    // §5.1 keeps operation state out of the dirty buffer, so a switch leaves it
    // nowhere to go. Blocked rather than auto-applied: §5.2 says pick one and
    // hold it, and the one that cannot silently write cannot surprise anybody.
    const after = run(
      editing,
      { type: 'setMode', mode: 'split' },
      { type: 'operationStarted' },
      { type: 'setMode', mode: 'move' },
    );
    expect(after.mode).toBe('split');
  });

  it('allows the switch once it resolves', () => {
    const after = run(
      editing,
      { type: 'setMode', mode: 'split' },
      { type: 'operationStarted' },
      { type: 'operationResolved' },
      { type: 'setMode', mode: 'move' },
    );
    expect(after.mode).toBe('move');
  });
});

describe('switching the active layer', () => {
  it('clears both scopes and cancels the operation', () => {
    // It ends the session. §5.2 requires the dirty buffer to be saved or
    // discarded first, which is the caller's obligation — this reducer has no
    // buffer to inspect and cannot refuse.
    const after = run(
      editing,
      { type: 'setMode', mode: 'vertex' },
      { type: 'selectFeatures', ids: ['f1'] },
      { type: 'operationStarted' },
      { type: 'setActiveLayer', layerId: 'layer-2' },
    );

    expect(after.activeLayerId).toBe('layer-2');
    expect(after.mode).toBe('select');
    expect(after.selectedFeatureIds).toEqual([]);
    expect(after.selectedVertexIds).toEqual([]);
    expect(after.operationActive).toBe(false);
  });

  it('keeps the select tool, which is a preference rather than session state', () => {
    const after = run(
      editing,
      { type: 'setSelectTool', tool: 'lasso' },
      { type: 'setActiveLayer', layerId: 'layer-2' },
    );
    expect(after.selectTool).toBe('lasso');
  });

  it('does nothing when the layer is already active', () => {
    const before = run(editing, { type: 'selectFeatures', ids: ['f1'] });
    const after = reduce(before, { type: 'setActiveLayer', layerId: 'layer-1' });
    expect(after).toBe(before);
  });
});

describe('the scope the command registry reads', () => {
  it('is derived rather than stored', () => {
    // Two fields that could disagree about what is selected is the shape that
    // produces a Delete which deletes the wrong thing.
    expect(selectionScope(editing)).toBe('none');
    expect(selectionScope(run(editing, { type: 'selectFeatures', ids: ['f1'] }))).toBe(
      'feature',
    );
    expect(selectionScope(run(editing, { type: 'selectFeatures', ids: ['f1', 'f2'] }))).toBe(
      'features',
    );
  });

  it('reports vertices only in a vertex mode', () => {
    const inVertexMode = run(
      editing,
      { type: 'selectFeatures', ids: ['f1'] },
      { type: 'setMode', mode: 'vertex' },
      { type: 'selectVertices', ids: ['v1'] },
    );
    expect(selectionScope(inVertexMode)).toBe('vertex');

    const backToSelect = reduce(inVertexMode, { type: 'setMode', mode: 'select' });
    expect(selectionScope(backToSelect)).toBe('feature');
  });
});
