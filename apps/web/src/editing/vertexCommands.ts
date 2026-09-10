/**
 * Vertex operations as commands. `09-editing.md` §3.4, §11.6.
 *
 * `geometryEdits.ts` changes a geometry; `session.ts` records a change. This
 * module is the join: it reads the feature as it currently stands, applies the
 * geometry operation, and returns the `Command` — `before` and `after` for
 * every feature touched — that the undo stack keeps.
 *
 * **`before` is read through `current`, never from the exact cache.** A second
 * edit to a feature must record the state the first edit left, or undoing it
 * silently reverts both.
 *
 * **A multi-vertex delete removes descending ordinals first.** Removing vertex
 * 1 renumbers vertex 2, so ascending order deletes the wrong vertices from the
 * second onward — the sort is the entire content of that function and the
 * reason it is not written inline at the call site.
 */

import type { Geometry } from 'geojson';

import { insertVertex, removeVertex, setVertex } from './geometryEdits.js';
import type { SelectedVertex } from './overlay.js';
import { current } from './session.js';
import type { Command, EditSession, Feature, FeatureDelta } from './session.js';

/** Ids exist to key the undo stack, so uniqueness is all they need. */
function commandId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `command-${Date.now()}-${Math.random()}`;
}

function required(session: EditSession, featureId: string, operation: string): Feature {
  const feature = current(session, featureId);
  if (!feature) {
    throw new Error(
      `Cannot ${operation}: feature '${featureId}' is not in this edit session — ` +
        `it was deleted, or the working set was rebuilt by a pan. Reselect it.`,
    );
  }
  return feature;
}

function delta(before: Feature, geometry: Geometry): FeatureDelta {
  return { featureId: before.id, before, after: { ...before, geometry } };
}

/** Move one vertex to a coordinate. The command a drag commits. */
export function moveVertexCommand(
  session: EditSession,
  vertex: SelectedVertex,
  position: [number, number],
): Command {
  const before = required(session, vertex.featureId, 'move a vertex');
  const geometry = setVertex(
    before.geometry as Geometry,
    vertex.ring,
    vertex.ordinal,
    position,
  );

  return {
    id: commandId(),
    label: 'Move Vertex',
    deltas: [delta(before, geometry)],
    timestamp: Date.now(),
  };
}

/** Add a vertex on a segment. Vertex-add mode's click. */
export function addVertexCommand(
  session: EditSession,
  featureId: string,
  ring: number,
  segmentIndex: number,
  position: [number, number],
): Command {
  const before = required(session, featureId, 'add a vertex');
  const geometry = insertVertex(before.geometry as Geometry, ring, segmentIndex, position);

  return {
    id: commandId(),
    label: 'Add Vertex',
    deltas: [delta(before, geometry)],
    timestamp: Date.now(),
  };
}

/**
 * Delete every selected vertex, across however many features.
 *
 * Descending by ordinal within each ring: removing vertex 1 renumbers vertex 2,
 * so an ascending pass deletes the wrong vertex from the second onward. And one
 * delta per feature rather than one per vertex, because §5.1 makes an operation
 * atomic — an undo must not leave half the vertices deleted.
 *
 * A ring that would fall below its minimum throws from `removeVertex`, and the
 * whole command fails rather than deleting what it could: a partial delete is
 * harder to reason about than a refusal, and §11.6 asks for a rejection.
 */
export function deleteVerticesCommand(
  session: EditSession,
  vertices: readonly SelectedVertex[],
): Command {
  if (vertices.length === 0) {
    throw new Error('No vertices are selected, so there is nothing to delete.');
  }

  const byFeature = new Map<string, SelectedVertex[]>();
  for (const vertex of vertices) {
    const group = byFeature.get(vertex.featureId);
    if (group) group.push(vertex);
    else byFeature.set(vertex.featureId, [vertex]);
  }

  const deltas: FeatureDelta[] = [];
  for (const [featureId, group] of byFeature) {
    const before = required(session, featureId, 'delete a vertex');
    let geometry = before.geometry as Geometry;

    const ordered = [...group].sort(
      (left, right) => right.ring - left.ring || right.ordinal - left.ordinal,
    );
    for (const vertex of ordered) {
      geometry = removeVertex(geometry, vertex.ring, vertex.ordinal);
    }
    deltas.push(delta(before, geometry));
  }

  return {
    id: commandId(),
    label: vertices.length === 1 ? 'Delete Vertex' : `Delete ${vertices.length} Vertices`,
    deltas,
    timestamp: Date.now(),
  };
}
