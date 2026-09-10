/**
 * Vertex operations as commands. `09-editing.md` §3.4, §11.6.
 *
 * Two rules carry these: `before` is the feature as it currently stands, not
 * as the session found it, and a multi-vertex delete works downward through
 * the ordinals.
 */

import { describe, expect, it } from 'vitest';

import { apply, openSession } from './session.js';
import type { EditSession, Feature } from './session.js';
import {
  addVertexCommand,
  deleteVerticesCommand,
  moveVertexCommand,
} from './vertexCommands.js';

const LINE: Feature = {
  id: 'fault',
  geometry: {
    type: 'LineString',
    coordinates: [
      [0, 0],
      [10, 0],
      [20, 0],
      [30, 0],
    ],
  },
  properties: { name: 'Big Lake' },
};

function session(): EditSession {
  return openSession('faults', 3, [LINE]);
}

function coordinatesOf(feature: Feature | null): number[][] {
  return (feature!.geometry as { coordinates: number[][] }).coordinates;
}

describe('moveVertexCommand', () => {
  it('records before and after for the feature it moved', () => {
    const command = moveVertexCommand(session(), { featureId: 'fault', ring: 0, ordinal: 1 }, [
      10, 5,
    ]);

    expect(command.label).toBe('Move Vertex');
    expect(command.deltas).toHaveLength(1);
    expect(coordinatesOf(command.deltas[0]!.before)[1]).toEqual([10, 0]);
    expect(coordinatesOf(command.deltas[0]!.after)[1]).toEqual([10, 5]);
  });

  it('keeps the feature’s attributes', () => {
    // A geometry edit is not an attribute edit. Rebuilding the feature from
    // its geometry alone would blank the name on the first drag.
    const command = moveVertexCommand(session(), { featureId: 'fault', ring: 0, ordinal: 1 }, [
      10, 5,
    ]);

    expect(command.deltas[0]!.after!.properties).toEqual({ name: 'Big Lake' });
  });

  it('reads the state the last edit left, not the one the session opened with', () => {
    // Otherwise the second drag of a vertex records a `before` from before the
    // first, and one undo silently reverts both.
    const live = session();
    apply(live, moveVertexCommand(live, { featureId: 'fault', ring: 0, ordinal: 1 }, [10, 5]));

    const second = moveVertexCommand(live, { featureId: 'fault', ring: 0, ordinal: 1 }, [10, 9]);

    expect(coordinatesOf(second.deltas[0]!.before)[1]).toEqual([10, 5]);
  });

  it('says what to do when the feature is not in the session', () => {
    expect(() =>
      moveVertexCommand(session(), { featureId: 'ghost', ring: 0, ordinal: 0 }, [1, 1]),
    ).toThrow(/Reselect it/);
  });
});

describe('addVertexCommand', () => {
  it('splices into the segment it names', () => {
    const command = addVertexCommand(session(), 'fault', 0, 1, [15, 0]);

    expect(command.label).toBe('Add Vertex');
    expect(coordinatesOf(command.deltas[0]!.after)).toEqual([
      [0, 0],
      [10, 0],
      [15, 0],
      [20, 0],
      [30, 0],
    ]);
  });
});

describe('deleteVerticesCommand', () => {
  it('deletes descending, so the ordinals stay valid', () => {
    // Removing vertex 1 renumbers vertex 2. An ascending pass deletes the
    // wrong vertex from the second onward — and looks right in the first test
    // anyone writes, which uses one vertex.
    const command = deleteVerticesCommand(session(), [
      { featureId: 'fault', ring: 0, ordinal: 1 },
      { featureId: 'fault', ring: 0, ordinal: 2 },
    ]);

    expect(coordinatesOf(command.deltas[0]!.after)).toEqual([
      [0, 0],
      [30, 0],
    ]);
  });

  it('is one delta per feature, however many vertices went', () => {
    // §5.1 makes an operation atomic: an undo must not leave half the
    // vertices deleted.
    const command = deleteVerticesCommand(session(), [
      { featureId: 'fault', ring: 0, ordinal: 1 },
      { featureId: 'fault', ring: 0, ordinal: 2 },
    ]);

    expect(command.deltas).toHaveLength(1);
    expect(command.label).toBe('Delete 2 Vertices');
  });

  it('names one vertex in the singular, for the undo tooltip', () => {
    const command = deleteVerticesCommand(session(), [
      { featureId: 'fault', ring: 0, ordinal: 1 },
    ]);

    expect(command.label).toBe('Delete Vertex');
  });

  it('fails whole rather than deleting what it can', () => {
    // §11.6 rejects a delete that would take a ring below its minimum. A
    // partial delete is harder to reason about than a refusal.
    expect(() =>
      deleteVerticesCommand(session(), [
        { featureId: 'fault', ring: 0, ordinal: 0 },
        { featureId: 'fault', ring: 0, ordinal: 1 },
        { featureId: 'fault', ring: 0, ordinal: 2 },
      ]),
    ).toThrow(/needs at least 2/);
  });

  it('refuses an empty selection rather than pushing an empty command', () => {
    // `apply` rejects a command with no deltas — it would still consume an
    // undo press and appear to do nothing.
    expect(() => deleteVerticesCommand(session(), [])).toThrow(/nothing to delete/);
  });
});
